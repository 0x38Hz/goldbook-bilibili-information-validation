# B站黄金UP主历史信号研究工具 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付一个仅监听本机的Web应用，自动处理指定B站UP最近半年公开视频，以本地Whisper转写、MiniMax结构化观点、XAU/USD日线回测，并展示UP排行榜和历史合订本。

**Architecture:** 使用一个Python包承载配置、SQLite仓储、外部适配器、评分服务、后台任务和Flask Web。所有外部系统都位于可注入的适配器边界后，核心评分和状态机不依赖网络，测试用固定字幕、固定JSON和固定行情完成。音轨只存在于任务临时目录，MiniMax密钥只由服务端环境读取。

**Tech Stack:** Python 3.12、Flask、SQLite、httpx、yt-dlp、faster-whisper、pytest、Chart.js、PowerShell。

**Spec:** `docs/superpowers/specs/2026-08-20-bilibili-gold-up-ranking-design.md`

## Global Constraints

- 默认回看183天，只处理B站公开投稿，不处理评论、弹幕、付费、登录受限或删除内容。
- Web默认且无配置时只能监听`127.0.0.1:8765`；首版拒绝公网监听地址。
- MiniMax中国区默认地址为`https://api.minimaxi.com/v1`，默认模型为`MiniMax-M3`。
- MiniMax并发在配置和执行两层都不得超过3。
- MiniMax密钥不得进入源码、数据库、HTTP响应、日志、测试快照或Git。
- 默认Whisper模型为多语言`small`；CUDA使用`float16`，CPU使用`int8`。
- 音轨在成功或失败后都必须删除；不保存视频、封面或UP头像。
- 入场为视频发布后的下一个交易日开盘；退出为第1、5、20个交易日收盘。
- `neutral`、`no_signal`、纯新闻、纯回顾、未复核、排除项、未定价和未到期项不进入排行榜。
- 正式排名至少需要3条已复核且成熟的5日信号。
- 所有新业务函数先写失败测试并观察正确失败，再写最小实现。
- 不执行`git add`、`git commit`、`git push`或PR操作，除非用户另行明确授权。

---

## File Map

```text
.env.example                         环境变量示例，不含密钥
.gitignore                           排除密钥、数据库、音轨和模型缓存
requirements.txt                    运行依赖的精确版本
requirements-dev.txt                pytest与静态检查依赖的精确版本
goldbook/__init__.py                 包版本
goldbook/__main__.py                 python -m goldbook入口
goldbook/config.py                   配置读取和安全校验
goldbook/models.py                   领域枚举与不可变数据类型
goldbook/db.py                       SQLite schema与仓储
goldbook/market.py                   XAU/USD行情端口和Stooq适配器
goldbook/scoring.py                  结果计算和UP聚合
goldbook/minimax.py                  MiniMax请求、JSON校验和证据核对
goldbook/bilibili.py                 yt-dlp投稿发现和音轨下载
goldbook/transcribe.py               faster-whisper适配器
goldbook/jobs.py                     任务状态机和后台执行器
goldbook/web.py                      Flask应用工厂和JSON路由
goldbook/templates/base.html         本地页面框架
goldbook/templates/index.html        UP管理和任务进度
goldbook/templates/leaderboard.html  排行榜
goldbook/templates/creator.html      UP合订本
goldbook/templates/video.html        视频详情和人工复核
goldbook/static/app.css              本地样式
goldbook/static/app.js               页面交互和图表初始化
goldbook/static/chart.umd.min.js     固定版本Chart.js浏览器包
scripts/setup.ps1                    创建Python 3.12环境并安装依赖
scripts/start.ps1                    本机启动
scripts/seed_demo.py                 离线演示数据
tests/                               单元与集成测试
README.md                            安装、使用、安全和数据口径
```

---

### Task 1: Project foundation, configuration, and SQLite repository

**Files:**
- Create: `.gitignore`
- Create: `.env.example`
- Create: `requirements.txt`
- Create: `requirements-dev.txt`
- Create: `goldbook/__init__.py`
- Create: `goldbook/config.py`
- Create: `goldbook/models.py`
- Create: `goldbook/db.py`
- Create: `tests/test_config.py`
- Create: `tests/test_db.py`

**Interfaces:**
- Produces: `Settings.from_env(env: Mapping[str, str]) -> Settings`
- Produces: `Database(path: Path)`, `Database.initialize() -> None`
- Produces: repository methods `upsert_creator`, `upsert_video`, `save_transcript`, `save_analysis`, `replace_prices`, `save_outcome`, `create_job`, `update_job`
- Produces: domain types `Direction`, `ReviewStatus`, `VideoStatus`, `Creator`, `Video`, `TranscriptSegment`, `SignalAnalysis`, `PriceBar`, `Outcome`

- [ ] **Step 1: Write failing configuration tests**

```python
# tests/test_config.py
import pytest
from goldbook.config import Settings


def test_defaults_are_local_and_minimax_concurrency_is_three(tmp_path):
    settings = Settings.from_env({"GOLDBOOK_DATA_DIR": str(tmp_path)})
    assert settings.web_host == "127.0.0.1"
    assert settings.web_port == 8765
    assert settings.lookback_days == 183
    assert settings.minimax_max_concurrency == 3
    assert settings.whisper_model == "small"


def test_rejects_public_binding(tmp_path):
    with pytest.raises(ValueError, match="local-only"):
        Settings.from_env({
            "GOLDBOOK_DATA_DIR": str(tmp_path),
            "WEB_HOST": "0.0.0.0",
        })


def test_rejects_minimax_concurrency_above_three(tmp_path):
    with pytest.raises(ValueError, match="cannot exceed 3"):
        Settings.from_env({
            "GOLDBOOK_DATA_DIR": str(tmp_path),
            "MINIMAX_MAX_CONCURRENCY": "4",
        })
```

- [ ] **Step 2: Run the configuration tests and observe import failure**

Run:

```powershell
python -m pytest tests/test_config.py -q
```

Expected: collection fails because `goldbook.config` does not exist.

- [ ] **Step 3: Implement the minimal immutable settings object**

```python
# goldbook/config.py
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    web_host: str
    web_port: int
    lookback_days: int
    minimax_api_key: str | None
    minimax_base_url: str
    minimax_model: str
    minimax_max_concurrency: int
    whisper_model: str
    whisper_device: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        host = env.get("WEB_HOST", "127.0.0.1")
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Goldbook is local-only and refuses public binding")
        concurrency = int(env.get("MINIMAX_MAX_CONCURRENCY", "3"))
        if concurrency < 1 or concurrency > 3:
            raise ValueError("MINIMAX_MAX_CONCURRENCY cannot exceed 3")
        return cls(
            data_dir=Path(env.get("GOLDBOOK_DATA_DIR", "data")),
            web_host=host,
            web_port=int(env.get("WEB_PORT", "8765")),
            lookback_days=int(env.get("LOOKBACK_DAYS", "183")),
            minimax_api_key=env.get("MINIMAX_API_KEY"),
            minimax_base_url=env.get("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1"),
            minimax_model=env.get("MINIMAX_MODEL", "MiniMax-M3"),
            minimax_max_concurrency=concurrency,
            whisper_model=env.get("WHISPER_MODEL", "small"),
            whisper_device=env.get("WHISPER_DEVICE", "auto"),
        )
```

- [ ] **Step 4: Run the configuration tests to green**

Run: `python -m pytest tests/test_config.py -q`

Expected: `3 passed`.

- [ ] **Step 5: Write failing database lifecycle tests**

```python
# tests/test_db.py
from datetime import datetime, timezone
from goldbook.db import Database
from goldbook.models import Creator, Video


def test_database_is_idempotent_for_creator_and_video(tmp_path):
    db = Database(tmp_path / "goldbook.db")
    db.initialize()
    creator = Creator(uid="42", name="测试UP", space_url="https://space.bilibili.com/42")
    db.upsert_creator(creator)
    db.upsert_creator(creator)
    video = Video(
        bvid="BV1TEST",
        creator_uid="42",
        title="黄金后市",
        published_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        duration_sec=600,
        url="https://www.bilibili.com/video/BV1TEST",
    )
    db.upsert_video(video)
    db.upsert_video(video)
    assert len(db.list_creators()) == 1
    assert len(db.list_videos("42")) == 1


def test_deleting_creator_keeps_shared_prices(tmp_path):
    db = Database(tmp_path / "goldbook.db")
    db.initialize()
    db.upsert_creator(Creator("42", "测试UP", "https://space.bilibili.com/42"))
    db.replace_prices([("2026-08-01", 2400.0, 2420.0, 2390.0, 2410.0)])
    db.delete_creator("42")
    assert db.list_prices()[0][0] == "2026-08-01"
```

- [ ] **Step 6: Run database tests and observe missing repository failure**

Run: `python -m pytest tests/test_db.py -q`

Expected: tests fail because `Database` and domain models are not implemented.

- [ ] **Step 7: Implement domain models and the schema-backed repository**

Create frozen dataclasses and string enums in `goldbook/models.py`. In `goldbook/db.py`, use `sqlite3.connect`, `PRAGMA foreign_keys=ON`, row factories, short transactions, and schema creation for the seven entities in the spec. Use `INSERT ... ON CONFLICT DO UPDATE` for creator and video idempotency. Declare `prices.trade_date` independent of creator foreign keys so creator deletion cannot remove price history.

Required constructor signatures:

```python
@dataclass(frozen=True)
class Creator:
    uid: str
    name: str
    space_url: str


@dataclass(frozen=True)
class Video:
    bvid: str
    creator_uid: str
    title: str
    published_at: datetime
    duration_sec: int
    url: str
```

- [ ] **Step 8: Run foundation tests to green**

Run: `python -m pytest tests/test_config.py tests/test_db.py -q`

Expected: all foundation tests pass.

- [ ] **Step 9: Add dependency manifests and secret exclusions**

Use `python -m pip index versions <package>` to select versions that publish Python 3.12 Windows wheels, then pin the selected versions exactly in the two requirements files. `.gitignore` must contain `.env`, `.venv/`, `data/`, `.pytest_cache/`, `__pycache__/`, `*.pyc`, and model/audio extensions. `.env.example` contains names and safe defaults but an empty `MINIMAX_API_KEY=`.

- [ ] **Step 10: Inspect the task diff checkpoint**

Run:

```powershell
git diff -- .gitignore .env.example requirements.txt requirements-dev.txt goldbook tests/test_config.py tests/test_db.py
python -m pytest tests/test_config.py tests/test_db.py -q
```

Expected: no secret value in the diff and all task tests pass. Do not commit without separate authorization.

---

### Task 2: XAU/USD market data and transparent scoring

**Files:**
- Create: `goldbook/market.py`
- Create: `goldbook/scoring.py`
- Create: `tests/fixtures/xauusd_daily.csv`
- Create: `tests/test_market.py`
- Create: `tests/test_scoring.py`

**Interfaces:**
- Consumes: `PriceBar`, `SignalAnalysis`, `Outcome`, and `Database`
- Produces: `MarketDataSource.fetch(start: date, end: date) -> list[PriceBar]`
- Produces: `StooqMarketDataSource(client: httpx.Client)`
- Produces: `score_signal(analysis: SignalAnalysis, published_at: datetime, bars: Sequence[PriceBar]) -> Outcome`
- Produces: `aggregate_creator(outcomes: Sequence[Outcome]) -> CreatorMetrics`

- [ ] **Step 1: Write failing market parser tests**

```python
# tests/test_market.py
from goldbook.market import parse_stooq_csv


def test_parses_and_sorts_valid_daily_bars():
    csv_text = "Date,Open,High,Low,Close\n2026-08-04,2410,2430,2400,2425\n2026-08-01,2400,2420,2390,2410\n"
    bars = parse_stooq_csv(csv_text)
    assert [bar.trade_date.isoformat() for bar in bars] == ["2026-08-01", "2026-08-04"]
    assert bars[1].open == 2410.0


def test_rejects_duplicate_dates():
    csv_text = "Date,Open,High,Low,Close\n2026-08-01,2400,2420,2390,2410\n2026-08-01,2401,2421,2391,2411\n"
    try:
        parse_stooq_csv(csv_text)
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate date must be rejected")
```

- [ ] **Step 2: Run and observe missing parser failure**

Run: `python -m pytest tests/test_market.py -q`

Expected: import or attribute failure for `parse_stooq_csv`.

- [ ] **Step 3: Implement validated CSV parsing and Stooq retrieval**

Parse only `Date,Open,High,Low,Close`; reject missing, non-positive, duplicated, or `high < low` rows. `StooqMarketDataSource.fetch` calls the configured daily CSV URL with `s=xauusd&i=d`, applies a 20-second timeout, and filters the parsed bars to the requested date range.

- [ ] **Step 4: Run market tests to green**

Run: `python -m pytest tests/test_market.py -q`

Expected: both tests pass.

- [ ] **Step 5: Write failing scoring tests for long, short, maturity, and rank eligibility**

```python
# tests/test_scoring.py
from datetime import date, datetime, timezone
from goldbook.models import Direction, PriceBar, ReviewStatus, SignalAnalysis
from goldbook.scoring import aggregate_creator, score_signal


def bars():
    return [
        PriceBar(date(2026, 8, d), 100 + d, 102 + d, 99 + d, 101 + d)
        for d in range(3, 25)
    ]


def analysis(direction):
    return SignalAnalysis(
        direction=direction,
        strength=4,
        confidence=0.9,
        horizon_text=None,
        target_price=None,
        stop_price=None,
        conditions=(),
        is_retrospective=False,
        is_news_only=False,
        evidence=(),
        summary="测试",
        review_status=ReviewStatus.APPROVED,
    )


def test_uses_next_bar_open_and_flips_bearish_return():
    published = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
    long_result = score_signal(analysis(Direction.BULLISH), published, bars())
    short_result = score_signal(analysis(Direction.BEARISH), published, bars())
    assert long_result.entry_date == date(2026, 8, 3)
    assert long_result.return_5d == -short_result.return_5d


def test_unmatured_twenty_day_result_is_none():
    result = score_signal(
        analysis(Direction.BULLISH),
        datetime(2026, 8, 2, tzinfo=timezone.utc),
        bars()[:10],
    )
    assert result.return_5d is not None
    assert result.return_20d is None


def test_three_matured_calls_are_required_for_formal_rank():
    result = score_signal(
        analysis(Direction.BULLISH),
        datetime(2026, 8, 2, tzinfo=timezone.utc),
        bars(),
    )
    assert aggregate_creator([result, result]).eligible_for_rank is False
    assert aggregate_creator([result, result, result]).eligible_for_rank is True
```

- [ ] **Step 6: Run scoring tests and observe missing implementation failure**

Run: `python -m pytest tests/test_scoring.py -q`

Expected: import or attribute failure for scoring functions.

- [ ] **Step 7: Implement fixed-horizon scoring and aggregation**

Use the first bar strictly after the publication date as entry. Treat horizon index 1 as the first trading-day close after entry, index 5 as the fifth, and index 20 as the twentieth; return `None` when unavailable. Compute signed returns with exact direction multiplication. Aggregate only approved, included, priced values. Compound with `math.prod(1 + value) - 1` in publication order.

- [ ] **Step 8: Run market and scoring tests to green**

Run: `python -m pytest tests/test_market.py tests/test_scoring.py -q`

Expected: all Task 2 tests pass.

- [ ] **Step 9: Inspect the task diff checkpoint**

Run: `git diff -- goldbook/market.py goldbook/scoring.py tests/test_market.py tests/test_scoring.py tests/fixtures/xauusd_daily.csv`

Expected: formulas and maturity rules match the approved spec. Do not commit without separate authorization.

---

### Task 3: MiniMax extraction with evidence validation and a hard concurrency ceiling

**Files:**
- Create: `goldbook/minimax.py`
- Create: `tests/test_minimax.py`

**Interfaces:**
- Consumes: `Settings`, `TranscriptSegment`, and `SignalAnalysis`
- Produces: `MiniMaxClient.analyze(segments: Sequence[TranscriptSegment]) -> SignalAnalysis`
- Produces: `parse_analysis(payload_text: str, segments: Sequence[TranscriptSegment]) -> SignalAnalysis`
- Produces: `run_analysis_batch(client: MiniMaxClient, items: Sequence[tuple[str, Sequence[TranscriptSegment]]]) -> dict[str, SignalAnalysis]`

- [ ] **Step 1: Write failing parsing and evidence tests**

```python
# tests/test_minimax.py
import json
from goldbook.minimax import parse_analysis
from goldbook.models import Direction, ReviewStatus, TranscriptSegment


SEGMENTS = (
    TranscriptSegment(10.0, 18.0, "我认为黄金下周还会继续上涨"),
    TranscriptSegment(18.0, 25.0, "如果跌破两千四就要重新评估"),
)


def test_parses_valid_signal_and_approves_locatable_evidence():
    payload = json.dumps({
        "direction": "bullish",
        "strength": 4,
        "confidence": 0.91,
        "horizon_text": "下周",
        "target_price": None,
        "stop_price": 2400,
        "conditions": ["跌破2400重新评估"],
        "is_retrospective": False,
        "is_news_only": False,
        "evidence": [{
            "start_sec": 10.0,
            "end_sec": 18.0,
            "quote": "黄金下周还会继续上涨"
        }],
        "summary": "明确看多黄金"
    }, ensure_ascii=False)
    result = parse_analysis(payload, SEGMENTS)
    assert result.direction is Direction.BULLISH
    assert result.review_status is ReviewStatus.APPROVED


def test_unlocatable_quote_requires_review():
    payload = json.dumps({
        "direction": "bearish", "strength": 3, "confidence": 0.8,
        "horizon_text": None, "target_price": None, "stop_price": None,
        "conditions": [], "is_retrospective": False, "is_news_only": False,
        "evidence": [{"start_sec": 10, "end_sec": 18, "quote": "字幕中不存在"}],
        "summary": "看空"
    }, ensure_ascii=False)
    result = parse_analysis(payload, SEGMENTS)
    assert result.evidence == ()
    assert result.review_status is ReviewStatus.NEEDS_REVIEW
```

- [ ] **Step 2: Run and observe missing parser failure**

Run: `python -m pytest tests/test_minimax.py -q`

Expected: import or attribute failure.

- [ ] **Step 3: Implement strict parsing and transcript evidence matching**

Extract a JSON object from optional surrounding Markdown, validate enum values, numeric ranges, booleans, and list types, and normalize nullable numeric fields. A quote is valid only if its whitespace-normalized text is a substring of transcript text within the claimed time range. Invalid evidence is dropped and forces `NEEDS_REVIEW`; otherwise a valid directional call with confidence at least `0.70` is initially `APPROVED`.

- [ ] **Step 4: Run parsing tests to green**

Run: `python -m pytest tests/test_minimax.py -q`

Expected: parsing tests pass.

- [ ] **Step 5: Add a failing test that measures real concurrent entries**

```python
def test_batch_never_enters_more_than_three_requests(monkeypatch):
    import threading
    import time
    from goldbook.minimax import MiniMaxClient, run_analysis_batch

    lock = threading.Lock()
    active = 0
    peak = 0

    def fake_request(_segments):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return '{"direction":"no_signal","strength":1,"confidence":0.9,"horizon_text":null,"target_price":null,"stop_price":null,"conditions":[],"is_retrospective":false,"is_news_only":false,"evidence":[],"summary":"无信号"}'

    client = MiniMaxClient.for_test(fake_request, max_concurrency=3)
    items = [(str(index), SEGMENTS) for index in range(10)]
    results = run_analysis_batch(client, items)
    assert len(results) == 10
    assert peak == 3
```

- [ ] **Step 6: Run the concurrency test and observe failure**

Run: `python -m pytest tests/test_minimax.py::test_batch_never_enters_more_than_three_requests -q`

Expected: missing batch implementation or observed peak above 3.

- [ ] **Step 7: Implement the server-side client, retries, and bounded batch executor**

Use `httpx.Client` with bearer authorization and `POST /chat/completions`. Submit only numbered transcript segments and the fixed schema instruction; do not include creator or video metadata. Wrap every request with `threading.BoundedSemaphore(3)` and use `ThreadPoolExecutor(max_workers=3)` for batches. Retry 429 and 5xx up to three attempts with deterministic backoff that can be replaced in tests.

- [ ] **Step 8: Run MiniMax tests to green**

Run: `python -m pytest tests/test_minimax.py -q`

Expected: all MiniMax tests pass and measured peak equals 3.

- [ ] **Step 9: Inspect the task diff and scan for secrets**

Run:

```powershell
git diff -- goldbook/minimax.py tests/test_minimax.py
$matches = rg -l 'sk-cp-|Authorization:\s*Bearer\s+sk-' . -g '!**/.git/**'; if ($LASTEXITCODE -ne 0) { 'secret scan clean' } else { $matches; exit 1 }
```

Expected: secret scan prints `secret scan clean`. Do not commit without separate authorization.

---

### Task 4: Bilibili public-video adapter and local Whisper transcription

**Files:**
- Create: `goldbook/bilibili.py`
- Create: `goldbook/transcribe.py`
- Create: `tests/test_bilibili.py`
- Create: `tests/test_transcribe.py`

**Interfaces:**
- Produces: `BilibiliSource.list_videos(source: str, published_after: datetime) -> list[Video]`
- Produces: `BilibiliSource.download_audio(video: Video, destination: Path) -> Path`
- Produces: `WhisperTranscriber.transcribe(audio_path: Path) -> tuple[TranscriptSegment, ...]`
- Bilibili command execution is injected as `runner(args: list[str]) -> CompletedProcess[str]`
- Whisper model creation is injected as `model_factory(model_name, device, compute_type) -> model`

- [ ] **Step 1: Write failing URL validation and list-filter tests**

```python
# tests/test_bilibili.py
from datetime import datetime, timezone
import pytest
from goldbook.bilibili import BilibiliSource, parse_public_source


def test_accepts_space_uid_and_bvid_but_rejects_other_hosts():
    assert parse_public_source("https://space.bilibili.com/42").kind == "space"
    assert parse_public_source("42").value == "42"
    assert parse_public_source("BV1TEST12345").kind == "video"
    with pytest.raises(ValueError, match="Bilibili"):
        parse_public_source("https://example.com/video/BV1TEST12345")


def test_filters_flat_playlist_by_publication_time():
    rows = [
        {"id": "BVNEW", "title": "新视频", "timestamp": 1785801600, "duration": 600},
        {"id": "BVOLD", "title": "旧视频", "timestamp": 1751328000, "duration": 600},
    ]
    source = BilibiliSource.for_test(rows)
    videos = source.list_videos(
        "https://space.bilibili.com/42",
        datetime(2026, 2, 18, tzinfo=timezone.utc),
    )
    assert [video.bvid for video in videos] == ["BVNEW"]
```

- [ ] **Step 2: Run and observe missing adapter failure**

Run: `python -m pytest tests/test_bilibili.py -q`

Expected: import or attribute failure.

- [ ] **Step 3: Implement source parsing, metadata normalization, and safe yt-dlp invocation**

Use the Python `yt_dlp.YoutubeDL` API rather than shell string composition. Set `quiet`, `no_warnings`, `extract_flat=in_playlist`, and no cookie options. Normalize BVID, title, timestamp, duration, creator UID, and canonical URL. `download_audio` writes to the supplied task directory with an explicit output template and `FFmpegExtractAudio` to WAV; reject destinations outside the resolved configured temporary root.

- [ ] **Step 4: Run Bilibili tests to green**

Run: `python -m pytest tests/test_bilibili.py -q`

Expected: adapter tests pass without network.

- [ ] **Step 5: Write failing lazy-model and timestamp tests**

```python
# tests/test_transcribe.py
from types import SimpleNamespace
from goldbook.transcribe import WhisperTranscriber


def test_transcriber_loads_model_once_and_preserves_timestamps(tmp_path):
    loads = []

    class FakeModel:
        def transcribe(self, *_args, **_kwargs):
            return iter([
                SimpleNamespace(start=1.25, end=3.5, text=" 黄金继续上涨 "),
            ]), SimpleNamespace(language="zh")

    def factory(model_name, device, compute_type):
        loads.append((model_name, device, compute_type))
        return FakeModel()

    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF-test")
    transcriber = WhisperTranscriber("small", "cpu", factory)
    first = transcriber.transcribe(audio)
    second = transcriber.transcribe(audio)
    assert len(loads) == 1
    assert first[0].start_sec == 1.25
    assert first[0].text == "黄金继续上涨"
    assert second == first
```

- [ ] **Step 6: Run and observe missing transcriber failure**

Run: `python -m pytest tests/test_transcribe.py -q`

Expected: import or attribute failure.

- [ ] **Step 7: Implement lazy faster-whisper adapter**

Select `cuda/float16` only when CUDA is explicitly available through CTranslate2; otherwise select `cpu/int8`. Call `transcribe(language="zh", vad_filter=True, beam_size=5)` and materialize the generator before returning immutable segments. Import `faster_whisper` inside the default factory so unit tests do not download a model.

- [ ] **Step 8: Run external-adapter tests to green**

Run: `python -m pytest tests/test_bilibili.py tests/test_transcribe.py -q`

Expected: all tests pass with no external network.

- [ ] **Step 9: Inspect task diff checkpoint**

Run: `git diff -- goldbook/bilibili.py goldbook/transcribe.py tests/test_bilibili.py tests/test_transcribe.py`

Expected: no cookies, login bypass, proxy rotation, or persistent-media path exists. Do not commit without separate authorization.

---

### Task 5: Resumable job orchestration and guaranteed temporary-file cleanup

**Files:**
- Create: `goldbook/jobs.py`
- Create: `tests/test_jobs.py`
- Modify: `goldbook/db.py`

**Interfaces:**
- Consumes: `BilibiliSource`, `WhisperTranscriber`, `MiniMaxClient`, `MarketDataSource`, `Database`
- Produces: `PipelineService.sync_creator(source: str, now: datetime) -> str`
- Produces: `PipelineService.process_video(bvid: str) -> None`
- Produces: `BackgroundRunner.start()`, `BackgroundRunner.stop()`, `BackgroundRunner.retry_failed(job_id: str)`

- [ ] **Step 1: Write a failing end-to-end service test with fake ports**

```python
# tests/test_jobs.py
from datetime import datetime, timezone
from pathlib import Path
from goldbook.jobs import PipelineService


def test_video_pipeline_is_idempotent_and_deletes_audio(tmp_path, fake_pipeline_ports):
    service = PipelineService(
        db=fake_pipeline_ports.db,
        source=fake_pipeline_ports.source,
        transcriber=fake_pipeline_ports.transcriber,
        analyzer=fake_pipeline_ports.analyzer,
        market=fake_pipeline_ports.market,
        temp_root=tmp_path / "tmp",
    )
    service.sync_creator("https://space.bilibili.com/42", datetime(2026, 8, 20, tzinfo=timezone.utc))
    service.sync_creator("https://space.bilibili.com/42", datetime(2026, 8, 20, tzinfo=timezone.utc))
    assert fake_pipeline_ports.source.download_count == 1
    assert fake_pipeline_ports.analyzer.call_count == 1
    assert list((tmp_path / "tmp").glob("**/*.wav")) == []
    assert fake_pipeline_ports.db.get_video("BVNEW").status == "complete"


def test_audio_is_deleted_when_transcription_fails(tmp_path, fake_pipeline_ports):
    fake_pipeline_ports.transcriber.error = RuntimeError("decode failed")
    service = PipelineService(
        db=fake_pipeline_ports.db,
        source=fake_pipeline_ports.source,
        transcriber=fake_pipeline_ports.transcriber,
        analyzer=fake_pipeline_ports.analyzer,
        market=fake_pipeline_ports.market,
        temp_root=tmp_path / "tmp",
    )
    service.sync_creator("42", datetime(2026, 8, 20, tzinfo=timezone.utc))
    assert list((tmp_path / "tmp").glob("**/*.wav")) == []
    assert fake_pipeline_ports.db.get_video("BVNEW").status == "failed"
```

- [ ] **Step 2: Run and observe missing orchestrator failure**

Run: `python -m pytest tests/test_jobs.py -q`

Expected: import or fixture failure before orchestration exists.

- [ ] **Step 3: Build explicit fakes in `tests/conftest.py`**

The fixture returns real temporary SQLite plus deterministic fake source, transcriber, analyzer, and market adapters. Fakes expose only call counters and configured outputs; they do not reproduce internal production logic.

- [ ] **Step 4: Implement the per-video state machine and cleanup**

Statuses are `pending -> downloading -> transcribing -> analyzing -> pricing -> complete`, with any exception moving to `failed` and storing an error type plus a 300-character sanitized message. Create a unique directory with `tempfile.mkdtemp(dir=temp_root)` and remove it in `finally` using an exact resolved path check. Skip completed videos whose transcript hash and model name match the current configuration.

- [ ] **Step 5: Run pipeline tests to green**

Run: `python -m pytest tests/test_jobs.py -q`

Expected: idempotency and cleanup tests pass.

- [ ] **Step 6: Add a failing recovery test**

```python
def test_startup_recovers_interrupted_jobs_as_pending(tmp_path, fake_pipeline_ports):
    job_id = fake_pipeline_ports.db.create_job("sync_creator", "42")
    fake_pipeline_ports.db.update_job(job_id, status="running", stage="transcribing")
    recovered = fake_pipeline_ports.db.recover_interrupted_jobs()
    assert recovered == 1
    assert fake_pipeline_ports.db.get_job(job_id).status == "pending"
```

- [ ] **Step 7: Run recovery test and observe failure**

Run: `python -m pytest tests/test_jobs.py::test_startup_recovers_interrupted_jobs_as_pending -q`

Expected: missing recovery method or wrong status.

- [ ] **Step 8: Implement job recovery and one-process background runner**

On startup, atomically change all `running` jobs to `pending` with stage `recovered`. Use one worker thread for downloads/transcription and delegate only MiniMax batches to the already bounded three-worker executor. Stop uses an event and joins the worker with a bounded timeout.

- [ ] **Step 9: Run all orchestration tests to green**

Run: `python -m pytest tests/test_jobs.py -q`

Expected: all job tests pass.

- [ ] **Step 10: Inspect task diff checkpoint**

Run: `git diff -- goldbook/jobs.py goldbook/db.py tests/test_jobs.py tests/conftest.py`

Expected: every media path cleanup is in `finally`, and resume state is persisted. Do not commit without separate authorization.

---

### Task 6: Local Flask dashboard, leaderboard, compilation, and review UI

**Files:**
- Create: `goldbook/web.py`
- Create: `goldbook/templates/base.html`
- Create: `goldbook/templates/index.html`
- Create: `goldbook/templates/leaderboard.html`
- Create: `goldbook/templates/creator.html`
- Create: `goldbook/templates/video.html`
- Create: `goldbook/static/app.css`
- Create: `goldbook/static/app.js`
- Create: `goldbook/static/chart.umd.min.js`
- Create: `tests/test_web.py`

**Interfaces:**
- Consumes: `Settings`, `Database`, `PipelineService`, and scoring aggregates
- Produces: `create_app(settings: Settings, db: Database, pipeline: PipelineService) -> Flask`
- Produces routes: `GET /`, `POST /api/creators`, `POST /api/creators/<uid>/sync`, `DELETE /api/creators/<uid>`, `GET /leaderboard`, `GET /creators/<uid>`, `GET|POST /videos/<bvid>`

- [ ] **Step 1: Write failing route and secret-isolation tests**

```python
# tests/test_web.py
def test_index_and_leaderboard_render(client):
    assert client.get("/").status_code == 200
    response = client.get("/leaderboard")
    assert response.status_code == 200
    assert "历史信号统计" in response.get_data(as_text=True)


def test_api_key_is_not_exposed_anywhere(client, app_settings):
    secret = app_settings.minimax_api_key
    for path in ["/", "/leaderboard", "/api/status"]:
        response = client.get(path)
        assert secret not in response.get_data(as_text=True)


def test_create_creator_accepts_only_bilibili_source(client):
    bad = client.post("/api/creators", json={"source": "https://example.com/u/1"})
    assert bad.status_code == 400
    good = client.post("/api/creators", json={"source": "https://space.bilibili.com/42"})
    assert good.status_code == 202
```

- [ ] **Step 2: Run and observe missing app failure**

Run: `python -m pytest tests/test_web.py -q`

Expected: missing app factory or routes.

- [ ] **Step 3: Implement app factory and JSON error envelope**

Every JSON response uses `{"ok": true, "data": ...}` or `{"ok": false, "error": {"code": ..., "message": ...}}`. Never serialize `Settings`; `/api/status` returns only version, database readiness, model name, and whether a key is configured as a boolean.

- [ ] **Step 4: Implement the four server-rendered screens**

Use semantic HTML tables and local CSS. The leaderboard defaults to five-day average signed return and separates eligible from insufficient-sample creators. Creator pages show counts for all dispositions, not only winners. Video pages show timestamped transcript segments, short evidence, 1/5/20-day results, an exclusion checkbox, editable structured fields, and the fixed research disclaimer.

- [ ] **Step 5: Add failing review-update test**

```python
def test_manual_review_recomputes_outcome(client, seeded_video):
    response = client.post(
        f"/videos/{seeded_video.bvid}",
        data={
            "direction": "bearish",
            "strength": "4",
            "confidence": "1.0",
            "summary": "人工确认看空",
            "excluded": "",
            "exclusion_reason": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    detail = client.get(f"/videos/{seeded_video.bvid}").get_data(as_text=True)
    assert "人工确认看空" in detail
    assert "已人工复核" in detail
```

- [ ] **Step 6: Run review test and observe failure**

Run: `python -m pytest tests/test_web.py::test_manual_review_recomputes_outcome -q`

Expected: update route missing or outcome unchanged.

- [ ] **Step 7: Implement audited manual review and outcome recomputation**

Validate direction, strength, confidence, exclusion reason, and CSRF token stored in the local session. Save before/after JSON in the analysis revision table, mark `APPROVED`, recompute outcomes from cached prices, and redirect with HTTP 303.

- [ ] **Step 8: Vendor Chart.js and add progressive interaction**

Download the exact chosen Chart.js UMD release into `goldbook/static/chart.umd.min.js`; record its version and upstream URL in README. `app.js` polls job status, submits creator actions with confirmation, and initializes a price chart only when chart data is present. Core tables and forms must still work if JavaScript is disabled.

- [ ] **Step 9: Run all web tests to green**

Run: `python -m pytest tests/test_web.py -q`

Expected: all route, secret, review, and deletion tests pass.

- [ ] **Step 10: Inspect task diff checkpoint**

Run:

```powershell
git diff -- goldbook/web.py goldbook/templates goldbook/static tests/test_web.py
rg -n '稳赚|专家|骗子|反向指标|立即买入|立即卖出' goldbook/templates goldbook/static
```

Expected: the prohibited-copy scan returns no matches. Do not commit without separate authorization.

---

### Task 7: Entrypoint, offline demo, local setup, and operator documentation

**Files:**
- Create: `goldbook/__main__.py`
- Create: `scripts/setup.ps1`
- Create: `scripts/start.ps1`
- Create: `scripts/seed_demo.py`
- Create: `README.md`
- Create: `tests/test_cli.py`
- Create: `tests/test_demo.py`

**Interfaces:**
- Produces: `python -m goldbook serve`
- Produces: `python -m goldbook refresh-prices`
- Produces: `python -m goldbook seed-demo`
- Produces: PowerShell commands `.\scripts\setup.ps1` and `.\scripts\start.ps1`

- [ ] **Step 1: Write failing CLI safety tests**

```python
# tests/test_cli.py
from goldbook.__main__ import build_parser


def test_cli_has_only_local_research_commands():
    parser = build_parser()
    assert parser.parse_args(["serve"]).command == "serve"
    assert parser.parse_args(["refresh-prices"]).command == "refresh-prices"
    assert parser.parse_args(["seed-demo"]).command == "seed-demo"


def test_cli_does_not_offer_public_deploy_or_trade_commands():
    help_text = build_parser().format_help().lower()
    assert "deploy" not in help_text
    assert "trade" not in help_text
```

- [ ] **Step 2: Run and observe missing CLI failure**

Run: `python -m pytest tests/test_cli.py -q`

Expected: missing parser.

- [ ] **Step 3: Implement the application composition root**

Load `.env` with a small local parser or `python-dotenv`, construct settings, initialize SQLite, recover interrupted jobs, construct adapters, start the background runner, and serve Flask with `debug=False` and `use_reloader=False`. Register `atexit` cleanup for the runner.

- [ ] **Step 4: Write failing deterministic demo test**

```python
# tests/test_demo.py
def test_demo_seed_creates_ranked_and_insufficient_sample_creators(tmp_path):
    from goldbook.db import Database
    from scripts.seed_demo import seed_demo

    db = Database(tmp_path / "demo.db")
    db.initialize()
    seed_demo(db)
    metrics = db.list_creator_metrics()
    assert any(item.eligible_for_rank for item in metrics)
    assert any(not item.eligible_for_rank for item in metrics)
```

- [ ] **Step 5: Run demo test and observe failure**

Run: `python -m pytest tests/test_demo.py -q`

Expected: missing seed function.

- [ ] **Step 6: Implement offline demo fixtures through public repository interfaces**

Seed two fictional creators, six fictional videos, approved bullish/bearish/no-signal analyses, and thirty deterministic XAU/USD bars. Do not use a real UP name, avatar, quote, title, or BVID. Call the same scoring service used in production.

- [ ] **Step 7: Implement Windows setup and start scripts**

`setup.ps1` locates Python 3.12 in this order: active Python if version 3.12, Codex bundled Python path if present, then `py -3.12`; it creates `.venv`, upgrades pip, and installs both requirement files. It must stop with a clear message when no Python 3.12 exists. `start.ps1` verifies `.env` exists, activates `.venv`, and runs `python -m goldbook serve` without opening a public firewall rule.

- [ ] **Step 8: Write README from the approved operating contract**

README sections are: purpose and non-advice boundary; Windows prerequisites; setup; creating and rotating the MiniMax key; first model download and expected disk use; adding an UP/BV; task states; scoring formulas; manual review; backup and deletion; troubleshooting Bstation/Whisper/MiniMax/Stooq; local-only security; test commands; Chart.js provenance and license.

- [ ] **Step 9: Run CLI and demo tests to green**

Run: `python -m pytest tests/test_cli.py tests/test_demo.py -q`

Expected: all tests pass.

- [ ] **Step 10: Inspect documentation and script checkpoint**

Run:

```powershell
git diff -- goldbook/__main__.py scripts README.md tests/test_cli.py tests/test_demo.py
rg -n 'MINIMAX_API_KEY=.+|sk-cp-' .env.example README.md scripts goldbook tests
```

Expected: no populated key or key prefix exists. Do not commit without separate authorization.

---

### Task 8: Full verification, local live probes, and delivery

**Files:**
- Modify only files proven necessary by failing verification tests
- Create: `docs/verification.md`

**Interfaces:**
- Verifies every interface in Tasks 1–7
- Produces reproducible verification evidence without saving secrets or copyrighted media

- [ ] **Step 1: Run the complete automated test suite**

Run:

```powershell
python -m pytest -q
```

Expected: zero failures, zero errors.

- [ ] **Step 2: Run static compilation and secret scans**

Run:

```powershell
python -m compileall -q goldbook scripts tests
$secretHits = rg -l 'sk-cp-|MINIMAX_API_KEY=..+' . -g '!**/.git/**' -g '!.env'; if ($LASTEXITCODE -eq 0) { $secretHits; exit 1 } else { 'secret scan clean' }
$mediaHits = rg --files data -g '*.wav' -g '*.mp3' -g '*.m4a' -g '*.mp4'; if ($LASTEXITCODE -eq 0) { $mediaHits; exit 1 } else { 'media cleanup clean' }
```

Expected: compile exit 0, secret scan clean, media cleanup clean.

- [ ] **Step 3: Verify the offline demo in a real local process**

Run:

```powershell
python -m goldbook seed-demo
python -m goldbook serve
```

In a second shell:

```powershell
Invoke-WebRequest http://127.0.0.1:8765/ -UseBasicParsing
Invoke-WebRequest http://127.0.0.1:8765/leaderboard -UseBasicParsing
```

Expected: both responses are HTTP 200; the process binds only to `127.0.0.1`.

- [ ] **Step 4: Verify MiniMax connectivity without logging the key or a real transcript**

With the key loaded from `.env`, call `MiniMaxClient.analyze` on two fictional Chinese transcript segments. Capture only HTTP status, model name, parsed direction, and latency in `docs/verification.md`; never capture headers or the raw response.

Expected: one parsed `SignalAnalysis`, no retry, and no key in logs or database.

- [ ] **Step 5: Verify model acquisition and local transcription with generated audio**

Generate a short synthetic Mandarin WAV through an installed local TTS only if available; otherwise use a CC0 test WAV checked into `tests/fixtures` with attribution. Run the configured Whisper adapter and record model, device, compute type, elapsed time, and non-empty segment count. Delete the WAV after the check when it is generated.

Expected: at least one timestamped segment and no residual task audio.

- [ ] **Step 6: Verify public Bilibili metadata extraction without retaining media**

Use one user-supplied BV or UP URL when available. If none is supplied, run `yt-dlp --simulate --dump-single-json` against a public Bilibili test URL documented in the current session and record only extractor status, BVID, publication timestamp presence, and duration presence. Do not write or retain audio during this metadata-only probe.

Expected: extractor returns public metadata or a clearly documented platform/rate-limit failure that the Web retry path surfaces.

- [ ] **Step 7: Verify Stooq XAU/USD live data and scoring dates**

Fetch the last forty calendar days, assert at least fifteen positive OHLC bars, persist them, and score a fictional approved bullish signal whose publication date is before the retrieved window end. Record the selected entry and exit dates, not the full provider response.

Expected: entry is strictly after publication and any available horizons match trading-day indexes.

- [ ] **Step 8: Review the spec requirement by requirement**

Create a checklist in `docs/verification.md` covering every bullet in design sections 2, 4, 5, 6, 7, 8, and 9. For each requirement, cite its test name or manual probe evidence. Mark genuine gaps as gaps and fix them only through a new failing test.

- [ ] **Step 9: Run fresh final verification after all fixes**

Run:

```powershell
python -m pytest -q
python -m compileall -q goldbook scripts tests
git status --short
git diff --stat
```

Expected: zero test failures, compile exit 0, and only intended project files are present.

- [ ] **Step 10: Deliver local run instructions and known live limitations**

Report exact test counts, live-probe outcomes, model download status, MiniMax connectivity status, Stooq status, Bilibili metadata status, and any remaining platform variability. Remind the user to rotate the MiniMax key because it was shared in chat. Do not state completion until Step 9 fresh evidence is read.

---

## Plan Self-Review Results

- Spec coverage: every approved subsystem and safety boundary maps to a task and test.
- Type consistency: domain types originate in Task 1; later tasks consume the same names and signatures.
- External isolation: all network/model dependencies have fake-backed tests; only Task 8 performs bounded live probes.
- Secret handling: real key is excluded from all planned files and scans ignore only the local `.env` value itself.
- Scope: no public deployment, multi-user system, live trade signal, comment/danmaku ingestion, or secondary asset is introduced.
- Git safety: task checkpoints inspect diffs but do not commit without explicit authorization.
