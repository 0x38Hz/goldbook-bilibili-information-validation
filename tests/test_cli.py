from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
import os
import shutil
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from goldbook.__main__ import build_parser, main
from goldbook.config import Settings
from goldbook.db import Database
from goldbook.models import IntradayPriceBar, PriceBar


def test_cli_has_only_local_research_commands():
    parser = build_parser()

    assert parser.parse_args(["serve"]).command == "serve"
    assert parser.parse_args(["refresh-prices"]).command == "refresh-prices"
    assert parser.parse_args(["seed-demo"]).command == "seed-demo"
    assert parser.parse_args(["reanalyse-claims"]).command == "reanalyse-claims"
    fact_check = parser.parse_args(["fact-check", "--bvid", "BV1CPI"])
    assert fact_check.command == "fact-check"
    assert fact_check.bvid == "BV1CPI"


def test_cli_does_not_offer_public_deploy_or_trade_commands():
    help_text = build_parser().format_help().lower()

    assert "deploy" not in help_text
    assert "trade" not in help_text


def test_seed_demo_main_uses_an_isolated_database_in_chinese_space_directory(tmp_path, monkeypatch):
    cwd = tmp_path / "中文 空格目录"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("GOLDBOOK_DATA_DIR", raising=False)

    assert main(["seed-demo"]) == 0
    assert main(["seed-demo"]) == 0

    demo_database = cwd / "data" / "demo" / "goldbook-demo.db"
    production_database = cwd / "data" / "goldbook.db"
    db = Database(demo_database)
    db.initialize()
    assert demo_database.is_file()
    assert not production_database.exists()
    assert len(db.list_creators()) == 2
    assert sum(len(db.list_videos(creator.uid)) for creator in db.list_creators()) == 6
    assert len(db.list_prices()) == 30


def test_seed_demo_main_rejects_the_production_data_directory(tmp_path, monkeypatch):
    production_dir = tmp_path / "生产 数据"
    monkeypatch.setenv("GOLDBOOK_DATA_DIR", str(production_dir))

    with pytest.raises(ValueError, match="production data directory"):
        main(["seed-demo", "--data-dir", str(production_dir)])

    assert not (production_dir / "goldbook.db").exists()


def test_main_dispatches_serve_through_the_composition_root(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    database = object()
    seen: list[object] = []
    monkeypatch.setattr("goldbook.__main__._load_settings", lambda: settings)
    monkeypatch.setattr("goldbook.__main__._database", lambda actual, name="goldbook.db": database)
    monkeypatch.setattr(
        "goldbook.__main__._serve",
        lambda actual_settings, actual_database: seen.extend((actual_settings, actual_database)) or 17,
    )

    assert main(["serve"]) == 17
    assert seen == [settings, database]


def test_main_dispatches_explicit_demo_server_database(monkeypatch, tmp_path):
    settings = _settings(tmp_path / "production")
    demo_dir = tmp_path / "演示 数据"
    database = object()
    opened: list[object] = []
    seen: list[object] = []
    monkeypatch.setattr("goldbook.__main__._load_settings", lambda: settings)
    monkeypatch.setattr(
        "goldbook.__main__._database",
        lambda actual, name="goldbook.db": opened.extend((actual, name)) or database,
    )
    monkeypatch.setattr(
        "goldbook.__main__._serve",
        lambda actual_settings, actual_database: seen.extend((actual_settings, actual_database)) or 19,
    )

    assert main(["serve", "--data-dir", str(demo_dir), "--database-name", "goldbook-demo.db"]) == 19
    assert opened[0].data_dir == demo_dir
    assert opened[1] == "goldbook-demo.db"
    assert seen == [opened[0], database]


def test_main_dispatches_refresh_prices_through_the_composition_root(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    database = object()
    seen: list[object] = []
    monkeypatch.setattr("goldbook.__main__._load_settings", lambda: settings)
    monkeypatch.setattr("goldbook.__main__._database", lambda actual, name="goldbook.db": database)
    monkeypatch.setattr(
        "goldbook.__main__._refresh_prices",
        lambda actual_settings, actual_database: seen.extend((actual_settings, actual_database)) or 23,
    )

    assert main(["refresh-prices"]) == 23
    assert seen == [settings, database]


def test_main_dispatches_cached_claim_reanalysis_through_composition_root(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    database = object()
    seen: list[object] = []
    monkeypatch.setattr("goldbook.__main__._load_settings", lambda: settings)
    monkeypatch.setattr("goldbook.__main__._database", lambda actual, name="goldbook.db": database)
    monkeypatch.setattr(
        "goldbook.__main__._reanalyse_claims",
        lambda actual_settings, actual_database: seen.extend((actual_settings, actual_database)) or 29,
    )

    assert main(["reanalyse-claims"]) == 29
    assert seen == [settings, database]


def test_main_dispatches_one_video_fact_check_through_composition_root(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    database = object()
    seen: list[object] = []
    monkeypatch.setattr("goldbook.__main__._load_settings", lambda: settings)
    monkeypatch.setattr("goldbook.__main__._database", lambda actual, name="goldbook.db": database)
    monkeypatch.setattr(
        "goldbook.__main__._fact_check",
        lambda actual_settings, actual_database, bvid: seen.extend(
            (actual_settings, actual_database, bvid)
        ) or 31,
        raising=False,
    )

    assert main(["fact-check", "--bvid", "BV1CPI"]) == 31
    assert seen == [settings, database, "BV1CPI"]


def test_refresh_prices_fetches_both_before_persisting_and_recomputing(monkeypatch, tmp_path, capsys):
    from goldbook.__main__ import _refresh_prices

    events: list[str] = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeDatabase:
        def replace_prices(self, bars):
            assert list(bars) == [PriceBar("2026-08-02", 100.0, 100.0, 100.0, 100.0)]
            events.append("daily-save")

        def upsert_intraday_prices(self, bars):
            assert list(bars) == [hourly_bar]
            events.append("hourly-save")
            return 1

    database = FakeDatabase()
    fallback_bar = PriceBar("2026-08-02", 100.0, 100.0, 100.0, 100.0)
    hourly_bar = IntradayPriceBar(
        datetime(2026, 8, 2, 10, tzinfo=timezone.utc),
        60,
        100.0,
        101.0,
        99.0,
        100.5,
        "XAUS-hourly",
    )

    class UnavailableStooq:
        def fetch(self, _start, _end):
            request = httpx.Request("GET", "https://stooq.com/q/d/l/")
            raise httpx.HTTPStatusError("bot protection", request=request, response=httpx.Response(404, request=request))

    monkeypatch.setattr("goldbook.__main__.httpx.Client", FakeClient)
    monkeypatch.setattr(
        "goldbook.__main__._market_data_source",
        lambda _client: SimpleNamespace(
            provider_name="XAUS (xaus.com; Yahoo Finance proxy)",
            fetch=lambda _start, _end: events.append("daily-fetch") or [fallback_bar],
        ),
    )
    monkeypatch.setattr(
        "goldbook.__main__._intraday_market_data_source",
        lambda _client: SimpleNamespace(
            provider_name="XAUS-hourly",
            fetch=lambda _start, _end: events.append("hourly-fetch") or [hourly_bar],
        ),
        raising=False,
    )
    monkeypatch.setattr(
        "goldbook.__main__.recompute_cached_outcomes",
        lambda actual_database: events.append("legacy-recompute") or SimpleNamespace(upserted=1),
    )
    monkeypatch.setattr(
        "goldbook.__main__.recompute_claim_evaluations",
        lambda actual_database, evaluated_at: events.append("claim-recompute")
        or SimpleNamespace(evaluated=2, unresolved=1),
    )

    assert _refresh_prices(_settings(tmp_path), database) == 0
    assert events == [
        "daily-fetch",
        "hourly-fetch",
        "daily-save",
        "hourly-save",
        "legacy-recompute",
        "claim-recompute",
    ]
    output = capsys.readouterr().out
    assert "via XAUS (xaus.com; Yahoo Finance proxy)" in output
    assert "1 hourly bars via XAUS-hourly" in output


def test_hourly_fetch_failure_preserves_both_existing_caches(monkeypatch, tmp_path):
    from goldbook.__main__ import _refresh_prices

    database = Database(tmp_path / "goldbook.db")
    database.initialize()
    database.replace_prices([PriceBar("2026-08-01", 90, 91, 89, 90.5)])
    original_hour = IntradayPriceBar(
        datetime(2026, 8, 1, 10, tzinfo=timezone.utc),
        60,
        90,
        91,
        89,
        90.5,
        "cached",
    )
    database.upsert_intraday_prices((original_hour,))

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fail(_start, _end):
        request = httpx.Request("GET", "https://xaus.com/api/v1/chart")
        raise httpx.HTTPStatusError(
            "upstream unavailable",
            request=request,
            response=httpx.Response(503, request=request),
        )

    monkeypatch.setattr("goldbook.__main__.httpx.Client", FakeClient)
    monkeypatch.setattr(
        "goldbook.__main__._market_data_source",
        lambda _client: SimpleNamespace(
            provider_name="new-daily",
            fetch=lambda _start, _end: [PriceBar("2026-08-02", 100, 101, 99, 100.5)],
        ),
    )
    monkeypatch.setattr(
        "goldbook.__main__._intraday_market_data_source",
        lambda _client: SimpleNamespace(provider_name="new-hourly", fetch=fail),
        raising=False,
    )

    with pytest.raises(httpx.HTTPStatusError):
        _refresh_prices(_settings(tmp_path), database)

    assert database.list_prices() == [("2026-08-01", 90.0, 91.0, 89.0, 90.5)]
    assert database.list_intraday_price_bars() == [original_hour]


_POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


@pytest.mark.skipif(_POWERSHELL is None, reason="PowerShell is unavailable")
def test_powershell_scripts_parse_and_fail_without_prerequisites(tmp_path):
    project_root = Path(__file__).parents[1]
    script_root = tmp_path / "中文 空格项目" / "scripts"
    script_root.mkdir(parents=True)
    setup = script_root / "setup.ps1"
    start = script_root / "start.ps1"
    shutil.copy2(project_root / "scripts" / "setup.ps1", setup)
    shutil.copy2(project_root / "scripts" / "start.ps1", start)

    escaped_root = str(project_root).replace("'", "''")
    parse_command = (
        f"Set-Location -LiteralPath '{escaped_root}';"
        "$errors=@();$tokens=@();"
        "[void][System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path 'scripts\\setup.ps1'),[ref]$tokens,[ref]$errors);"
        "[void][System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path 'scripts\\start.ps1'),[ref]$tokens,[ref]$errors);"
        "if($errors.Count){$errors|ForEach-Object{$_.ErrorId};exit 1}"
    )
    parsed = subprocess.run(
        [
            _POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            base64.b64encode(parse_command.encode("utf-16-le")).decode("ascii"),
        ],
        text=True,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        cwd=project_root,
    )
    assert parsed.returncode == 0, parsed.stderr or parsed.stdout

    isolated_env = os.environ.copy()
    isolated_env.update(
        {
            "PATH": os.pathsep.join([os.environ["SystemRoot"] + "\\System32"]),
            "LOCALAPPDATA": str(tmp_path / "local"),
            "USERPROFILE": str(tmp_path / "profile"),
        }
    )
    setup_result = subprocess.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(setup)],
        text=True,
        capture_output=True,
        check=False,
        env=isolated_env,
        encoding="utf-8",
        errors="replace",
    )
    assert setup_result.returncode != 0
    assert "Python 3.12 was not found" in ((setup_result.stdout or "") + (setup_result.stderr or ""))

    start_result = subprocess.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(start)],
        text=True,
        capture_output=True,
        check=False,
        env=isolated_env,
        encoding="utf-8",
        errors="replace",
    )
    assert start_result.returncode != 0
    assert ".env is required" in ((start_result.stdout or "") + (start_result.stderr or ""))


def test_setup_pip_installs_ignore_user_pip_configuration():
    setup = (Path(__file__).parents[1] / "scripts" / "setup.ps1").read_text(encoding="utf-8")
    pip_install_commands = [
        line.strip()
        for line in setup.splitlines()
        if line.strip().startswith("& $venvPython -m pip install")
    ]

    assert pip_install_commands == [
        "& $venvPython -m pip install --upgrade pip",
        "& $venvPython -m pip install -r requirements.txt -r requirements-dev.txt",
    ]
    assert "$pipIsolationConfig" in setup
    assert "$env:PIP_CONFIG_FILE = $pipIsolationConfig" in setup
    assert "extra-index-url =" in setup
    assert "'PIP_NO_INDEX'" not in setup
    assert "'PIP_INDEX_URL'" in setup
    assert "'PIP_EXTRA_INDEX_URL'" in setup
    assert "'PIP_TRUSTED_HOST'" in setup
    assert "finally {" in setup


@pytest.mark.skipif(_POWERSHELL is None, reason="PowerShell is unavailable")
def test_setup_accepts_an_explicit_python312_executable_without_falling_back(tmp_path):
    bundled_python = Path(
        r"C:\Users\ran\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    )
    if not bundled_python.is_file():
        pytest.skip("the Codex bundled Python 3.12 runtime is unavailable")

    project_root = Path(__file__).parents[1]
    script_root = tmp_path / "isolated project" / "scripts"
    script_root.mkdir(parents=True)
    setup = script_root / "setup.ps1"
    shutil.copy2(project_root / "scripts" / "setup.ps1", setup)
    shutil.copy2(project_root / "requirements.txt", script_root.parent / "requirements.txt")
    shutil.copy2(project_root / "requirements-dev.txt", script_root.parent / "requirements-dev.txt")

    isolated_env = os.environ.copy()
    isolated_env.update({"PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
    result = subprocess.run(
        [
            _POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(setup),
            "-PythonExecutable",
            str(bundled_python),
        ],
        text=True,
        capture_output=True,
        check=False,
        env=isolated_env,
        encoding="utf-8",
        errors="replace",
    )

    output = (result.stdout or "") + (result.stderr or "")
    assert result.returncode != 0
    assert "Python 3.12 was not found" not in output
    assert "PythonExecutable" not in output
    assert "pip upgrade failed" in output or "Project dependency installation failed" in output


@pytest.mark.skipif(_POWERSHELL is None, reason="PowerShell is unavailable")
def test_start_loads_safe_dotenv_values_and_rejects_invalid_or_duplicate_keys(tmp_path):
    project_root = Path(__file__).parents[1]

    def make_project(name: str, dotenv: str) -> Path:
        project = tmp_path / name
        scripts = project / "scripts"
        venv_scripts = project / ".venv" / "Scripts"
        scripts.mkdir(parents=True)
        venv_scripts.mkdir(parents=True)
        shutil.copy2(project_root / "scripts" / "start.ps1", scripts / "start.ps1")
        (project / ".env").write_text(dotenv, encoding="utf-8")
        (venv_scripts / "Activate.ps1").write_text(
            "function python {\n"
            "  Write-Output ('TEST_ALPHA=' + $env:TEST_ALPHA)\n"
            "  Write-Output ('TEST_EMPTY=' + $env:TEST_EMPTY)\n"
            "}\n",
            encoding="utf-8",
        )
        return scripts / "start.ps1"

    valid = make_project(
        "valid",
        "# comment\n\nTEST_ALPHA=left=right\nTEST_EMPTY=\n",
    )
    valid_result = subprocess.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(valid)],
        text=True, capture_output=True, check=False, encoding="utf-8", errors="replace",
    )
    assert valid_result.returncode == 0
    assert valid_result.stdout.splitlines() == ["TEST_ALPHA=left=right", "TEST_EMPTY="]

    invalid = make_project("invalid", "BAD-KEY=value\n")
    invalid_result = subprocess.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(invalid)],
        text=True, capture_output=True, check=False, encoding="utf-8", errors="replace",
    )
    assert invalid_result.returncode != 0
    assert "invalid .env variable name" in (invalid_result.stdout + invalid_result.stderr)

    duplicate = make_project("duplicate", "TEST_ALPHA=one\nTEST_ALPHA=two\n")
    duplicate_result = subprocess.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(duplicate)],
        text=True, capture_output=True, check=False, encoding="utf-8", errors="replace",
    )
    assert duplicate_result.returncode != 0
    assert "duplicate .env variable" in (duplicate_result.stdout + duplicate_result.stderr)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        web_host="127.0.0.1",
        web_port=8765,
        lookback_days=183,
        minimax_api_key=None,
        minimax_base_url="https://api.minimaxi.com/v1",
        minimax_model="test-model",
        minimax_max_concurrency=1,
        whisper_model="small",
        whisper_device="cpu",
    )
