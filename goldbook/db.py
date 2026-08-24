import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

from goldbook.fact_check import (
    BranchDecision,
    BranchPredicate,
    BranchStatus,
    FactCheckImpact,
    FactCheckResult,
    FactCheckRun,
    FactCheckStatus,
    FactValue,
    SearchEvidence,
    StoredFactCheck,
    validate_search_evidence,
)

from goldbook.models import (
    ClaimEvaluation,
    ClaimLeg,
    ClaimStatus,
    ClaimType,
    Creator,
    CreatorMetricSample,
    Direction,
    EvaluationVerdict,
    ForecastClaim,
    HorizonSource,
    Instrument,
    IntradayPriceBar,
    Outcome,
    PriceBar,
    ReviewStatus,
    SignalAnalysis,
    TranscriptSegment,
    Video,
    VideoStatus,
)


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    creator_uid: str
    video_bvid: str | None
    status: str
    stage: str
    progress: float
    retries: int
    error: str | None
    cleanup_error: str | None


class Database:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS creators (
                    uid TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    space_url TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    synced_at TEXT
                );
                CREATE TABLE IF NOT EXISTS videos (
                    bvid TEXT PRIMARY KEY,
                    creator_uid TEXT NOT NULL REFERENCES creators(uid) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    duration_sec INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_summary TEXT
                );
                CREATE TABLE IF NOT EXISTS transcripts (
                    id INTEGER PRIMARY KEY,
                    bvid TEXT NOT NULL REFERENCES videos(bvid) ON DELETE CASCADE,
                    start_sec REAL NOT NULL,
                    end_sec REAL NOT NULL,
                    text TEXT NOT NULL,
                    model TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (bvid, start_sec, end_sec)
                );
                CREATE TABLE IF NOT EXISTS analyses (
                    id INTEGER PRIMARY KEY,
                    bvid TEXT NOT NULL REFERENCES videos(bvid) ON DELETE CASCADE,
                    transcript_hash TEXT NOT NULL,
                    raw_response_hash TEXT,
                    signal_json TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    strength INTEGER NOT NULL,
                    confidence REAL NOT NULL,
                    review_status TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    model_name TEXT,
                    prompt_version TEXT,
                    UNIQUE (bvid, revision)
                );
                CREATE TABLE IF NOT EXISTS prices (
                    trade_date TEXT PRIMARY KEY,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intraday_prices (
                    started_at TEXT NOT NULL,
                    interval_minutes INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    provider TEXT NOT NULL,
                    PRIMARY KEY (started_at, interval_minutes)
                );
                CREATE TABLE IF NOT EXISTS outcomes (
                    bvid TEXT PRIMARY KEY REFERENCES videos(bvid) ON DELETE CASCADE,
                    direction TEXT NOT NULL,
                    entry_date TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    signal_id TEXT,
                    review_status TEXT NOT NULL DEFAULT 'needs_review',
                    included INTEGER NOT NULL DEFAULT 0,
                    exit_1d REAL,
                    exit_5d REAL,
                    exit_20d REAL,
                    return_1d REAL,
                    return_5d REAL,
                    return_20d REAL,
                    mature INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS forecast_claims (
                    claim_id TEXT PRIMARY KEY,
                    bvid TEXT NOT NULL REFERENCES videos(bvid) ON DELETE CASCADE,
                    analysis_revision INTEGER NOT NULL,
                    claim_index INTEGER NOT NULL,
                    instrument TEXT NOT NULL,
                    claim_type TEXT NOT NULL,
                    direction TEXT,
                    legs_json TEXT NOT NULL,
                    condition_text TEXT NOT NULL,
                    horizon_text TEXT,
                    horizon_source TEXT NOT NULL,
                    horizon_min_trading_days INTEGER,
                    horizon_max_trading_days INTEGER,
                    horizon_point_trading_days INTEGER,
                    deadline_at TEXT,
                    time_confidence REAL NOT NULL,
                    confidence REAL NOT NULL,
                    evidence_json TEXT NOT NULL,
                    supersedes_claim_id TEXT,
                    status TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    transcript_hash TEXT NOT NULL,
                    UNIQUE (bvid, analysis_revision, claim_index)
                );
                CREATE TABLE IF NOT EXISTS claim_evaluations (
                    claim_id TEXT PRIMARY KEY REFERENCES forecast_claims(claim_id) ON DELETE CASCADE,
                    evaluated_at TEXT NOT NULL,
                    window_start TEXT,
                    window_end TEXT,
                    entry_price REAL,
                    observed_min REAL,
                    observed_max REAL,
                    final_close REAL,
                    closest_price REAL,
                    closest_date TEXT,
                    distance_pct REAL,
                    first_hit_date TEXT,
                    verdict TEXT NOT NULL,
                    mature INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    window_start_at TEXT,
                    window_end_at TEXT,
                    closest_at TEXT,
                    first_hit_at TEXT
                );
                CREATE TABLE IF NOT EXISTS fact_check_runs (
                    run_id TEXT PRIMARY KEY,
                    bvid TEXT NOT NULL REFERENCES videos(bvid) ON DELETE CASCADE,
                    analysis_revision INTEGER NOT NULL,
                    event_description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    search_count INTEGER NOT NULL DEFAULT 0,
                    error_summary TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE (bvid, analysis_revision, event_description)
                );
                CREATE TABLE IF NOT EXISTS fact_check_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES fact_check_runs(run_id) ON DELETE CASCADE,
                    query TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    published_at TEXT,
                    snippet TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fact_check_results (
                    run_id TEXT PRIMARY KEY REFERENCES fact_check_runs(run_id) ON DELETE CASCADE,
                    result_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outcome_recomputation_requirements (
                    bvid TEXT PRIMARY KEY,
                    reason TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL DEFAULT 'video',
                    creator_uid TEXT NOT NULL REFERENCES creators(uid) ON DELETE CASCADE,
                    video_bvid TEXT REFERENCES videos(bvid) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    stage TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    retries INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    cleanup_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            _ensure_outcome_columns(connection)
            _ensure_analysis_columns(connection)
            _ensure_job_columns(connection)
            _ensure_claim_evaluation_columns(connection)
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_creator_sync_per_creator
                ON jobs(kind, creator_uid)
                WHERE kind = 'sync_creator' AND status IN ('pending', 'running')
                """
            )
            _migrate_legacy_outcomes(connection)

    def upsert_creator(self, creator: Creator) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO creators (uid, name, space_url, enabled, synced_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(uid) DO UPDATE SET
                    name = excluded.name,
                    space_url = excluded.space_url,
                    enabled = excluded.enabled,
                    synced_at = excluded.synced_at
                """,
                (
                    creator.uid,
                    creator.name,
                    creator.space_url,
                    int(creator.enabled),
                    _serialize_datetime(creator.synced_at),
                ),
            )
    def list_outcome_recomputation_requirements(self) -> list[tuple[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT bvid, reason FROM outcome_recomputation_requirements ORDER BY bvid"
            ).fetchall()
        return [(row["bvid"], row["reason"]) for row in rows]

    def list_creators(self) -> list[Creator]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM creators ORDER BY uid").fetchall()
        return [
            Creator(
                uid=row["uid"],
                name=row["name"],
                space_url=row["space_url"],
                enabled=bool(row["enabled"]),
                synced_at=_parse_datetime(row["synced_at"]),
            )
            for row in rows
        ]

    def get_creator(self, uid: str) -> Creator | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM creators WHERE uid = ?", (uid,)).fetchone()
        if row is None:
            return None
        return Creator(
            uid=row["uid"], name=row["name"], space_url=row["space_url"],
            enabled=bool(row["enabled"]), synced_at=_parse_datetime(row["synced_at"]),
        )

    def delete_creator(self, uid: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM creators WHERE uid = ?", (uid,))

    def set_creator_enabled(self, uid: str, enabled: bool) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE creators SET enabled = ? WHERE uid = ?", (int(enabled), uid)
            )
            return cursor.rowcount == 1

    def upsert_video(self, video: Video) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO videos (
                    bvid, creator_uid, title, published_at, duration_sec, url, status,
                    error_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bvid) DO UPDATE SET
                    creator_uid = excluded.creator_uid,
                    title = excluded.title,
                    published_at = excluded.published_at,
                    duration_sec = excluded.duration_sec,
                    url = excluded.url,
                    status = excluded.status,
                    error_summary = excluded.error_summary
                """,
                (
                    video.bvid,
                    video.creator_uid,
                    video.title,
                    _serialize_datetime(video.published_at),
                    video.duration_sec,
                    video.url,
                    video.status.value,
                    video.error_summary,
                ),
            )

    def list_videos(self, creator_uid: str) -> list[Video]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM videos WHERE creator_uid = ? ORDER BY published_at DESC",
                (creator_uid,),
            ).fetchall()
        return [
            Video(
                bvid=row["bvid"],
                creator_uid=row["creator_uid"],
                title=row["title"],
                published_at=_parse_datetime(row["published_at"]),
                duration_sec=row["duration_sec"],
                url=row["url"],
                status=VideoStatus(row["status"]),
                error_summary=row["error_summary"],
            )
            for row in rows
        ]

    def get_video(self, bvid: str) -> Video | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM videos WHERE bvid = ?", (bvid,)).fetchone()
        if row is None:
            return None
        return Video(
            bvid=row["bvid"],
            creator_uid=row["creator_uid"],
            title=row["title"],
            published_at=_parse_datetime(row["published_at"]),
            duration_sec=row["duration_sec"],
            url=row["url"],
            status=VideoStatus(row["status"]),
            error_summary=row["error_summary"],
        )

    def update_video_status(
        self, bvid: str, status: VideoStatus | str, *, error_summary: str | None = None
    ) -> None:
        resolved_status = VideoStatus(status)
        with self._connect() as connection:
            connection.execute(
                "UPDATE videos SET status = ?, error_summary = ? WHERE bvid = ?",
                (resolved_status.value, error_summary, bvid),
            )

    def get_transcript_identity(self, bvid: str) -> tuple[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT model, text_hash FROM transcripts
                WHERE bvid = ? ORDER BY id DESC LIMIT 1
                """,
                (bvid,),
            ).fetchone()
        return None if row is None else (row["model"], row["text_hash"])

    def list_transcript_segments(self, bvid: str) -> list[TranscriptSegment]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM transcripts WHERE bvid = ? ORDER BY start_sec, end_sec", (bvid,)
            ).fetchall()
        return [
            TranscriptSegment(
                row["start_sec"], row["end_sec"], row["text"], bvid=row["bvid"],
                model=row["model"], text_hash=row["text_hash"],
                created_at=_parse_datetime(row["created_at"]),
            )
            for row in rows
        ]

    def save_transcript(
        self,
        bvid_or_segments: str | TranscriptSegment | Iterable[TranscriptSegment],
        segments: Iterable[TranscriptSegment] | None = None,
        *,
        model: str | None = None,
        text_hash: str | None = None,
    ) -> None:
        if isinstance(bvid_or_segments, str):
            bvid = bvid_or_segments
            rows = list(segments or ())
        else:
            bvid = None
            rows = (
                [bvid_or_segments]
                if isinstance(bvid_or_segments, TranscriptSegment)
                else list(bvid_or_segments)
            )
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO transcripts (
                    bvid, start_sec, end_sec, text, model, text_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bvid, start_sec, end_sec) DO UPDATE SET
                    text = excluded.text,
                    model = excluded.model,
                    text_hash = excluded.text_hash,
                    created_at = excluded.created_at
                """,
                [
                    _transcript_row(segment, bvid, model, text_hash)
                    for segment in rows
                ],
            )

    def save_analysis(
        self,
        analysis: SignalAnalysis,
        *,
        bvid: str | None = None,
        transcript_hash: str | None = None,
    ) -> None:
        resolved_bvid = bvid or analysis.bvid
        resolved_transcript_hash = transcript_hash or analysis.transcript_hash
        if resolved_bvid is None or resolved_transcript_hash is None:
            raise ValueError("analysis persistence requires bvid and transcript_hash")
        with self._connect() as connection:
            _upsert_analysis(
                connection, analysis, resolved_bvid, resolved_transcript_hash
            )

    def next_analysis_revision(self, bvid: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(revision), -1) + 1 FROM analyses WHERE bvid = ?",
                (bvid,),
            ).fetchone()
        return int(row[0])

    def save_claim_extraction(
        self, analysis: SignalAnalysis, claims: Sequence[ForecastClaim]
    ) -> None:
        if analysis.bvid is None or analysis.transcript_hash is None:
            raise ValueError("claim extraction requires analysis identity")
        if any(
            claim.bvid != analysis.bvid
            or claim.analysis_revision != analysis.revision
            or claim.transcript_hash != analysis.transcript_hash
            for claim in claims
        ):
            raise ValueError("claim identity does not match analysis")
        with self._connect() as connection:
            _upsert_analysis(
                connection, analysis, analysis.bvid, analysis.transcript_hash
            )
            connection.execute(
                "DELETE FROM forecast_claims WHERE bvid = ? AND analysis_revision = ?",
                (analysis.bvid, analysis.revision),
            )
            connection.executemany(
                """
                INSERT INTO forecast_claims (
                    claim_id, bvid, analysis_revision, claim_index, instrument,
                    claim_type, direction, legs_json, condition_text, horizon_text,
                    horizon_source, horizon_min_trading_days,
                    horizon_max_trading_days, horizon_point_trading_days,
                    deadline_at, time_confidence, confidence, evidence_json,
                    supersedes_claim_id, status, model_name, prompt_version,
                    transcript_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [_forecast_claim_row(claim) for claim in claims],
            )

    def get_latest_analysis(self, bvid: str) -> SignalAnalysis | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM analyses WHERE bvid = ? ORDER BY revision DESC LIMIT 1", (bvid,)
            ).fetchone()
        return None if row is None else _analysis_from_row(row)

    def list_videos_with_latest_analysis(self) -> list[tuple[Video, SignalAnalysis | None]]:
        """Return every video with its deterministic latest analysis, if any."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT videos.*, analyses.* FROM videos
                LEFT JOIN analyses ON analyses.id = (
                    SELECT id FROM analyses AS latest
                    WHERE latest.bvid = videos.bvid
                    ORDER BY latest.revision DESC, latest.id DESC
                    LIMIT 1
                )
                ORDER BY videos.published_at DESC, videos.bvid
                """
            ).fetchall()
        return [
            (_video_from_row(row), None if row["id"] is None else _analysis_from_row(row))
            for row in rows
        ]

    def list_analysis_revisions(self, bvid: str) -> list[SignalAnalysis]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM analyses WHERE bvid = ? ORDER BY revision DESC", (bvid,)
            ).fetchall()
        return [_analysis_from_row(row) for row in rows]

    def replace_prices(
        self, prices: Iterable[PriceBar | tuple[str, float, float, float, float]]
    ) -> None:
        rows = [_normalize_price(price) for price in prices]
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO prices (trade_date, open, high, low, close)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(trade_date) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close
                """,
                [
                    (
                        _serialize_date(price.trade_date),
                        price.open,
                        price.high,
                        price.low,
                        price.close,
                    )
                    for price in rows
                ],
            )

    def list_prices(self) -> list[tuple[str, float, float, float, float]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT trade_date, open, high, low, close FROM prices ORDER BY trade_date"
            ).fetchall()
        return [tuple(row) for row in rows]

    def list_price_bars(self) -> list[PriceBar]:
        return [PriceBar(*row) for row in self.list_prices()]

    def upsert_intraday_prices(self, prices: Iterable[IntradayPriceBar]) -> int:
        rows = list(prices)
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO intraday_prices (
                    started_at, interval_minutes, open, high, low, close, provider
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(started_at, interval_minutes) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    provider = excluded.provider
                """,
                [
                    (
                        _serialize_datetime(bar.started_at),
                        bar.interval_minutes,
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.provider,
                    )
                    for bar in rows
                ],
            )
        return len(rows)

    def list_intraday_price_bars(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[IntradayPriceBar]:
        if start is not None:
            _serialize_datetime(start)
        if end is not None:
            _serialize_datetime(end)
        if start is not None and end is not None and end < start:
            raise ValueError("end datetime must not precede start datetime")

        clauses: list[str] = []
        parameters: list[str] = []
        if start is not None:
            clauses.append("started_at >= ?")
            parameters.append(_serialize_datetime(start))
        if end is not None:
            clauses.append("started_at <= ?")
            parameters.append(_serialize_datetime(end))
        query = "SELECT * FROM intraday_prices"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY started_at, interval_minutes"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            IntradayPriceBar(
                started_at=_parse_datetime(row["started_at"]),
                interval_minutes=int(row["interval_minutes"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                provider=row["provider"],
            )
            for row in rows
        ]

    def replace_forecast_claims(
        self,
        bvid: str,
        analysis_revision: int,
        claims: Sequence[ForecastClaim],
    ) -> None:
        if any(
            claim.bvid != bvid or claim.analysis_revision != analysis_revision
            for claim in claims
        ):
            raise ValueError("claim identity does not match replacement scope")
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM forecast_claims WHERE bvid = ? AND analysis_revision = ?",
                (bvid, analysis_revision),
            )
            connection.executemany(
                """
                INSERT INTO forecast_claims (
                    claim_id, bvid, analysis_revision, claim_index, instrument,
                    claim_type, direction, legs_json, condition_text, horizon_text,
                    horizon_source, horizon_min_trading_days,
                    horizon_max_trading_days, horizon_point_trading_days,
                    deadline_at, time_confidence, confidence, evidence_json,
                    supersedes_claim_id, status, model_name, prompt_version,
                    transcript_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [_forecast_claim_row(claim) for claim in claims],
            )

    def list_forecast_claims(
        self, bvid: str, *, latest_only: bool = True
    ) -> list[ForecastClaim]:
        query = "SELECT * FROM forecast_claims WHERE bvid = ?"
        parameters: list[object] = [bvid]
        if latest_only:
            query += (
                " AND analysis_revision = COALESCE((SELECT MAX(revision) "
                "FROM analyses WHERE bvid = ? AND model_name IS NOT NULL "
                "AND prompt_version IS NOT NULL), (SELECT MAX(analysis_revision) "
                "FROM forecast_claims WHERE bvid = ?))"
            )
            parameters.extend((bvid, bvid))
        query += " ORDER BY analysis_revision, claim_index"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_forecast_claim_from_row(row) for row in rows]

    def list_creator_forecast_claims(self, creator_uid: str) -> list[ForecastClaim]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT claims.* FROM forecast_claims AS claims
                JOIN videos ON videos.bvid = claims.bvid
                WHERE videos.creator_uid = ?
                  AND claims.analysis_revision = (
                      COALESCE(
                          (SELECT MAX(analyses.revision) FROM analyses
                           WHERE analyses.bvid = claims.bvid
                             AND analyses.model_name IS NOT NULL
                             AND analyses.prompt_version IS NOT NULL),
                          (SELECT MAX(latest.analysis_revision)
                           FROM forecast_claims AS latest
                           WHERE latest.bvid = claims.bvid)
                      )
                  )
                ORDER BY videos.published_at, claims.claim_index
                """,
                (creator_uid,),
            ).fetchall()
        return [_forecast_claim_from_row(row) for row in rows]

    def save_claim_evaluation(self, value: ClaimEvaluation) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO claim_evaluations (
                    claim_id, evaluated_at, window_start, window_end, entry_price,
                    observed_min, observed_max, final_close, closest_price,
                    closest_date, distance_pct, first_hit_date, verdict, mature, reason,
                    window_start_at, window_end_at, closest_at, first_hit_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(claim_id) DO UPDATE SET
                    evaluated_at = excluded.evaluated_at,
                    window_start = excluded.window_start,
                    window_end = excluded.window_end,
                    entry_price = excluded.entry_price,
                    observed_min = excluded.observed_min,
                    observed_max = excluded.observed_max,
                    final_close = excluded.final_close,
                    closest_price = excluded.closest_price,
                    closest_date = excluded.closest_date,
                    distance_pct = excluded.distance_pct,
                    first_hit_date = excluded.first_hit_date,
                    verdict = excluded.verdict,
                    mature = excluded.mature,
                    reason = excluded.reason,
                    window_start_at = excluded.window_start_at,
                    window_end_at = excluded.window_end_at,
                    closest_at = excluded.closest_at,
                    first_hit_at = excluded.first_hit_at
                """,
                _claim_evaluation_row(value),
            )

    def get_claim_evaluation(self, claim_id: str) -> ClaimEvaluation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM claim_evaluations WHERE claim_id = ?", (claim_id,)
            ).fetchone()
        return None if row is None else _claim_evaluation_from_row(row)

    def list_creator_claim_evaluations(
        self, creator_uid: str
    ) -> list[ClaimEvaluation]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT evaluations.* FROM claim_evaluations AS evaluations
                JOIN forecast_claims AS claims ON claims.claim_id = evaluations.claim_id
                JOIN videos ON videos.bvid = claims.bvid
                WHERE videos.creator_uid = ?
                ORDER BY videos.published_at, claims.claim_index
                """,
                (creator_uid,),
            ).fetchall()
        return [_claim_evaluation_from_row(row) for row in rows]

    def create_fact_check_run(
        self,
        bvid: str,
        analysis_revision: int,
        event_description: str,
        model_name: str,
        *,
        created_at: datetime | None = None,
    ) -> FactCheckRun:
        created = created_at or datetime.now(timezone.utc)
        identity = sha256(
            f"{bvid}\n{analysis_revision}\n{event_description}".encode("utf-8")
        ).hexdigest()[:24]
        run_id = f"fc-{identity}"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO fact_check_runs (
                    run_id, bvid, analysis_revision, event_description, status,
                    model_name, created_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    run_id,
                    bvid,
                    analysis_revision,
                    event_description,
                    model_name,
                    _serialize_datetime(created),
                ),
            )
            row = connection.execute(
                "SELECT * FROM fact_check_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("could not create fact-check run")
        return _fact_check_run_from_row(row)

    def get_fact_check_run(self, run_id: str) -> FactCheckRun | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM fact_check_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return None if row is None else _fact_check_run_from_row(row)

    def save_fact_check_evidence(
        self, run_id: str, evidence: Sequence[SearchEvidence]
    ) -> None:
        checked = tuple(validate_search_evidence(item) for item in evidence)
        if len({item.evidence_id for item in checked}) != len(checked):
            raise ValueError("duplicate fact-check evidence ID")
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM fact_check_runs WHERE run_id = ?", (run_id,)
            ).fetchone() is None:
                raise ValueError("unknown fact-check run")
            connection.execute(
                "DELETE FROM fact_check_evidence WHERE run_id = ?", (run_id,)
            )
            connection.executemany(
                """
                INSERT INTO fact_check_evidence (
                    evidence_id, run_id, query, title, url, domain,
                    published_at, snippet, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.evidence_id,
                        run_id,
                        item.query,
                        item.title,
                        item.url,
                        item.domain,
                        _serialize_datetime(item.published_at),
                        item.snippet,
                        _serialize_datetime(item.fetched_at),
                    )
                    for item in checked
                ],
            )

    def save_fact_check_result(
        self,
        run_id: str,
        result: FactCheckResult,
        *,
        search_count: int,
        completed_at: datetime | None = None,
    ) -> None:
        if search_count < 0:
            raise ValueError("search_count must not be negative")
        completed = completed_at or datetime.now(timezone.utc)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE fact_check_runs
                SET status = 'completed', search_count = ?, error_summary = NULL,
                    completed_at = ?
                WHERE run_id = ?
                """,
                (search_count, _serialize_datetime(completed), run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("unknown fact-check run")
            connection.execute(
                """
                INSERT INTO fact_check_results (run_id, result_json) VALUES (?, ?)
                ON CONFLICT(run_id) DO UPDATE SET result_json = excluded.result_json
                """,
                (run_id, _fact_check_result_json(result)),
            )

    def fail_fact_check_run(self, run_id: str, error_summary: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE fact_check_runs
                SET status = 'failed', error_summary = ?, completed_at = ?
                WHERE run_id = ? AND status IN ('pending', 'searching')
                """,
                (
                    error_summary[:300],
                    _serialize_datetime(datetime.now(timezone.utc)),
                    run_id,
                ),
            )
            return cursor.rowcount == 1

    def get_current_fact_check(self, bvid: str) -> StoredFactCheck | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT runs.*, results.result_json
                FROM fact_check_runs AS runs
                JOIN fact_check_results AS results ON results.run_id = runs.run_id
                WHERE runs.bvid = ? AND runs.status = 'completed'
                  AND runs.analysis_revision = (
                    SELECT MAX(revision) FROM analyses WHERE bvid = ?
                  )
                ORDER BY runs.completed_at DESC, runs.run_id DESC
                LIMIT 1
                """,
                (bvid, bvid),
            ).fetchone()
            if row is None:
                return None
            evidence_rows = connection.execute(
                """
                SELECT * FROM fact_check_evidence
                WHERE run_id = ? ORDER BY fetched_at, evidence_id
                """,
                (row["run_id"],),
            ).fetchall()
        return StoredFactCheck(
            run=_fact_check_run_from_row(row),
            evidence=tuple(_search_evidence_from_row(item) for item in evidence_rows),
            result=_fact_check_result_from_json(row["result_json"]),
        )

    def has_claim_extraction(
        self,
        bvid: str,
        transcript_hash: str,
        model_name: str,
        prompt_version: str,
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM forecast_claims
                WHERE bvid = ? AND transcript_hash = ? AND model_name = ?
                  AND prompt_version = ?
                UNION ALL
                SELECT 1 FROM analyses
                WHERE bvid = ? AND transcript_hash = ? AND model_name = ?
                  AND prompt_version = ?
                LIMIT 1
                """,
                (
                    bvid, transcript_hash, model_name, prompt_version,
                    bvid, transcript_hash, model_name, prompt_version,
                ),
            ).fetchone()
        return row is not None

    def delete_claim_evaluations_except(self, live_ids: set[str]) -> int:
        with self._connect() as connection:
            if not live_ids:
                return connection.execute("DELETE FROM claim_evaluations").rowcount
            placeholders = ",".join("?" for _value in live_ids)
            return connection.execute(
                f"DELETE FROM claim_evaluations WHERE claim_id NOT IN ({placeholders})",
                tuple(sorted(live_ids)),
            ).rowcount

    def save_outcome(self, outcome: Outcome) -> None:
        if outcome.bvid is None or outcome.entry_date is None or outcome.entry_price is None:
            raise ValueError("outcome persistence requires bvid, entry_date, and entry_price")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO outcomes (
                    bvid, direction, entry_date, entry_price, exit_1d, exit_5d, exit_20d,
                    return_1d, return_5d, return_20d, mature, signal_id, review_status, included
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bvid) DO UPDATE SET
                    direction = excluded.direction,
                    entry_date = excluded.entry_date,
                    entry_price = excluded.entry_price,
                    exit_1d = excluded.exit_1d,
                    exit_5d = excluded.exit_5d,
                    exit_20d = excluded.exit_20d,
                    return_1d = excluded.return_1d,
                    return_5d = excluded.return_5d,
                    return_20d = excluded.return_20d,
                    mature = excluded.mature,
                    signal_id = excluded.signal_id,
                    review_status = excluded.review_status,
                    included = excluded.included
                """,
                (
                    outcome.bvid,
                    outcome.direction.value,
                    _serialize_date(outcome.entry_date),
                    outcome.entry_price,
                    outcome.exit_1d,
                    outcome.exit_5d,
                    outcome.exit_20d,
                    outcome.return_1d,
                    outcome.return_5d,
                    outcome.return_20d,
                    int(outcome.mature),
                    outcome.signal_id,
                    outcome.review_status.value,
                    int(outcome.included),
                ),
            )
            connection.execute(
                "DELETE FROM outcome_recomputation_requirements WHERE bvid = ?",
                (outcome.bvid,),
            )

    def get_outcome(self, bvid: str) -> Outcome | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM outcomes WHERE bvid = ?", (bvid,)).fetchone()
        return None if row is None else _outcome_from_row(row)

    def list_creator_outcomes(self, creator_uid: str) -> list[Outcome]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT outcomes.* FROM outcomes
                JOIN videos ON videos.bvid = outcomes.bvid
                WHERE videos.creator_uid = ? ORDER BY videos.published_at DESC
                """,
                (creator_uid,),
            ).fetchall()
        return [_outcome_from_row(row) for row in rows]

    def list_creator_metric_samples(self, creator_uid: str) -> list[CreatorMetricSample]:
        """Return one latest-analysis metric sample per creator video in publication order."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    videos.bvid AS video_bvid,
                    analyses.bvid AS analysis_bvid,
                    analyses.direction AS analysis_direction,
                    analyses.confidence AS analysis_confidence,
                    analyses.review_status AS analysis_review_status,
                    analyses.revision AS analysis_revision,
                    outcomes.bvid AS outcome_bvid,
                    outcomes.signal_id AS outcome_signal_id,
                    outcomes.included AS outcome_included,
                    outcomes.mature AS outcome_mature,
                    outcomes.entry_price AS outcome_entry_price,
                    outcomes.return_1d AS outcome_return_1d,
                    outcomes.return_5d AS outcome_return_5d,
                    outcomes.return_20d AS outcome_return_20d
                FROM videos
                LEFT JOIN analyses ON analyses.id = (
                    SELECT id FROM analyses AS latest
                    WHERE latest.bvid = videos.bvid
                    ORDER BY latest.revision DESC, latest.id DESC
                    LIMIT 1
                )
                LEFT JOIN outcomes ON outcomes.bvid = videos.bvid
                WHERE videos.creator_uid = ?
                ORDER BY videos.published_at DESC, videos.bvid
                """,
                (creator_uid,),
            ).fetchall()
        return [_metric_sample_from_row(row) for row in rows]

    def list_creator_metrics(self):
        """Aggregate the complete latest-analysis sample set for each creator."""
        from goldbook.scoring import aggregate_creator

        return [aggregate_creator(self.list_creator_metric_samples(creator.uid)) for creator in self.list_creators()]

    def delete_outcome(self, bvid: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM outcomes WHERE bvid = ?", (bvid,))
            return cursor.rowcount == 1

    def create_job(
        self,
        kind_or_creator_uid: str,
        creator_uid: str | None = None,
        *,
        video_bvid: str | None = None,
        stage: str = "pending",
        status: str = "pending",
    ) -> int:
        """Create a job while retaining the original creator-first call shape.

        ``create_job("42", video_bvid="BV...")`` creates a video job; the
        explicit ``create_job("sync_creator", "42")`` form names its kind.
        """
        kind = "video" if creator_uid is None else kind_or_creator_uid
        resolved_creator_uid = kind_or_creator_uid if creator_uid is None else creator_uid
        now = _serialize_datetime(datetime.now(timezone.utc))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO jobs (kind, creator_uid, video_bvid, status, stage, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (kind, resolved_creator_uid, video_bvid, status, stage, now, now),
            )
            return int(cursor.lastrowid)

    def create_or_get_active_video_job(self, creator_uid: str, bvid: str) -> Job:
        """Atomically return the one pending/running/paused job for a video."""
        now = _serialize_datetime(datetime.now(timezone.utc))
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO jobs (kind, creator_uid, video_bvid, status, stage, created_at, updated_at)
                    VALUES ('video', ?, ?, 'pending', 'pending', ?, ?)
                    """,
                    (creator_uid, bvid, now, now),
                )
            except sqlite3.IntegrityError:
                pass
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE kind = 'video' AND video_bvid = ?
                  AND status IN ('pending', 'running', 'paused')
                ORDER BY id DESC LIMIT 1
                """,
                (bvid,),
            ).fetchone()
        if row is None:
            raise RuntimeError("could not create or find an active video job")
        return _job_from_row(row)

    def create_or_get_active_fact_check_job(
        self, creator_uid: str, bvid: str
    ) -> Job:
        now = _serialize_datetime(datetime.now(timezone.utc))
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO jobs (
                        kind, creator_uid, video_bvid, status, stage,
                        created_at, updated_at
                    ) VALUES ('fact_check', ?, ?, 'pending', 'pending', ?, ?)
                    """,
                    (creator_uid, bvid, now, now),
                )
            except sqlite3.IntegrityError:
                pass
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE kind = 'fact_check' AND video_bvid = ?
                  AND status IN ('pending', 'running', 'paused')
                ORDER BY id DESC LIMIT 1
                """,
                (bvid,),
            ).fetchone()
        if row is None:
            raise RuntimeError("could not create or find an active fact-check job")
        return _job_from_row(row)

    def get_active_fact_check_job(self, bvid: str) -> Job | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE kind = 'fact_check' AND video_bvid = ?
                  AND status IN ('pending', 'running', 'paused')
                ORDER BY id DESC LIMIT 1
                """,
                (bvid,),
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def create_or_get_active_creator_sync_job(self, creator_uid: str) -> Job:
        """Atomically return the one pending or running discovery job for a creator."""
        now = _serialize_datetime(datetime.now(timezone.utc))
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO jobs (kind, creator_uid, video_bvid, status, stage, created_at, updated_at)
                    VALUES ('sync_creator', ?, NULL, 'pending', 'pending', ?, ?)
                    """,
                    (creator_uid, now, now),
                )
            except sqlite3.IntegrityError:
                pass
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE kind = 'sync_creator' AND creator_uid = ?
                  AND status IN ('pending', 'running')
                ORDER BY id DESC LIMIT 1
                """,
                (creator_uid,),
            ).fetchone()
        if row is None:
            raise RuntimeError("could not create or find an active creator sync job")
        return _job_from_row(row)

    def reassign_job_creator(self, job_id: int | str, creator_uid: str) -> bool:
        """Move a bootstrap discovery job to its resolved public creator line."""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET creator_uid = ? WHERE id = ? AND status = 'running'",
                (creator_uid, job_id),
            )
            return cursor.rowcount == 1

    def claim_pending_job(self, job_id: int | str) -> bool:
        now = _serialize_datetime(datetime.now(timezone.utc))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'running', updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, job_id),
            )
            return cursor.rowcount == 1

    def advance_job(self, job_id: int | str, stage: str, progress: float) -> bool:
        allowed_previous = {
            "downloading": ("pending", "recovered", "downloading"),
            "transcribing": ("downloading", "transcribing"),
            "analyzing": ("transcribing", "analyzing"),
            "pricing": ("analyzing", "pricing"),
            "detecting": ("pending", "recovered", "detecting"),
            "searching": ("detecting", "searching"),
            "validating": ("searching", "validating"),
            "evaluating": ("validating", "evaluating"),
        }
        if stage not in allowed_previous or not 0.0 <= progress <= 1.0:
            raise ValueError("invalid job stage or progress")
        placeholders = ", ".join("?" for _ in allowed_previous[stage])
        now = _serialize_datetime(datetime.now(timezone.utc))
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE jobs SET stage = ?, progress = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND stage IN ({placeholders})
                  AND progress <= ?
                """,
                (stage, progress, now, job_id, *allowed_previous[stage], progress),
            )
            return cursor.rowcount == 1

    def complete_job(self, job_id: int | str) -> bool:
        now = _serialize_datetime(datetime.now(timezone.utc))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'complete', stage = 'complete', progress = 1.0, updated_at = ?
                WHERE id = ? AND status IN ('pending', 'running')
                """,
                (now, job_id),
            )
            return cursor.rowcount == 1

    def fail_job(self, job_id: int | str, stage: str, error: str) -> bool:
        now = _serialize_datetime(datetime.now(timezone.utc))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'failed', stage = ?, error = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (stage, error, now, job_id),
            )
            return cursor.rowcount == 1

    def cancel_job(self, job_id: int | str) -> bool:
        now = _serialize_datetime(datetime.now(timezone.utc))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'cancelled', stage = 'cancelled', updated_at = ?
                WHERE id = ? AND status IN ('pending', 'running', 'paused')
                """,
                (now, job_id),
            )
            return cursor.rowcount == 1

    def pause_job(self, job_id: int | str) -> bool:
        now = _serialize_datetime(datetime.now(timezone.utc))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'paused', stage = 'paused', updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (now, job_id),
            )
            return cursor.rowcount == 1

    def retry_job(self, job_id: int | str) -> bool:
        now = _serialize_datetime(datetime.now(timezone.utc))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = 'pending', stage = 'pending', progress = 0.0, retries = retries + 1,
                    error = NULL, cleanup_error = NULL, updated_at = ?
                WHERE id = ? AND status IN ('failed', 'cancelled', 'paused')
                """,
                (now, job_id),
            )
            return cursor.rowcount == 1

    def record_cleanup_failure(self, job_id: int | str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET cleanup_error = ?, updated_at = ? WHERE id = ?",
                (error, _serialize_datetime(datetime.now(timezone.utc)), job_id),
            )

    def update_job(
        self,
        job_id: int,
        *,
        status: str | None = None,
        stage: str | None = None,
        progress: float | None = None,
        retries: int | None = None,
        error: str | None = None,
    ) -> None:
        current = self.get_job(job_id)
        if current is None:
            raise ValueError("unknown job")
        if status is not None:
            _validate_job_transition(current.status, status)
            if status == "running" and stage not in (None, "downloading"):
                raise ValueError("running jobs must start at downloading")
            if status == "complete" and stage not in (None, "complete"):
                raise ValueError("complete jobs must have complete stage")
        if progress is not None and progress < current.progress:
            raise ValueError("job progress must not decrease")
        if stage is not None and status is None and stage != current.stage:
            raise ValueError("stage changes must use advance_job")
        fields: list[str] = ["updated_at = ?"]
        values: list[object] = [_serialize_datetime(datetime.now(timezone.utc))]
        for column, value in (
            ("status", status),
            ("stage", stage),
            ("progress", progress),
            ("retries", retries),
            ("error", error),
        ):
            if value is not None:
                fields.append(f"{column} = ?")
                values.append(value)
        values.append(job_id)
        values.append(current.status)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE jobs SET {', '.join(fields)} WHERE id = ? AND status = ?", values
            )
            if cursor.rowcount != 1:
                raise ValueError("job state changed concurrently")

    def get_job(self, job_id: int | str) -> Job | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job_from_row(row) if row is not None else None

    def get_active_video_job(self, bvid: str) -> Job | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE kind = 'video' AND video_bvid = ?
                  AND status IN ('pending', 'running', 'paused')
                ORDER BY id DESC LIMIT 1
                """,
                (bvid,),
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def list_pending_video_jobs(self) -> list[Job]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs WHERE kind = 'video' AND status = 'pending'
                ORDER BY id
                """
            ).fetchall()
        return [_job_from_row(row) for row in rows]

    def list_pending_jobs(self) -> list[Job]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status = 'pending' ORDER BY id"
            ).fetchall()
        return [_job_from_row(row) for row in rows]

    def list_jobs(self, limit: int = 30) -> list[Job]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_job_from_row(row) for row in rows]

    def recover_interrupted_jobs(self) -> int:
        """Atomically make jobs interrupted by a prior process runnable again."""
        now = _serialize_datetime(datetime.now(timezone.utc))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = 'pending', stage = 'recovered', updated_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )
            return cursor.rowcount


def _ensure_outcome_columns(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(outcomes)").fetchall()
    }
    for column, definition in (
        ("signal_id", "TEXT"),
        ("review_status", "TEXT NOT NULL DEFAULT 'needs_review'"),
        ("included", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if column not in existing_columns:
            connection.execute(f"ALTER TABLE outcomes ADD COLUMN {column} {definition}")


def _ensure_analysis_columns(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(analyses)").fetchall()
    }
    for column in ("model_name", "prompt_version"):
        if column not in existing_columns:
            connection.execute(f"ALTER TABLE analyses ADD COLUMN {column} TEXT")


def _ensure_claim_evaluation_columns(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(claim_evaluations)").fetchall()
    }
    for column in ("window_start_at", "window_end_at", "closest_at", "first_hit_at"):
        if column not in existing_columns:
            connection.execute(
                f"ALTER TABLE claim_evaluations ADD COLUMN {column} TEXT"
            )


def _ensure_job_columns(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    }
    for column, definition in (
        ("kind", "TEXT NOT NULL DEFAULT 'video'"),
        ("status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("cleanup_error", "TEXT"),
    ):
        if column not in existing_columns:
            connection.execute(f"ALTER TABLE jobs ADD COLUMN {column} {definition}")
    connection.execute("DROP INDEX IF EXISTS active_video_jobs")
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS active_video_jobs
        ON jobs(kind, video_bvid)
        WHERE video_bvid IS NOT NULL AND status IN ('pending', 'running', 'paused')
        """
    )


def _job_from_row(row: sqlite3.Row) -> Job:
    return Job(
        id=int(row["id"]),
        kind=row["kind"],
        creator_uid=row["creator_uid"],
        video_bvid=row["video_bvid"],
        status=row["status"],
        stage=row["stage"],
        progress=float(row["progress"]),
        retries=int(row["retries"]),
        error=row["error"],
        cleanup_error=row["cleanup_error"],
    )


def _validate_job_transition(previous: str, next_status: str) -> None:
    allowed = {
        "pending": {"running", "cancelled", "paused"},
        "running": {"complete", "failed", "cancelled", "paused"},
        "paused": {"pending", "cancelled"},
        "failed": {"pending"},
        "cancelled": {"pending"},
        "complete": set(),
    }
    if next_status not in allowed.get(previous, set()):
        raise ValueError(f"illegal job transition: {previous} -> {next_status}")


def _migrate_legacy_outcomes(connection: sqlite3.Connection) -> None:
    legacy_rows = connection.execute(
        "SELECT bvid, direction FROM outcomes WHERE signal_id IS NULL"
    ).fetchall()
    for row in legacy_rows:
        bvid = row["bvid"]
        analysis = connection.execute(
            """
            SELECT review_status, signal_json, direction FROM analyses
            WHERE bvid = ? ORDER BY revision DESC LIMIT 1
            """,
            (bvid,),
        ).fetchone()
        if analysis is None:
            _mark_outcome_recomputation_required(connection, bvid, "missing analysis provenance")
            _set_legacy_outcome_qualification(connection, bvid, ReviewStatus.NEEDS_REVIEW, False)
            continue

        qualification = _legacy_analysis_qualification(analysis, row["direction"])
        if qualification is None:
            _mark_outcome_recomputation_required(
                connection, bvid, "analysis qualification cannot be proven"
            )
            _set_legacy_outcome_qualification(connection, bvid, ReviewStatus.NEEDS_REVIEW, False)
            continue

        review_status, included, recomputation_reason = qualification
        _set_legacy_outcome_qualification(connection, bvid, review_status, included)
        if recomputation_reason is not None:
            _mark_outcome_recomputation_required(connection, bvid, recomputation_reason)
        else:
            connection.execute(
                "DELETE FROM outcome_recomputation_requirements WHERE bvid = ?", (bvid,)
            )


def _legacy_analysis_qualification(
    analysis: sqlite3.Row, outcome_direction: str
) -> tuple[ReviewStatus, bool, str | None] | None:
    try:
        review_status = ReviewStatus(analysis["review_status"])
        analysis_direction = Direction(analysis["direction"])
        stored_direction = Direction(outcome_direction)
        signal = json.loads(analysis["signal_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if (
        not isinstance(signal, dict)
        or not isinstance(signal.get("is_retrospective"), bool)
        or not isinstance(signal.get("is_news_only"), bool)
    ):
        return None
    if analysis_direction not in (Direction.BULLISH, Direction.BEARISH):
        return review_status, False, "analysis direction is not actionable"
    if analysis_direction is not stored_direction:
        return review_status, False, "analysis direction differs from outcome"
    return review_status, (
        review_status is ReviewStatus.APPROVED
        and not signal["is_retrospective"]
        and not signal["is_news_only"]
    ), None


def _set_legacy_outcome_qualification(
    connection: sqlite3.Connection,
    bvid: str,
    review_status: ReviewStatus,
    included: bool,
) -> None:
    connection.execute(
        """
        UPDATE outcomes
        SET signal_id = ?, review_status = ?, included = ?
        WHERE bvid = ?
        """,
        (bvid, review_status.value, int(included), bvid),
    )


def _mark_outcome_recomputation_required(
    connection: sqlite3.Connection, bvid: str, reason: str
) -> None:
    connection.execute(
        """
        INSERT INTO outcome_recomputation_requirements (bvid, reason) VALUES (?, ?)
        ON CONFLICT(bvid) DO UPDATE SET reason = excluded.reason
        """,
        (bvid, reason),
    )


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("persisted datetimes must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("persisted datetimes must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _serialize_date(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else value


def _normalize_price(
    price: PriceBar | tuple[date | str, float, float, float, float],
) -> PriceBar:
    if isinstance(price, PriceBar):
        return price
    return PriceBar(*price)


def _transcript_row(
    segment: TranscriptSegment,
    bvid: str | None,
    model: str | None,
    text_hash: str | None,
) -> tuple[str, float, float, str, str, str, str]:
    resolved_bvid = segment.bvid or bvid
    if resolved_bvid is None:
        raise ValueError("transcript persistence requires bvid")
    return (
        resolved_bvid,
        segment.start_sec,
        segment.end_sec,
        segment.text,
        segment.model or model or "unknown",
        segment.text_hash or text_hash or sha256(segment.text.encode()).hexdigest(),
        _serialize_datetime(segment.created_at),
    )


def _upsert_analysis(
    connection: sqlite3.Connection,
    analysis: SignalAnalysis,
    bvid: str,
    transcript_hash: str,
) -> None:
    connection.execute(
        """
        INSERT INTO analyses (
            bvid, transcript_hash, raw_response_hash, signal_json, direction,
            strength, confidence, review_status, revision, created_at,
            model_name, prompt_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(bvid, revision) DO UPDATE SET
            transcript_hash = excluded.transcript_hash,
            raw_response_hash = excluded.raw_response_hash,
            signal_json = excluded.signal_json,
            direction = excluded.direction,
            strength = excluded.strength,
            confidence = excluded.confidence,
            review_status = excluded.review_status,
            created_at = excluded.created_at,
            model_name = excluded.model_name,
            prompt_version = excluded.prompt_version
        """,
        (
            bvid,
            transcript_hash,
            analysis.raw_response_hash,
            analysis.signal_json or _analysis_json(analysis),
            analysis.direction.value,
            analysis.strength,
            analysis.confidence,
            analysis.review_status.value,
            analysis.revision,
            _serialize_datetime(analysis.created_at),
            analysis.model_name,
            analysis.prompt_version,
        ),
    )


def _analysis_from_row(row: sqlite3.Row) -> SignalAnalysis:
    payload = json.loads(row["signal_json"])
    return SignalAnalysis(
        direction=Direction(row["direction"]),
        strength=int(row["strength"]),
        confidence=float(row["confidence"]),
        horizon_text=payload.get("horizon_text"),
        target_price=payload.get("target_price"),
        stop_price=payload.get("stop_price"),
        conditions=tuple(payload.get("conditions") or ()),
        is_retrospective=bool(payload.get("is_retrospective")),
        is_news_only=bool(payload.get("is_news_only")),
        evidence=tuple(payload.get("evidence") or ()),
        summary=str(payload.get("summary") or ""),
        review_status=ReviewStatus(row["review_status"]),
        bvid=row["bvid"],
        transcript_hash=row["transcript_hash"],
        raw_response_hash=row["raw_response_hash"],
        signal_json=row["signal_json"],
        revision=int(row["revision"]),
        created_at=_parse_datetime(row["created_at"]),
        model_name=row["model_name"],
        prompt_version=row["prompt_version"],
    )


def _forecast_claim_row(claim: ForecastClaim) -> tuple[object, ...]:
    return (
        claim.claim_id,
        claim.bvid,
        claim.analysis_revision,
        claim.claim_index,
        claim.instrument.value,
        claim.claim_type.value,
        None if claim.direction is None else claim.direction.value,
        json.dumps(
            [
                {
                    "operator": leg.operator,
                    "level_low": leg.level_low,
                    "level_high": leg.level_high,
                }
                for leg in claim.legs
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        claim.condition_text,
        claim.horizon_text,
        claim.horizon_source.value,
        claim.horizon_min_trading_days,
        claim.horizon_max_trading_days,
        claim.horizon_point_trading_days,
        _serialize_datetime(claim.deadline_at),
        claim.time_confidence,
        claim.confidence,
        json.dumps(claim.evidence, ensure_ascii=False, separators=(",", ":")),
        claim.supersedes_claim_id,
        claim.status.value,
        claim.model_name,
        claim.prompt_version,
        claim.transcript_hash,
    )


def _forecast_claim_from_row(row: sqlite3.Row) -> ForecastClaim:
    legs = json.loads(row["legs_json"])
    evidence = json.loads(row["evidence_json"])
    return ForecastClaim(
        claim_id=row["claim_id"],
        bvid=row["bvid"],
        analysis_revision=int(row["analysis_revision"]),
        claim_index=int(row["claim_index"]),
        instrument=Instrument(row["instrument"]),
        claim_type=ClaimType(row["claim_type"]),
        direction=None if row["direction"] is None else Direction(row["direction"]),
        legs=tuple(
            ClaimLeg(
                operator=str(leg["operator"]),
                level_low=leg.get("level_low"),
                level_high=leg.get("level_high"),
            )
            for leg in legs
        ),
        condition_text=row["condition_text"],
        horizon_text=row["horizon_text"],
        horizon_source=HorizonSource(row["horizon_source"]),
        horizon_min_trading_days=row["horizon_min_trading_days"],
        horizon_max_trading_days=row["horizon_max_trading_days"],
        horizon_point_trading_days=row["horizon_point_trading_days"],
        deadline_at=_parse_datetime(row["deadline_at"]),
        time_confidence=float(row["time_confidence"]),
        confidence=float(row["confidence"]),
        evidence=tuple(evidence),
        supersedes_claim_id=row["supersedes_claim_id"],
        status=ClaimStatus(row["status"]),
        model_name=row["model_name"],
        prompt_version=row["prompt_version"],
        transcript_hash=row["transcript_hash"],
    )


def _claim_evaluation_row(value: ClaimEvaluation) -> tuple[object, ...]:
    return (
        value.claim_id,
        _serialize_datetime(value.evaluated_at),
        None if value.window_start is None else value.window_start.isoformat(),
        None if value.window_end is None else value.window_end.isoformat(),
        value.entry_price,
        value.observed_min,
        value.observed_max,
        value.final_close,
        value.closest_price,
        None if value.closest_date is None else value.closest_date.isoformat(),
        value.distance_pct,
        None if value.first_hit_date is None else value.first_hit_date.isoformat(),
        value.verdict.value,
        int(value.mature),
        value.reason,
        _serialize_datetime(value.window_start_at),
        _serialize_datetime(value.window_end_at),
        _serialize_datetime(value.closest_at),
        _serialize_datetime(value.first_hit_at),
    )


def _claim_evaluation_from_row(row: sqlite3.Row) -> ClaimEvaluation:
    return ClaimEvaluation(
        claim_id=row["claim_id"],
        evaluated_at=_parse_datetime(row["evaluated_at"]),
        window_start=None if row["window_start"] is None else date.fromisoformat(row["window_start"]),
        window_end=None if row["window_end"] is None else date.fromisoformat(row["window_end"]),
        entry_price=row["entry_price"],
        observed_min=row["observed_min"],
        observed_max=row["observed_max"],
        final_close=row["final_close"],
        closest_price=row["closest_price"],
        closest_date=None if row["closest_date"] is None else date.fromisoformat(row["closest_date"]),
        distance_pct=row["distance_pct"],
        first_hit_date=None if row["first_hit_date"] is None else date.fromisoformat(row["first_hit_date"]),
        verdict=EvaluationVerdict(row["verdict"]),
        mature=bool(row["mature"]),
        reason=row["reason"],
        window_start_at=_parse_datetime(row["window_start_at"]),
        window_end_at=_parse_datetime(row["window_end_at"]),
        closest_at=_parse_datetime(row["closest_at"]),
        first_hit_at=_parse_datetime(row["first_hit_at"]),
    )


def _fact_check_run_from_row(row: sqlite3.Row) -> FactCheckRun:
    return FactCheckRun(
        run_id=row["run_id"],
        bvid=row["bvid"],
        analysis_revision=int(row["analysis_revision"]),
        event_description=row["event_description"],
        status=FactCheckStatus(row["status"]),
        model_name=row["model_name"],
        search_count=int(row["search_count"]),
        error_summary=row["error_summary"],
        created_at=_parse_datetime(row["created_at"]),
        completed_at=_parse_datetime(row["completed_at"]),
    )


def _search_evidence_from_row(row: sqlite3.Row) -> SearchEvidence:
    return SearchEvidence(
        evidence_id=row["evidence_id"],
        query=row["query"],
        title=row["title"],
        url=row["url"],
        domain=row["domain"],
        published_at=_parse_datetime(row["published_at"]),
        snippet=row["snippet"],
        fetched_at=_parse_datetime(row["fetched_at"]),
    )


def _fact_check_result_json(value: FactCheckResult) -> str:
    return json.dumps(
        {
            "question": value.question,
            "event_name": value.event_name,
            "event_time_utc": _serialize_datetime(value.event_time_utc),
            "facts": [
                {
                    "name": item.name,
                    "actual": item.actual,
                    "forecast": item.forecast,
                    "previous": item.previous,
                    "unit": item.unit,
                }
                for item in value.facts
            ],
            "impact": value.impact.value,
            "reasoning_summary": value.reasoning_summary,
            "evidence_ids": value.evidence_ids,
            "branch_decisions": [
                {
                    "claim_id": item.claim_id,
                    "predicate": item.predicate.value,
                    "status": item.status.value,
                    "reason": item.reason,
                }
                for item in value.branch_decisions
            ],
            "confidence": value.confidence,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _fact_check_result_from_json(raw: str) -> FactCheckResult:
    payload = json.loads(raw)
    return FactCheckResult(
        question=payload["question"],
        event_name=payload["event_name"],
        event_time_utc=_parse_datetime(payload.get("event_time_utc")),
        facts=tuple(
            FactValue(
                item["name"],
                item.get("actual"),
                item.get("forecast"),
                item.get("previous"),
                item.get("unit"),
            )
            for item in payload["facts"]
        ),
        impact=FactCheckImpact(payload["impact"]),
        reasoning_summary=payload["reasoning_summary"],
        evidence_ids=tuple(payload["evidence_ids"]),
        branch_decisions=tuple(
            BranchDecision(
                item["claim_id"],
                BranchPredicate(item["predicate"]),
                BranchStatus(item["status"]),
                item["reason"],
            )
            for item in payload["branch_decisions"]
        ),
        confidence=float(payload["confidence"]),
    )


def _video_from_row(row: sqlite3.Row) -> Video:
    return Video(
        bvid=row["bvid"],
        creator_uid=row["creator_uid"],
        title=row["title"],
        published_at=_parse_datetime(row["published_at"]),
        duration_sec=row["duration_sec"],
        url=row["url"],
        status=VideoStatus(row["status"]),
        error_summary=row["error_summary"],
    )


def _metric_sample_from_row(row: sqlite3.Row) -> CreatorMetricSample:
    if row["analysis_bvid"] is None:
        return CreatorMetricSample(
            bvid=row["video_bvid"], signal_id=None, direction=None, review_status=None,
            included=False, mature=False, entry_price=None, return_1d=None,
            return_5d=None, return_20d=None, confidence=None, manual_revision=False,
            disposition="unanalysed",
        )
    review_status = ReviewStatus(row["analysis_review_status"])
    has_outcome = row["outcome_bvid"] is not None
    return CreatorMetricSample(
        bvid=row["video_bvid"],
        signal_id=row["outcome_signal_id"] if has_outcome else row["video_bvid"],
        direction=Direction(row["analysis_direction"]),
        review_status=review_status,
        included=bool(row["outcome_included"]) if has_outcome else False,
        mature=bool(row["outcome_mature"]) if has_outcome else False,
        entry_price=row["outcome_entry_price"] if has_outcome else None,
        return_1d=row["outcome_return_1d"] if has_outcome else None,
        return_5d=row["outcome_return_5d"] if has_outcome else None,
        return_20d=row["outcome_return_20d"] if has_outcome else None,
        confidence=float(row["analysis_confidence"]),
        manual_revision=int(row["analysis_revision"]) > 0,
        disposition=review_status.value,
    )


def _outcome_from_row(row: sqlite3.Row) -> Outcome:
    return Outcome(
        direction=Direction(row["direction"]),
        entry_date=row["entry_date"], entry_price=row["entry_price"], bvid=row["bvid"],
        exit_1d=row["exit_1d"], exit_5d=row["exit_5d"], exit_20d=row["exit_20d"],
        return_1d=row["return_1d"], return_5d=row["return_5d"],
        return_20d=row["return_20d"], mature=bool(row["mature"]),
        signal_id=row["signal_id"], review_status=ReviewStatus(row["review_status"]),
        included=bool(row["included"]),
    )


def _analysis_json(analysis: SignalAnalysis) -> str:
    return json.dumps(
        {
            "horizon_text": analysis.horizon_text,
            "target_price": analysis.target_price,
            "stop_price": analysis.stop_price,
            "conditions": analysis.conditions,
            "is_retrospective": analysis.is_retrospective,
            "is_news_only": analysis.is_news_only,
            "evidence": analysis.evidence,
            "summary": analysis.summary,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
