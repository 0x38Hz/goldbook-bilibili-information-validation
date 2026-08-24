"""Command line entrypoint for the local-only Goldbook research tool."""

from __future__ import annotations

import argparse
import atexit
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
from typing import Sequence

import httpx

from goldbook.bilibili import BilibiliSource
from goldbook.claim_evaluation import recompute_claim_evaluations
from goldbook.claim_pipeline import reanalyse_cached_claims
from goldbook.config import Settings
from goldbook.db import Database
from goldbook.fact_check_agent import M3FactCheckAgent
from goldbook.jobs import BackgroundRunner, PipelineService
from goldbook.intraday_market import XausIntradayMarketDataSource
from goldbook.market import FallbackMarketDataSource, StooqMarketDataSource, XausMarketDataSource
from goldbook.minimax import MiniMaxClient
from goldbook.minimax_search import MiniMaxWebSearchClient
from goldbook.recompute import recompute_cached_outcomes
from goldbook.transcribe import WhisperTranscriber
from goldbook.web import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Goldbook local research commands")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the local dashboard on 127.0.0.1")
    serve.add_argument("--data-dir", type=Path, help="use a local data directory for this server")
    serve.add_argument("--database-name", default="goldbook.db", help="SQLite filename in --data-dir")
    commands.add_parser("refresh-prices", help="refresh cached XAU/USD daily prices")
    claims = commands.add_parser(
        "reanalyse-claims", help="extract claim-level forecasts from cached transcripts"
    )
    claims.add_argument(
        "--data-dir", type=Path, help="use a local data directory for cached transcripts"
    )
    fact_check = commands.add_parser(
        "fact-check", help="run cited M3 web fact checking for one cached video"
    )
    fact_check.add_argument("--bvid", required=True, help="cached Bilibili video ID")
    fact_check.add_argument(
        "--data-dir", type=Path, help="use a local data directory for the cached video"
    )
    demo = commands.add_parser("seed-demo", help="seed fictional offline demonstration data")
    demo.add_argument("--data-dir", type=Path, help="isolated directory for the demo database")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "seed-demo":
        return _seed_demo(args.data_dir)

    settings = _load_settings()
    if args.command in {"serve", "reanalyse-claims", "fact-check"} and args.data_dir is not None:
        settings = replace(settings, data_dir=args.data_dir)
    database = _database(settings, args.database_name if args.command == "serve" else "goldbook.db")

    if args.command == "refresh-prices":
        return _refresh_prices(settings, database)
    if args.command == "reanalyse-claims":
        return _reanalyse_claims(settings, database)
    if args.command == "fact-check":
        return _fact_check(settings, database, args.bvid)
    return _serve(settings, database)


def _load_settings() -> Settings:
    environment = dict(os.environ)
    environment.update({key: value for key, value in _read_dotenv(Path(".env")).items() if key not in environment})
    return Settings.from_env(environment)


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    return values


def _seed_demo(requested_data_dir: Path | None) -> int:
    production_settings = _load_settings()
    data_dir = requested_data_dir or Path("data") / "demo"
    if _same_path(data_dir, production_settings.data_dir):
        raise ValueError("demo data directory cannot be the production data directory")
    database = _open_database(data_dir / "goldbook-demo.db")
    from scripts.seed_demo import seed_demo

    seed_demo(database)
    print(f"Offline demo data is ready in {database.path}")
    return 0


def _database(settings: Settings, database_name: str = "goldbook.db") -> Database:
    return _open_database(settings.data_dir / database_name)


def _open_database(path: Path) -> Database:
    path.parent.mkdir(parents=True, exist_ok=True)
    database = Database(path)
    database.initialize()
    database.recover_interrupted_jobs()
    return database


def _same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve() == right.expanduser().resolve()


def _serve(settings: Settings, database: Database) -> int:
    http_client = httpx.Client()
    service = _pipeline_service(settings, database, http_client)
    runner = BackgroundRunner(service)
    runner.start()
    atexit.register(runner.stop)
    atexit.register(http_client.close)
    try:
        create_app(settings, database, service).run(
            host=settings.web_host,
            port=settings.web_port,
            debug=False,
            use_reloader=False,
        )
    finally:
        runner.stop()
        http_client.close()
    return 0


def _pipeline_service(
    settings: Settings, database: Database, http_client: httpx.Client
) -> PipelineService:
    analyzer = MiniMaxClient(settings, http_client=http_client)
    fact_checker = None
    if settings.minimax_api_key:
        fact_checker = M3FactCheckAgent(
            analyzer,
            MiniMaxWebSearchClient(
                settings.minimax_base_url,
                settings.minimax_api_key,
                http_client=http_client,
            ),
        )
    return PipelineService(
        db=database,
        source=BilibiliSource(temporary_root=settings.data_dir / "tmp"),
        transcriber=WhisperTranscriber(settings.whisper_model, settings.whisper_device),
        analyzer=analyzer,
        market=_market_data_source(http_client),
        temp_root=settings.data_dir / "tmp",
        fact_checker=fact_checker,
    )


def _refresh_prices(settings: Settings, database: Database) -> int:
    evaluated_at = datetime.now(timezone.utc)
    with httpx.Client() as client:
        source = _market_data_source(client)
        bars = source.fetch(
            date.today() - timedelta(days=settings.lookback_days), date.today()
        )
        intraday_source = _intraday_market_data_source(client)
        intraday_bars = intraday_source.fetch(
            evaluated_at - timedelta(days=366), evaluated_at
        )
    database.replace_prices(bars)
    database.upsert_intraday_prices(intraday_bars)
    summary = recompute_cached_outcomes(database)
    claim_summary = recompute_claim_evaluations(
        database, evaluated_at=evaluated_at
    )
    print(
        f"Refreshed {len(bars)} XAU/USD daily bars via {source.provider_name}; "
        f"{len(intraday_bars)} hourly bars via {intraday_source.provider_name}; "
        f"recomputed {summary.upserted} legacy outcomes and "
        f"{claim_summary.evaluated} claim evaluations "
        f"({claim_summary.unresolved} unresolved)."
    )
    return 0


def _reanalyse_claims(settings: Settings, database: Database) -> int:
    if not settings.minimax_api_key:
        raise ValueError("MINIMAX_API_KEY is required")
    with httpx.Client() as http_client:
        client = MiniMaxClient(settings, http_client=http_client)
        summary = reanalyse_cached_claims(
            database,
            client,
            evaluated_at=datetime.now(timezone.utc),
            on_progress=lambda done, total, bvid, status: print(
                f"{done}/{total} {bvid} {status}", flush=True
            ),
        )
    print(
        f"Claim reanalysis: {summary.completed} completed, "
        f"{summary.skipped} skipped, {summary.failed} failed."
    )
    return 1 if summary.failed else 0


def _fact_check(settings: Settings, database: Database, bvid: str) -> int:
    if not settings.minimax_api_key:
        raise ValueError("MINIMAX_API_KEY is required")
    with httpx.Client() as http_client:
        service = _pipeline_service(settings, database, http_client)
        job = service.enqueue_fact_check(bvid)
        service.process_fact_check(bvid)
    finished = database.get_job(job.id)
    stored = database.get_current_fact_check(bvid)
    if finished is None or finished.status != "complete" or stored is None:
        print("Fact check did not complete; the job remains available for a safe retry.")
        return 1
    print(
        f"Fact check completed for {bvid}: {stored.result.impact.value}; "
        f"{stored.run.search_count} searches, {len(stored.evidence)} saved sources."
    )
    return 0


def _market_data_source(client: httpx.Client) -> FallbackMarketDataSource:
    return FallbackMarketDataSource(StooqMarketDataSource(client), XausMarketDataSource(client))


def _intraday_market_data_source(
    client: httpx.Client,
) -> XausIntradayMarketDataSource:
    return XausIntradayMarketDataSource(client)


if __name__ == "__main__":
    raise SystemExit(main())
