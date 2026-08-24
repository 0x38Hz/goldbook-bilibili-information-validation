# 逐条黄金观点与严格时间对齐 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有“每视频一个方向、固定 1/5/20 日”的分析升级为每视频多观点、M3 动态周期识别、严格发布后行情验证和按视频等权的 UP 能力排行。

**Architecture:** 保留现有字幕、视频、任务和旧分析表作为兼容层，新增逐条观点领域模型、SQLite 持久化、纯函数时间窗口解析与确定性评价器。MiniMax-M3 只读取标题、发布时间和字幕来生成结构化观点；行情评价在独立模块中完成，缓存字幕重分析和行情重算分别幂等执行。

**Tech Stack:** Python 3.12、dataclasses、SQLite、Flask、httpx、MiniMax-M3、pytest、Chart.js、PowerShell。

**Spec:** `docs/superpowers/specs/2026-08-21-claim-level-forecast-evaluation-design.md`

## Global Constraints

- 一个视频可以有零个、一个或多个观点，每个观点独立保存证据、点位、条件和周期。
- MiniMax 默认模型保持 `MiniMax-M3`，最大并发在配置、客户端和批处理三层均不得超过 3。
- M3 抽取阶段只能接收标题、带时区发布时间和字幕，不能接收未来视频、未来行情或事后结果。
- 视频发布当日的日线不得用于验证；第一个观察点是发布后的第一根完整交易日 K 线。
- “短期、中期、长期”必须转换成具体交易日点估计和区间；完全无法推断时不得默认 20 天。
- 无周期观点仅在下一条同 UP、同品种、可执行预测发布前有效；明确周期观点不因普通日更自动截断。
- 日内观点在只有日线时标记 `unresolved_intraday_data`，不能伪装成命中或失败。
- 只用 XAU/USD 现货评价 `XAU_USD_SPOT`；COMEX、沪金和未知品种不得混算。
- 结构和字幕证据校验通过的观点自动统计，不以 `ReviewStatus.APPROVED` 为门槛；人工操作只生成可审计修订。
- LLM 不判断输赢；命中、接近、时间顺序、收益和聚合全部由确定性程序计算。
- `partial_near` 的距离阈值固定为目标点位的 0.5%；“站稳”固定为连续两个完整交易日收盘在目标同侧。
- 排行先在每个视频内等权平均成熟观点，再对视频等权平均；至少 3 个成熟可评价视频才显示名次。
- 既有 99 条字幕直接重分析，不重新下载媒体、不重新运行 Whisper、不保留原始 MiniMax 响应。
- 所有业务变更先观察目标测试失败，再写最小实现；每项任务结束运行定向测试和完整回归。

---

## File Map

```text
goldbook/models.py                 新增观点、观点事件、评价与指标不可变类型
goldbook/db.py                     新表迁移、观点/评价事务仓储和幂等查询
goldbook/minimax.py                M3 多观点 prompt、解析、证据校验和三并发批处理
goldbook/claim_time.py             发布后完整交易日窗口、周期和替代关系解析
goldbook/claim_evaluation.py       点位、方向、站稳、区间和顺序的确定性评价
goldbook/claim_pipeline.py         缓存字幕重分析、进度、失败恢复与评价重算
goldbook/claim_metrics.py          视频等权与 UP 等权指标
goldbook/__main__.py               reanalyse-claims 命令和 refresh-prices 联动重算
goldbook/web.py                    新观点查询、纠错路由和页面上下文
goldbook/templates/creator.html    UP 视频级观点覆盖和结论列表
goldbook/templates/video.html      每条观点预测/实际对照卡和图表
goldbook/templates/leaderboard.html 新版能力指标和覆盖率
goldbook/static/app.css            对照卡、结论徽标和响应式布局
goldbook/static/app.js             观点目标线、截止和命中标记
scripts/seed_demo.py               离线多观点演示数据
README.md                          新口径、命令、限制和数据说明
docs/verification.md               新测试与两个真实 UP 的回填结果
tests/test_claim_db.py             观点/评价迁移与仓储
tests/test_claim_extraction.py     M3 schema、证据、缓存身份和并发
tests/test_claim_time.py           时间边界与替代规则
tests/test_claim_evaluation.py     确定性评价规则
tests/test_claim_pipeline.py       缓存字幕重分析和重算编排
tests/test_claim_metrics.py        视频等权排行榜
tests/test_claim_web.py            页面、纠错、CSRF 和图表数据
tests/test_cli.py                  新 CLI 与行情刷新联动
tests/test_demo.py                 新演示数据幂等性
```

---

### Task 1: Claim domain model and SQLite repository

**Files:**
- Modify: `goldbook/models.py`
- Modify: `goldbook/db.py`
- Create: `tests/test_claim_db.py`

**Interfaces:**
- Produces: `Instrument`, `ClaimType`, `HorizonSource`, `ClaimStatus`, `EvaluationVerdict`
- Produces: `ClaimLeg`, `ForecastClaim`, `ClaimEvaluation`
- Produces: `Database.replace_forecast_claims(bvid: str, analysis_revision: int, claims: Sequence[ForecastClaim]) -> None`
- Produces: `Database.list_forecast_claims(bvid: str, *, latest_only: bool = True) -> list[ForecastClaim]`
- Produces: `Database.list_creator_forecast_claims(creator_uid: str) -> list[ForecastClaim]`
- Produces: `Database.save_claim_evaluation(value: ClaimEvaluation) -> None`
- Produces: `Database.get_claim_evaluation(claim_id: str) -> ClaimEvaluation | None`
- Produces: `Database.list_creator_claim_evaluations(creator_uid: str) -> list[ClaimEvaluation]`
- Produces: `Database.has_claim_extraction(bvid: str, transcript_hash: str, model_name: str, prompt_version: str) -> bool`
- Produces: `Database.list_price_bars() -> list[PriceBar]`
- Produces: `Database.delete_claim_evaluations_except(live_ids: set[str]) -> int`

- [ ] **Step 1: Write failing model and repository tests**

```python
# tests/test_claim_db.py
from datetime import date, datetime, timezone
from goldbook.models import (
    ClaimEvaluation, ClaimLeg, ClaimStatus, ClaimType, EvaluationVerdict,
    ForecastClaim, HorizonSource, Instrument,
)

def claim() -> ForecastClaim:
    return ForecastClaim(
        claim_id="BV1:1:0", bvid="BV1", analysis_revision=1, claim_index=0,
        instrument=Instrument.XAU_USD_SPOT, claim_type=ClaimType.SEQUENCE,
        direction=Direction.BULLISH,
        legs=(ClaimLeg("<=", 4650.0, None), ClaimLeg(">=", 4700.0, None)),
        condition_text="先回踩4650再看4700", horizon_text="短期",
        horizon_source=HorizonSource.CONTEXT_INFERRED,
        horizon_min_trading_days=1, horizon_max_trading_days=3,
        horizon_point_trading_days=2, deadline_at=None,
        time_confidence=0.81, confidence=0.92,
        evidence=({"start_sec": 3.0, "end_sec": 8.0, "quote": "先回踩4650再看4700"},),
        supersedes_claim_id=None, status=ClaimStatus.AUTO_VALIDATED,
        model_name="MiniMax-M3", prompt_version="claims-v1", transcript_hash="hash-1",
    )

def test_claims_and_evaluations_round_trip(database):
    database.replace_forecast_claims("BV1", 1, [claim()])
    assert database.list_forecast_claims("BV1") == [claim()]
    result = ClaimEvaluation(
        claim_id="BV1:1:0", evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        window_start=date(2026, 8, 4), window_end=date(2026, 8, 6),
        entry_price=4660.0, observed_min=4648.0, observed_max=4702.0,
        final_close=4698.0, closest_price=4702.0, closest_date=date(2026, 8, 5),
        distance_pct=0.0, first_hit_date=date(2026, 8, 5),
        verdict=EvaluationVerdict.HIT, mature=True, reason="sequence satisfied",
    )
    database.save_claim_evaluation(result)
    assert database.get_claim_evaluation(claim().claim_id) == result
    assert database.has_claim_extraction("BV1", "hash-1", "MiniMax-M3", "claims-v1")
```

- [ ] **Step 2: Run the new repository tests and observe the missing-type failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_claim_db.py -q`

Expected: collection fails because `ForecastClaim` and `ClaimEvaluation` do not exist.

- [ ] **Step 3: Add the immutable domain types**

```python
# goldbook/models.py
class Instrument(str, Enum):
    XAU_USD_SPOT = "xau_usd_spot"
    COMEX_GC = "comex_gc"
    SHFE_AU = "shfe_au"
    UNKNOWN = "unknown"

class ClaimType(str, Enum):
    DIRECTIONAL_MOVE = "directional_move"
    TARGET_TOUCH = "target_touch"
    CROSS_ABOVE = "cross_above"
    CROSS_BELOW = "cross_below"
    HOLD_ABOVE = "hold_above"
    HOLD_BELOW = "hold_below"
    RANGE = "range"
    VOLATILITY = "volatility"
    SEQUENCE = "sequence"

class HorizonSource(str, Enum):
    EXPLICIT_EXACT = "explicit_exact"
    EXPLICIT_RELATIVE = "explicit_relative"
    CONTEXT_INFERRED = "context_inferred"
    UNKNOWN = "unknown"

class ClaimStatus(str, Enum):
    AUTO_VALIDATED = "auto_validated"
    UNRESOLVED = "unresolved"
    EXCLUDED = "excluded"
    HUMAN_CORRECTED = "human_corrected"

class EvaluationVerdict(str, Enum):
    HIT = "hit"
    PARTIAL_NEAR = "partial_near"
    MISS = "miss"
    UNRESOLVED = "unresolved"
    SUPERSEDED = "superseded"
    EXCLUDED = "excluded"

@dataclass(frozen=True)
class ClaimLeg:
    operator: str
    level_low: float | None
    level_high: float | None

@dataclass(frozen=True)
class ForecastClaim:
    claim_id: str
    bvid: str
    analysis_revision: int
    claim_index: int
    instrument: Instrument
    claim_type: ClaimType
    direction: Direction | None
    legs: tuple[ClaimLeg, ...]
    condition_text: str
    horizon_text: str | None
    horizon_source: HorizonSource
    horizon_min_trading_days: int | None
    horizon_max_trading_days: int | None
    horizon_point_trading_days: int | None
    deadline_at: datetime | None
    time_confidence: float
    confidence: float
    evidence: tuple[dict[str, object], ...]
    supersedes_claim_id: str | None
    status: ClaimStatus
    model_name: str
    prompt_version: str
    transcript_hash: str

@dataclass(frozen=True)
class ClaimEvaluation:
    claim_id: str
    evaluated_at: datetime
    window_start: date | None
    window_end: date | None
    entry_price: float | None
    observed_min: float | None
    observed_max: float | None
    final_close: float | None
    closest_price: float | None
    closest_date: date | None
    distance_pct: float | None
    first_hit_date: date | None
    verdict: EvaluationVerdict
    mature: bool
    reason: str
```

- [ ] **Step 4: Add schema migrations and transactional serialization**

Extend `analyses` with nullable `model_name` and `prompt_version` columns so an empty `no_claim` extraction is cacheable. Create the two claim tables and serialize enums/JSON inside one short transaction per write:

```sql
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
    reason TEXT NOT NULL
);
```

Add `model_name: str | None = None` and `prompt_version: str | None = None` to the end of `SignalAnalysis`, persist them in `save_analysis`, and make `has_claim_extraction` query the latest `analyses` identity rather than requiring at least one claim row.

- [ ] **Step 5: Add migration, replacement, cascade and latest-revision assertions**

```python
def test_replacing_one_revision_is_idempotent_and_latest_only(database):
    first = claim()
    database.replace_forecast_claims("BV1", 1, [first])
    database.replace_forecast_claims("BV1", 1, [first])
    assert database.list_forecast_claims("BV1") == [first]

def test_deleting_video_cascades_claims_and_evaluations(database):
    database.replace_forecast_claims("BV1", 1, [claim()])
    database.delete_creator("42")
    assert database.list_forecast_claims("BV1", latest_only=False) == []
```

- [ ] **Step 6: Run focused and legacy database tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_claim_db.py tests/test_db.py -q`

Expected: all tests pass and an existing database initializes without dropping legacy analyses/outcomes.

- [ ] **Step 7: Commit the domain and repository unit**

```powershell
git add goldbook/models.py goldbook/db.py tests/test_claim_db.py
git commit -m "feat: persist claim-level forecasts"
```

---

### Task 2: MiniMax-M3 multi-claim extraction

**Files:**
- Modify: `goldbook/minimax.py`
- Create: `tests/test_claim_extraction.py`

**Interfaces:**
- Consumes: `ForecastClaim`, `ClaimLeg`, `Video`, `TranscriptSegment`
- Produces: `ClaimExtraction(summary: str, claims: tuple[ForecastClaim, ...])`
- Produces: `ClaimExtractionFailure(bvid: str, reason: str)` with a fixed non-secret reason
- Produces: `MiniMaxClient.analyze_claims(video: Video, segments: Sequence[TranscriptSegment], *, revision: int, transcript_hash: str) -> ClaimExtraction`
- Produces: `parse_claim_extraction(payload_text: str, video: Video, segments: Sequence[TranscriptSegment], *, revision: int, transcript_hash: str, model_name: str) -> ClaimExtraction`
- Produces: `run_claim_extraction_batch(client, items) -> dict[str, ClaimExtraction | ClaimExtractionFailure]`
- Produces: `CLAIM_PROMPT_VERSION = "claims-v1"`

- [ ] **Step 1: Write a failing multi-claim parser test**

```python
def test_parses_multiple_point_and_horizon_claims_with_locatable_evidence(video, segments):
    payload = json.dumps({
        "summary": "短线回踩后看涨，中期目标4700",
        "claims": [
            {
                "instrument": "xau_usd_spot", "claim_type": "sequence", "direction": "bullish",
                "legs": [{"operator": "<=", "level_low": 4650, "level_high": None},
                         {"operator": ">=", "level_low": 4700, "level_high": None}],
                "condition_text": "先回踩4650再看4700", "horizon_text": "短期",
                "horizon_source": "context_inferred", "horizon_min_trading_days": 1,
                "horizon_max_trading_days": 3, "horizon_point_trading_days": 2,
                "deadline_at": None, "time_confidence": .8, "confidence": .9,
                "evidence": [{"start_sec": 0, "end_sec": 5, "quote": "先回踩4650再看4700"}],
                "status": "auto_validated"
            }
        ]
    }, ensure_ascii=False)
    extraction = parse_claim_extraction(payload, video, segments, revision=1,
        transcript_hash="hash", model_name="MiniMax-M3")
    assert extraction.claims[0].horizon_max_trading_days == 3
    assert extraction.claims[0].legs[1].level_low == 4700
```

- [ ] **Step 2: Run it and observe the missing parser failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_claim_extraction.py::test_parses_multiple_point_and_horizon_claims_with_locatable_evidence -q`

Expected: import fails for `parse_claim_extraction`.

- [ ] **Step 3: Define the exact M3 schema and no-lookahead prompt**

Add a system prompt that requires one JSON object with `summary` and `claims`, the exact enum values from Task 1, numeric horizon min/max/point fields, typed legs, and timestamped evidence. The user message must contain only `title`, `published_at`, and numbered transcript segments. Add an assertion test on the fake HTTP request body:

```python
assert "future_prices" not in request_body
assert video.published_at.isoformat() in request_body
assert "短期必须换算" in request_body
assert request_body["model"] == "MiniMax-M3"
```

Define the returned wrapper and parse error explicitly:

```python
CLAIM_PROMPT_VERSION = "claims-v1"

@dataclass(frozen=True)
class ClaimExtraction:
    summary: str
    claims: tuple[ForecastClaim, ...]

class ClaimExtractionError(ValueError):
    pass

@dataclass(frozen=True)
class ClaimExtractionFailure:
    bvid: str
    reason: str
```

- [ ] **Step 4: Implement strict parsing and evidence validation**

Reject unknown keys, invalid enums, bool-as-number values, negative or reversed horizon ranges, missing sequence legs, non-finite levels, out-of-range confidence, and evidence not locatable by existing `evidence_is_locatable`. Set `claim_id` deterministically and validate the horizon tuple:

```python
claim_id = f"{video.bvid}:{revision}:{claim_index}"
if horizon_source is HorizonSource.UNKNOWN:
    if any(value is not None for value in (minimum, point, maximum)):
        raise ClaimExtractionError("unknown horizon cannot contain trading days")
elif not (
    isinstance(minimum, int) and isinstance(point, int) and isinstance(maximum, int)
    and 1 <= minimum <= point <= maximum
):
    raise ClaimExtractionError("horizon must satisfy 1 <= min <= point <= max")
if deadline_at is not None and deadline_at <= video.published_at:
    raise ClaimExtractionError("deadline must be after publication")
if not all(evidence_is_locatable(item, segments) for item in evidence):
    raise ClaimExtractionError("claim evidence is not locatable")
```

- [ ] **Step 5: Test invalid payload retry versus provider failure**

```python
def test_invalid_claim_schema_is_requested_once_more_then_raises_parse_error(fake_transport):
    client = MiniMaxClient.for_test(lambda _segments: '{"claims":"bad"}')
    with pytest.raises(ClaimExtractionError, match="invalid claim extraction"):
        client.analyze_claims(VIDEO, SEGMENTS, revision=1, transcript_hash="hash")

def test_provider_exhaustion_remains_retryable_provider_error(fake_503_transport):
    with pytest.raises(MiniMaxProviderError):
        client.analyze_claims(VIDEO, SEGMENTS, revision=1, transcript_hash="hash")
```

- [ ] **Step 6: Test the hard three-way batch limit**

Use a barrier-protected fake request counter for six videos and assert `maximum_observed == 3` when configuration requests 10 and when it requests 3. Catch errors per future and return `ClaimExtractionFailure(bvid, "provider unavailable")` or `ClaimExtractionFailure(bvid, "invalid structured response")`; never put exception text or response bodies in the result.

- [ ] **Step 7: Run MiniMax focused tests and compile**

Run: `.venv\Scripts\python.exe -m pytest tests/test_claim_extraction.py tests/test_minimax.py -q`

Run: `.venv\Scripts\python.exe -m py_compile goldbook/minimax.py goldbook/models.py`

Expected: all pass; no test performs a real provider request.

- [ ] **Step 8: Commit the extraction unit**

```powershell
git add goldbook/minimax.py tests/test_claim_extraction.py
git commit -m "feat: extract multiple forecast claims with M3"
```

---

### Task 3: Strict trading-day horizon resolution

**Files:**
- Create: `goldbook/claim_time.py`
- Create: `tests/test_claim_time.py`

**Interfaces:**
- Consumes: `ForecastClaim`, `Video`, `PriceBar`
- Produces: `ClaimWindow(start_date: date | None, end_date: date | None, mature: bool, reason: str | None)`
- Produces: `resolve_claim_window(claim: ForecastClaim, video: Video, bars: Sequence[PriceBar], *, next_same_instrument_prediction_at: datetime | None = None, superseded_at: datetime | None = None, evaluated_at: datetime) -> ClaimWindow`
- Produces: `find_next_same_instrument_prediction(claim, video, creator_videos, creator_claims) -> datetime | None`

- [ ] **Step 1: Write failing strict-time tests**

```python
def test_publish_day_bar_is_never_observed():
    video = video_at("2026-08-03T12:00:00+08:00")
    window = resolve_claim_window(claim(days=(1, 2, 3)), video,
        bars("2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06"),
        evaluated_at=aware("2026-08-07T00:00:00+08:00"))
    assert window.start_date == date(2026, 8, 4)
    assert window.end_date == date(2026, 8, 6)

def test_intraday_claim_is_unresolved_with_daily_bars():
    window = resolve_claim_window(claim(horizon_text="今晚", days=(None, None, None)),
        video_at("2026-08-03T12:00:00+08:00"), bars("2026-08-03", "2026-08-04"),
        evaluated_at=aware("2026-08-05T00:00:00+08:00"))
    assert window.reason == "unresolved_intraday_data"
```

- [ ] **Step 2: Run and observe `goldbook.claim_time` missing**

Run: `.venv\Scripts\python.exe -m pytest tests/test_claim_time.py -q`

Expected: collection fails with `ModuleNotFoundError`.

- [ ] **Step 3: Implement publication-day exclusion and horizon upper-bound maturity**

Sort bars by `trade_date`, select only dates strictly greater than the Shanghai publication date, and mature at the horizon upper bound:

```python
publication_date = video.published_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
eligible = tuple(bar for bar in sorted(bars, key=lambda value: value.trade_date)
                 if bar.trade_date > publication_date)
maximum = claim.horizon_max_trading_days
if maximum is not None:
    if not eligible:
        return ClaimWindow(None, None, False, "awaiting_first_complete_bar")
    if len(eligible) < maximum:
        return ClaimWindow(eligible[0].trade_date, eligible[-1].trade_date,
                           False, "horizon_not_mature")
    return ClaimWindow(eligible[0].trade_date, eligible[maximum - 1].trade_date,
                       True, None)
```

For an exact `deadline_at`, include only complete bar dates whose session end is no later than the deadline; never include the publication-day bar.

- [ ] **Step 4: Implement the no-horizon next-prediction cutoff**

```python
def test_unknown_horizon_stops_before_next_same_instrument_prediction():
    cutoff = aware("2026-08-06T10:00:00+08:00")
    window = resolve_claim_window(claim(days=(None, None, None)), VIDEO,
        bars("2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"),
        next_same_instrument_prediction_at=cutoff, evaluated_at=aware("2026-08-08T00:00:00+08:00"))
    assert window.end_date == date(2026, 8, 5)
    assert window.mature is True

def test_explicit_long_horizon_is_not_cut_off_by_daily_video():
    window = resolve_claim_window(claim(days=(10, 15, 20)), VIDEO, twenty_bars(),
        next_same_instrument_prediction_at=aware("2026-08-05T10:00:00+08:00"),
        evaluated_at=aware("2026-09-01T00:00:00+08:00"))
    assert window.end_date == twenty_bars()[19].trade_date
```

Only claims with `HorizonSource.UNKNOWN` use the mechanical next-video cutoff. `superseded_at` applies to any horizon and returns a window reason of `superseded` so the evaluator emits `EvaluationVerdict.SUPERSEDED`. `find_next_same_instrument_prediction` ignores excluded/no-claim videos and different instruments.

- [ ] **Step 5: Add weekend,休市, timezone and supersession tests**

```python
def test_saturday_publication_starts_on_monday():
    assert resolved(SATURDAY_VIDEO, MONDAY_AND_TUESDAY).start_date == date(2026, 8, 24)

def test_explicit_supersession_uses_only_full_bars_before_new_publication():
    assert resolved(OLD_CLAIM, DAILY_BARS, superseded_at=NEW_VIDEO.published_at).end_date \
        == date(2026, 8, 5)

def test_missing_weekday_is_not_counted_as_a_trading_day():
    assert resolved(VIDEO, BARS_WITH_HOLIDAY).end_date == date(2026, 8, 7)
```

- [ ] **Step 6: Run focused tests and commit**

Run: `.venv\Scripts\python.exe -m pytest tests/test_claim_time.py -q`

```powershell
git add goldbook/claim_time.py tests/test_claim_time.py
git commit -m "feat: resolve forecast horizons without lookahead"
```

---

### Task 4: Deterministic claim evaluation

**Files:**
- Create: `goldbook/claim_evaluation.py`
- Create: `tests/test_claim_evaluation.py`

**Interfaces:**
- Consumes: `ForecastClaim`, `Video`, `PriceBar`, `ClaimWindow`
- Produces: `evaluate_claim(claim: ForecastClaim, video: Video, bars: Sequence[PriceBar], window: ClaimWindow, *, evaluated_at: datetime) -> ClaimEvaluation`
- Produces: `recompute_claim_evaluations(database: Database, *, evaluated_at: datetime) -> ClaimRecomputationSummary`

- [ ] **Step 1: Write failing point-touch and near tests**

```python
def test_target_touch_uses_high_and_records_first_hit():
    result = evaluate_claim(target(">=", 4700), VIDEO,
        ohlc((4660, 4690, 4650, 4680), (4680, 4702, 4670, 4698)),
        mature_window("2026-08-04", "2026-08-05"), evaluated_at=NOW)
    assert result.verdict is EvaluationVerdict.HIT
    assert result.first_hit_date == date(2026, 8, 5)
    assert result.observed_max == 4702

def test_exact_break_not_relabelled_hit_when_only_near():
    result = evaluate_claim(target(">=", 4700), VIDEO,
        ohlc((4660, 4680, 4650, 4670), (4670, 4685, 4660, 4680)),
        mature_window("2026-08-04", "2026-08-05"), evaluated_at=NOW)
    assert result.verdict is EvaluationVerdict.PARTIAL_NEAR
    assert result.distance_pct == pytest.approx((4700 - 4685) / 4700)
```

- [ ] **Step 2: Run and observe the missing evaluator failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_claim_evaluation.py -q`

Expected: collection fails for `goldbook.claim_evaluation`.

- [ ] **Step 3: Implement executable claim rules**

Implement all verdicts from the exact bar slice selected by `ClaimWindow`:

```python
window_bars = [bar for bar in bars
               if window.start_date <= bar.trade_date <= window.end_date]
entry_price = window_bars[0].open
observed_min = min(bar.low for bar in window_bars)
observed_max = max(bar.high for bar in window_bars)
final_close = window_bars[-1].close

def leg_matches(leg: ClaimLeg, bar: PriceBar) -> bool:
    if leg.operator == ">=":
        return bar.high >= require_level(leg.level_low)
    if leg.operator == "<=":
        return bar.low <= require_level(leg.level_low)
    if leg.operator == "between":
        return bar.low >= require_level(leg.level_low) and bar.high <= require_level(leg.level_high)
    raise ValueError("unsupported claim operator")
```

For holds, require two adjacent closes on the requested side. For sequences, advance the leg index only after its current leg first matches, so reverse order fails. For directional claims, compare deadline close against entry open. Save min, max, final close, closest price/date, exact distance and first-hit date in every mature result.

- [ ] **Step 4: Add rule-specific tests**

```python
@pytest.mark.parametrize("claim_type,expected", [
    (ClaimType.HOLD_ABOVE, EvaluationVerdict.HIT),
    (ClaimType.HOLD_BELOW, EvaluationVerdict.MISS),
])
def test_hold_requires_two_consecutive_closes(claim_type, expected):
    assert evaluate_claim(hold_claim(claim_type, 4700), VIDEO, HOLD_BARS, WINDOW,
                          evaluated_at=NOW).verdict is expected

def test_sequence_fails_when_events_arrive_in_reverse_order():
    assert evaluate_claim(sequence(4650, 4700), VIDEO, REVERSE_BARS, WINDOW,
                          evaluated_at=NOW).verdict is EvaluationVerdict.MISS
```

Add assertions for bullish/bearish direction, range, numeric volatility, unsupported instrument, unresolved intraday data, unknown immature horizon, excluded and superseded claims.

- [ ] **Step 5: Implement database-wide recomputation**

Load each creator's videos and latest claims, determine next same-instrument predictions, evaluate against cached price bars, and replace stale results:

```python
@dataclass(frozen=True)
class ClaimRecomputationSummary:
    evaluated: int
    unresolved: int
    deleted: int
    failed: int

def recompute_claim_evaluations(database: Database, *, evaluated_at: datetime) -> ClaimRecomputationSummary:
    bars = database.list_price_bars()
    live_ids: set[str] = set()
    evaluated = unresolved = failed = 0
    for creator in database.list_creators():
        videos = database.list_videos(creator.uid)
        claims = database.list_creator_forecast_claims(creator.uid)
        for claim in claims:
            live_ids.add(claim.claim_id)
            video = next(value for value in videos if value.bvid == claim.bvid)
            cutoff = find_next_same_instrument_prediction(claim, video, videos, claims)
            window = resolve_claim_window(claim, video, bars,
                next_same_instrument_prediction_at=cutoff, evaluated_at=evaluated_at)
            result = evaluate_claim(claim, video, bars, window, evaluated_at=evaluated_at)
            database.save_claim_evaluation(result)
            unresolved += result.verdict is EvaluationVerdict.UNRESOLVED
            evaluated += result.verdict is not EvaluationVerdict.UNRESOLVED
    deleted = database.delete_claim_evaluations_except(live_ids)
    return ClaimRecomputationSummary(evaluated, unresolved, deleted, failed)
```

Task 1 must therefore also expose `list_price_bars() -> list[PriceBar]` and `delete_claim_evaluations_except(live_ids: set[str]) -> int`.

- [ ] **Step 6: Prove recomputation is idempotent and refresh-matures claims**

```python
first = recompute_claim_evaluations(database, evaluated_at=NOW)
second = recompute_claim_evaluations(database, evaluated_at=NOW)
assert second == first
database.replace_prices(MORE_BARS)
third = recompute_claim_evaluations(database, evaluated_at=LATER)
assert third.evaluated == first.evaluated + 1
```

- [ ] **Step 7: Run focused tests and commit**

Run: `.venv\Scripts\python.exe -m pytest tests/test_claim_evaluation.py tests/test_claim_time.py tests/test_claim_db.py -q`

```powershell
git add goldbook/claim_evaluation.py tests/test_claim_evaluation.py
git commit -m "feat: evaluate forecast claims against OHLC"
```

---

### Task 5: Cached transcript reanalysis and CLI orchestration

**Files:**
- Create: `goldbook/claim_pipeline.py`
- Modify: `goldbook/__main__.py`
- Modify: `goldbook/recompute.py`
- Modify: `goldbook/db.py`
- Create: `tests/test_claim_pipeline.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: `Database`, `MiniMaxClient`, `run_claim_extraction_batch`, `recompute_claim_evaluations`
- Produces: `ClaimBackfillSummary(total: int, completed: int, skipped: int, failed: int)`
- Produces: `reanalyse_cached_claims(database: Database, client: MiniMaxClient, *, on_progress: Callable[[int, int, str], None] | None = None) -> ClaimBackfillSummary`
- Produces CLI: `python -m goldbook reanalyse-claims`

- [ ] **Step 1: Write a failing cached-only pipeline test**

```python
def test_backfill_uses_cached_transcripts_without_media_or_whisper(database, fake_claim_client):
    seed_video_and_transcript(database, bvid="BV1", text="短期回踩4650后看4700")
    summary = reanalyse_cached_claims(database, fake_claim_client)
    assert summary == ClaimBackfillSummary(total=1, completed=1, skipped=0, failed=0)
    assert database.list_forecast_claims("BV1")[0].legs[1].level_low == 4700
    assert fake_claim_client.calls == ["BV1"]
```

- [ ] **Step 2: Run it and observe `claim_pipeline` missing**

Run: `.venv\Scripts\python.exe -m pytest tests/test_claim_pipeline.py -q`

Expected: collection fails with `ModuleNotFoundError`.

- [ ] **Step 3: Implement resumable batch reanalysis**

List cached transcripts, skip an exact extraction identity, process the remainder through the bounded batch API, and persist each completed extraction:

```python
@dataclass(frozen=True)
class ClaimBackfillSummary:
    total: int
    completed: int
    skipped: int
    failed: int

def reanalyse_cached_claims(database: Database, client: MiniMaxClient, *, on_progress=None):
    pending = []
    skipped = 0
    for video, _analysis in database.list_videos_with_latest_analysis():
        segments = tuple(database.list_transcript_segments(video.bvid))
        if not segments:
            continue
        identity = transcript_hash(segments)
        if database.has_claim_extraction(video.bvid, identity, "MiniMax-M3", CLAIM_PROMPT_VERSION):
            skipped += 1
            continue
        pending.append((video, segments, database.next_analysis_revision(video.bvid), identity))
    results = run_claim_extraction_batch(client, pending)
    failures = 0
    for completed, item in enumerate(pending, start=1):
        video, _segments, revision, identity = item
        extraction = results[video.bvid]
        if isinstance(extraction, ClaimExtractionFailure):
            failures += 1
            if on_progress:
                on_progress(completed, len(pending), video.bvid)
            continue
        database.save_claim_extraction(video, revision, identity, extraction)
        if on_progress:
            on_progress(completed, len(pending), video.bvid)
    recompute_claim_evaluations(database, evaluated_at=datetime.now(timezone.utc))
    return ClaimBackfillSummary(len(pending) + skipped, len(results) - failures, skipped, failures)
```

Expose `Database.next_analysis_revision` and `Database.save_claim_extraction` as one-transaction repository helpers. Provider or parse failures increment `failed` and leave no cache identity so the next run retries them.

`save_claim_extraction` also creates the legacy-compatible video summary deterministically: no claims becomes `Direction.NO_SIGNAL`; claims all sharing one actionable direction use that direction; mixed directions become `Direction.NEUTRAL`. Valid evidence-backed extractions use `ReviewStatus.APPROVED` only for old-page compatibility—the new evaluator and ranking never read that field as an approval gate. Store `model_name="MiniMax-M3"`, `prompt_version="claims-v1"`, the transcript hash and the structured extraction JSON on that analysis revision.

- [ ] **Step 4: Test idempotency, failure retry and maximum concurrency**

```python
first = reanalyse_cached_claims(database, client)
second = reanalyse_cached_claims(database, client)
assert first.completed == 6
assert second.skipped == 6
assert client.maximum_observed_concurrency == 3
assert database.list_forecast_claims("FAILED_ONCE") == [EXPECTED_AFTER_RETRY]
```

- [ ] **Step 5: Add the CLI command with safe progress**

The command loads the existing local `.env` through the same startup path, refuses to run without a key, prints only `completed/total` and BVID, never prints transcript/model response/key, recomputes claim evaluations after extraction, and exits nonzero when `failed > 0`.

```python
def _reanalyse_claims(settings: Settings, database: Database) -> int:
    if not settings.minimax_api_key:
        raise SystemExit("MINIMAX_API_KEY is required")
    client = MiniMaxClient(settings)
    summary = reanalyse_cached_claims(
        database, client,
        on_progress=lambda done, total, bvid: print(f"{done}/{total} {bvid}"),
    )
    return 1 if summary.failed else 0

result = main(["reanalyse-claims", "--data-dir", str(tmp_path)])
assert result == 0
assert "1/1" in capsys.readouterr().out
assert secret not in capsys.readouterr().out
```

- [ ] **Step 6: Link price refresh to claim evaluation only**

After `replace_prices`, call both legacy `recompute_cached_outcomes` and new `recompute_claim_evaluations`. Assert `MiniMaxClient` is never constructed by `refresh-prices`.

- [ ] **Step 7: Run pipeline, CLI and legacy job regressions**

Run: `.venv\Scripts\python.exe -m pytest tests/test_claim_pipeline.py tests/test_cli.py tests/test_jobs.py tests/test_recompute.py -q`

- [ ] **Step 8: Commit the orchestration unit**

```powershell
git add goldbook/claim_pipeline.py goldbook/__main__.py goldbook/recompute.py goldbook/db.py tests/test_claim_pipeline.py tests/test_cli.py
git commit -m "feat: reanalyse cached transcripts into claims"
```

---

### Task 6: Video-equal creator metrics

**Files:**
- Create: `goldbook/claim_metrics.py`
- Create: `tests/test_claim_metrics.py`

**Interfaces:**
- Produces: `VideoClaimMetrics`
- Produces: `CreatorClaimMetrics`
- Produces: `aggregate_video_claims(bvid: str, claims: Sequence[ForecastClaim], evaluations: Sequence[ClaimEvaluation]) -> VideoClaimMetrics`
- Produces: `aggregate_creator_claims(videos: Sequence[VideoClaimMetrics]) -> CreatorClaimMetrics`

- [ ] **Step 1: Write the failing equal-video-weight test**

```python
def test_many_claims_in_one_video_do_not_outweigh_other_videos():
    first = video_metrics("BV1", [HIT] * 10)
    second = video_metrics("BV2", [MISS])
    third = video_metrics("BV3", [MISS])
    metrics = aggregate_creator_claims([first, second, third])
    assert first.score == 1.0
    assert metrics.score == pytest.approx(1 / 3)
    assert metrics.eligible_for_rank is True
```

- [ ] **Step 2: Run and observe the missing metrics module**

Run: `.venv\Scripts\python.exe -m pytest tests/test_claim_metrics.py -q`

- [ ] **Step 3: Implement transparent metrics**

Map verdicts and aggregate in two stages:

```python
_SCORES = {
    EvaluationVerdict.HIT: 1.0,
    EvaluationVerdict.PARTIAL_NEAR: 0.5,
    EvaluationVerdict.MISS: 0.0,
}

def aggregate_video_claims(bvid, claims, evaluations):
    by_id = {value.claim_id: value for value in evaluations}
    scoreable = [by_id[claim.claim_id] for claim in claims
                 if claim.claim_id in by_id and by_id[claim.claim_id].verdict in _SCORES]
    score = (sum(_SCORES[value.verdict] for value in scoreable) / len(scoreable)
             if scoreable else None)
    return VideoClaimMetrics.from_rows(bvid, claims, scoreable, score)

def aggregate_creator_claims(videos):
    scores = [video.score for video in videos if video.score is not None]
    return CreatorClaimMetrics.from_videos(
        videos, score=(sum(scores) / len(scores) if scores else None),
        eligible_for_rank=len(scores) >= 3,
    )
```

`from_rows` and `from_videos` calculate exact hit rate, near-inclusive rate, directional rate, target rate, condition/sequence rate, mean closest distance, maturity/coverage counts, horizon groups and verdict counts from their supplied immutable rows.

- [ ] **Step 4: Add eligibility, revision and coverage tests**

Assert fewer than three mature scoreable videos is ineligible, only the latest claim revision counts, unresolved claims reduce coverage without reducing accuracy, and a video with zero scoreable claims has `score=None`.

- [ ] **Step 5: Run focused and legacy metric tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_claim_metrics.py tests/test_scoring.py -q`

- [ ] **Step 6: Commit the metrics unit**

```powershell
git add goldbook/claim_metrics.py tests/test_claim_metrics.py
git commit -m "feat: rank creators with video-equal claim metrics"
```

---

### Task 7: Claim comparison Web UI and optional corrections

**Files:**
- Modify: `goldbook/web.py`
- Modify: `goldbook/templates/creator.html`
- Modify: `goldbook/templates/video.html`
- Modify: `goldbook/templates/leaderboard.html`
- Modify: `goldbook/static/app.css`
- Modify: `goldbook/static/app.js`
- Create: `tests/test_claim_web.py`

**Interfaces:**
- Consumes: claim repository, evaluation repository and claim metrics
- Produces: existing nested route `/creators/<uid>/videos/<bvid>` with `claim_rows`
- Produces: `POST /creators/<uid>/videos/<bvid>/claims/<claim_id>/correct`
- Preserves: legacy `/videos/<bvid>` redirect/route, local-only binding, CSRF and security headers

- [ ] **Step 1: Write a failing per-video comparison page test**

```python
def test_video_page_renders_each_claim_against_actual_result(client, seeded_claims):
    body = client.get("/creators/42/videos/BV1").get_data(as_text=True)
    assert "先回踩4650再看4700" in body
    assert "短期 → 2个交易日（1–3）" in body
    assert "实际最高 4702" in body
    assert "首次命中 2026-08-05" in body
    assert "命中" in body
```

- [ ] **Step 2: Run and observe missing claim content**

Run: `.venv\Scripts\python.exe -m pytest tests/test_claim_web.py::test_video_page_renders_each_claim_against_actual_result -q`

Expected: response is 200 but lacks the new claim text and evaluation fields.

- [ ] **Step 3: Build claim page contexts and cards**

Load only claims belonging to the nested creator/video route and join results by ID:

```python
claims = db.list_forecast_claims(video.bvid)
claim_rows = [
    {"claim": claim, "evaluation": db.get_claim_evaluation(claim.claim_id)}
    for claim in claims
]
return render_template("video.html", video=video, analysis=analysis,
                       outcome=outcome, claim_rows=claim_rows,
                       claim_chart=_claim_chart(db, video, claim_rows))
```

Render one card per row. The card must label three distinct sources: `作者原话`, `M3 周期换算`, and `程序行情验证`. Show unresolved reason instead of a red failure badge.

- [ ] **Step 4: Add chart markers and target lines**

Return JSON containing publication, first complete bar, deadline, first hit, supersession, level lines and only the exact bar slice used by the evaluator:

```python
return {
    "published_at": video.published_at.isoformat(),
    "claims": [{
        "claim_id": row["claim"].claim_id,
        "levels": [leg.level_low for leg in row["claim"].legs if leg.level_low is not None],
        "window_start": _iso(row["evaluation"].window_start),
        "window_end": _iso(row["evaluation"].window_end),
        "first_hit": _iso(row["evaluation"].first_hit_date),
    } for row in claim_rows if row["evaluation"] is not None],
}
```

`app.js` consumes only this server-produced JSON and cannot recompute verdicts in the browser.

- [ ] **Step 5: Upgrade creator and leaderboard pages**

Creator rows show claim count, hit/near/miss/unresolved counts, video score and coverage, with every row linking to its nested detail page. Leaderboard orders eligible creators by `CreatorClaimMetrics.score`, displays sample videos and coverage, and shows exact/near/direction/target metrics rather than legacy review counts.

- [ ] **Step 6: Add optional correction with revision audit**

```python
response = client.post(
    "/creators/42/videos/BV1/claims/BV1:1:0/correct",
    data={"csrf_token": token, "claim_type": "target_touch", "instrument": "xau_usd_spot",
          "operator": ">=", "level_low": "4710", "horizon_min": "1",
          "horizon_point": "2", "horizon_max": "3", "evidence_json": evidence_json},
)
assert response.status_code == 303
assert db.list_forecast_claims("BV1")[0].status is ClaimStatus.HUMAN_CORRECTED
assert db.get_claim_evaluation(db.list_forecast_claims("BV1")[0].claim_id) is not None
```

The route validates evidence with the shared locator, creates a new analysis revision and claim IDs, never mutates the old revision, and recomputes the affected creator's evaluations.

- [ ] **Step 7: Test ownership, CSRF, unresolved and no-JavaScript behavior**

Assert wrong creator returns 404, production correction without CSRF returns 400, unsupported instrument renders “品种不匹配”, intraday renders “缺少分时行情”, and all comparison text is present in server HTML before JavaScript executes.

- [ ] **Step 8: Run Web and security regressions**

Run: `.venv\Scripts\python.exe -m pytest tests/test_claim_web.py tests/test_web.py -q`

Expected: all pass; CSP, `nosniff`, `SAMEORIGIN` and referrer headers remain present.

- [ ] **Step 9: Commit the Web unit**

```powershell
git add goldbook/web.py goldbook/templates/creator.html goldbook/templates/video.html goldbook/templates/leaderboard.html goldbook/static/app.css goldbook/static/app.js tests/test_claim_web.py
git commit -m "feat: show claim-by-claim forecast outcomes"
```

---

### Task 8: Demo, documentation, real backfill, and final verification

**Files:**
- Modify: `scripts/seed_demo.py`
- Modify: `tests/test_demo.py`
- Modify: `README.md`
- Modify: `docs/verification.md`

**Interfaces:**
- Consumes: all prior tasks
- Produces: idempotent offline multi-claim demo
- Produces: completed local M3 backfill and evaluation for the two configured UP creators

- [ ] **Step 1: Write a failing demo assertion**

```python
def test_demo_contains_hit_near_miss_and_unresolved_claims(tmp_path):
    seed_demo(tmp_path)
    db = open_demo(tmp_path)
    verdicts = {value.verdict for creator in db.list_creators()
                for value in db.list_creator_claim_evaluations(creator.uid)}
    assert {EvaluationVerdict.HIT, EvaluationVerdict.PARTIAL_NEAR,
            EvaluationVerdict.MISS, EvaluationVerdict.UNRESOLVED} <= verdicts
```

- [ ] **Step 2: Run it and observe missing demo claims**

Run: `.venv\Scripts\python.exe -m pytest tests/test_demo.py::test_demo_contains_hit_near_miss_and_unresolved_claims -q`

- [ ] **Step 3: Seed deterministic multi-claim demo data**

Add point touch, reverse sequence, vague intraday and unsupported-instrument examples without network access, then compute them with the production evaluator:

```python
database.replace_forecast_claims("BVDEMOA1", 1, demo_claims())
recompute_claim_evaluations(database, evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc))
```

Re-running the seed command must leave row counts unchanged.

- [ ] **Step 4: Update user documentation**

Document the `reanalyse-claims` command, dynamic trading-day semantics, publication-day exclusion, 0.5% near rule, two-close hold rule, video-equal weighting, unsupported instruments, daily-data limitation, optional correction and the fact that old 1/5/20 results are legacy compatibility data.

- [ ] **Step 5: Run the complete offline suite before any paid call**

Run: `.venv\Scripts\python.exe -m pytest -q`

Run: `.venv\Scripts\python.exe -m compileall -q goldbook scripts tests`

Run: `.venv\Scripts\python.exe -m pip check`

Run: `git diff --check`

Expected: all tests pass, compile/check commands exit 0, and no media or key value appears in tracked files.

- [ ] **Step 6: Back up the local database and run the real cached transcript backfill**

Copy only the SQLite database to a timestamped file inside the ignored local data directory, then run:

```powershell
.venv\Scripts\python.exe -m goldbook reanalyse-claims
```

Monitor progress until every cached video is either completed or has an explicit retryable failure. Re-run the same command to retry failures and prove completed identities are skipped. Do not print or copy the configured API key.

- [ ] **Step 7: Refresh prices and recompute mature claims**

Run: `.venv\Scripts\python.exe -m goldbook refresh-prices`

Verify that extraction row counts do not change, claim evaluations do change when new bars mature them, and no MiniMax request occurs during the refresh.

- [ ] **Step 8: Verify both real creator lines and all video pages**

For UIDs `546630884` and `1847287889`, assert every cached video has either a latest claim extraction or explicit `no_claim`/failure state; request every nested video page and require HTTP 200. Sample videos containing short/medium/long, 4700, break/below/hold and pullback language, and compare evidence, converted horizon and OHLC result.

- [ ] **Step 9: Run final regression and safety checks**

Run the four commands from Step 5 again. Scan the workspace for audio/video extensions under the task temp and data directories, confirm zero retained media, scan tracked files for the exact configured key without printing it, and confirm `/api/status` contains model name but no key field or value.

- [ ] **Step 10: Record fresh verification evidence and commit delivery docs**

Update `docs/verification.md` with exact test count, completed/skipped/failed backfill counts, claim/evaluation counts, two creator coverage figures, provider/date window and remaining unresolved reasons.

```powershell
git add scripts/seed_demo.py tests/test_demo.py README.md docs/verification.md
git commit -m "docs: deliver claim-level forecast evaluation"
```
