# Intraday Claim Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evaluate every eligible intraday gold forecast from the first complete one-hour XAU/USD bar after the Bilibili publication timestamp, persist exact timestamps, and explain the result in the local Web UI.

**Architecture:** Keep the existing daily pipeline unchanged for horizons of one or more trading days. Add a strict one-hour XAUS adapter and SQLite cache, add an intraday window beside `ClaimWindow`, route only intraday claims through timestamp-aware deterministic scoring, then expose timestamped evidence and focused hourly chart data through the existing Flask pages.

**Tech Stack:** Python 3.12, dataclasses, `httpx`, SQLite, Flask/Jinja, Chart.js, pytest

**Spec:** `docs/superpowers/specs/2026-08-22-intraday-claim-evaluation-design.md`

## Global Constraints

- Internal timestamps are timezone-aware UTC; author-facing semantics and rendered timestamps use `Asia/Shanghai`.
- Only complete one-hour bars whose start is at or after `video.published_at` may be evaluated.
- The market identity must be `xau`, `Gold (XAU/USD)`, `USD`, interval `1h`, and fresh/upstream.
- Existing daily `PriceBar`, daily outcomes, facts, claims, and evaluations remain backward compatible.
- Missing intraday data stays unresolved; daily data must never substitute for an intraday decision.
- Network failure must preserve previously cached hourly bars.
- The app remains bound to loopback and no key, Cookie, provider body, or media file may appear in logs or pages.

---

### Task 1: Hourly market model and strict XAUS adapter

**Files:**
- Create: `goldbook/intraday_market.py`
- Modify: `goldbook/models.py:141-151`
- Test: `tests/test_intraday_market.py`

**Interfaces:**
- Consumes: XAUS JSON from `GET https://xaus.com/api/v1/chart?symbol=xau&range=1y&interval=1h`.
- Produces: `IntradayPriceBar(started_at: datetime, interval_minutes: int, open: float, high: float, low: float, close: float, provider: str)`; `parse_xaus_intraday_chart(payload) -> list[IntradayPriceBar]`; `XausIntradayMarketDataSource.fetch(start: datetime, end: datetime) -> list[IntradayPriceBar]`.

- [ ] **Step 1: Write model and parser failure tests**

```python
def test_parse_xaus_intraday_chart_preserves_utc_hours():
    payload = {
        "symbol": "xau", "label": "Gold (XAU/USD)", "currency": "USD",
        "interval": "1h",
        "data_state": {"status": "fresh", "source": "upstream", "age_seconds": 0},
        "points": [{"t": 1787302800, "o": 4400, "h": 4412, "l": 4398, "c": 4410}],
    }
    bars = parse_xaus_intraday_chart(payload)
    assert bars == [IntradayPriceBar(
        datetime.fromtimestamp(1787302800, timezone.utc), 60,
        4400.0, 4412.0, 4398.0, 4410.0, "XAUS (xaus.com; Yahoo Finance proxy)",
    )]

@pytest.mark.parametrize("field,value", [
    ("symbol", "GC=F"), ("currency", "CNY"), ("interval", "1d"),
])
def test_parse_xaus_intraday_chart_rejects_wrong_market_identity(field, value):
    payload = valid_hourly_payload()
    payload[field] = value
    with pytest.raises(ValueError, match="hourly XAU/USD"):
        parse_xaus_intraday_chart(payload)
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_intraday_market.py -q`

Expected: collection fails because `goldbook.intraday_market` and `IntradayPriceBar` do not exist.

- [ ] **Step 3: Implement the immutable bar and parser**

```python
@dataclass(frozen=True)
class IntradayPriceBar:
    started_at: datetime
    interval_minutes: int
    open: float
    high: float
    low: float
    close: float
    provider: str

    def __post_init__(self) -> None:
        _require_aware_datetime(self.started_at, "started_at")
        if self.interval_minutes != 60:
            raise ValueError("intraday interval must be 60 minutes")
        values = (self.open, self.high, self.low, self.close)
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("intraday OHLC values must be finite and positive")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("invalid intraday OHLC range")
```

```python
class XausIntradayMarketDataSource:
    provider_name = "XAUS (xaus.com; Yahoo Finance proxy)"

    def fetch(self, start: datetime, end: datetime) -> list[IntradayPriceBar]:
        _require_aware_range(start, end)
        response = self._client.get(
            "https://xaus.com/api/v1/chart",
            params={"symbol": "xau", "range": "1y", "interval": "1h"},
            timeout=20.0,
        )
        response.raise_for_status()
        return [bar for bar in parse_xaus_intraday_chart(response.json())
                if start <= bar.started_at <= end]
```

- [ ] **Step 4: Run focused tests and the existing market tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_intraday_market.py tests\test_market.py -q`

Expected: PASS with no warnings.

- [ ] **Step 5: Commit Task 1**

```powershell
git add goldbook/models.py goldbook/intraday_market.py tests/test_intraday_market.py
git commit -m "feat: add strict hourly gold market adapter"
```

---

### Task 2: SQLite hourly cache and timestamped evaluation migration

**Files:**
- Modify: `goldbook/db.py:65-220,536-580,645-700,1698-1740`
- Modify: `goldbook/models.py:187-211`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: `Iterable[IntradayPriceBar]` from Task 1.
- Produces: `Database.upsert_intraday_prices(bars) -> int`; `Database.list_intraday_price_bars(start: datetime | None = None, end: datetime | None = None) -> list[IntradayPriceBar]`; four optional UTC fields on `ClaimEvaluation`: `window_start_at`, `window_end_at`, `closest_at`, `first_hit_at`.

- [ ] **Step 1: Write migration, idempotency, and range-query tests**

```python
def test_intraday_prices_upsert_and_query_by_aware_utc_range(tmp_path):
    db = Database(tmp_path / "goldbook.db")
    db.initialize()
    first = IntradayPriceBar(_utc("2026-08-12T10:00:00"), 60, 4400, 4410, 4390, 4405, "XAUS")
    corrected = replace(first, close=4407)
    second = IntradayPriceBar(_utc("2026-08-12T11:00:00"), 60, 4407, 4420, 4400, 4418, "XAUS")
    assert db.upsert_intraday_prices((first, second)) == 2
    assert db.upsert_intraday_prices((corrected,)) == 1
    assert db.list_intraday_price_bars(_utc("2026-08-12T10:30:00"), None) == [second]

def test_existing_database_migrates_claim_evaluation_timestamp_columns(tmp_path):
    create_pre_intraday_schema(tmp_path / "goldbook.db")
    db = Database(tmp_path / "goldbook.db")
    db.initialize()
    columns = {row[1] for row in sqlite3.connect(db.path).execute(
        "PRAGMA table_info(claim_evaluations)"
    )}
    assert {"window_start_at", "window_end_at", "closest_at", "first_hit_at"} <= columns
```

- [ ] **Step 2: Run the two tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_db.py -k "intraday or timestamp_columns" -q`

Expected: FAIL because the table, methods, and model fields are absent.

- [ ] **Step 3: Add schema and safe migration**

```sql
CREATE TABLE IF NOT EXISTS intraday_prices (
    started_at TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    provider TEXT NOT NULL,
    PRIMARY KEY (started_at, interval_minutes)
)
```

Add nullable `TEXT` columns `window_start_at`, `window_end_at`, `closest_at`, and `first_hit_at` through the repository's existing idempotent column-migration helper. Serialize aware datetimes with the existing `_serialize_datetime` and reject naive query bounds.

- [ ] **Step 4: Extend `ClaimEvaluation` and repository serialization**

```python
@dataclass(frozen=True)
class ClaimEvaluation:
    # existing fields remain in their current order
    reason: str
    window_start_at: datetime | None = None
    window_end_at: datetime | None = None
    closest_at: datetime | None = None
    first_hit_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("evaluated_at", "window_start_at", "window_end_at", "closest_at", "first_hit_at"):
            _require_aware_datetime(getattr(self, name), name)
```

- [ ] **Step 5: Run DB regression tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_db.py -q`

Expected: PASS, including old-schema migration and daily evaluation round trips.

- [ ] **Step 6: Commit Task 2**

```powershell
git add goldbook/models.py goldbook/db.py tests/test_db.py
git commit -m "feat: cache hourly prices and evaluation timestamps"
```

---

### Task 3: Strict post-publication intraday window engine

**Files:**
- Modify: `goldbook/claim_time.py:1-180`
- Test: `tests/test_claim_time.py`

**Interfaces:**
- Consumes: `ForecastClaim`, `Video`, `Sequence[IntradayPriceBar]`, cutoff timestamps, and `evaluated_at`.
- Produces: public `is_intraday_claim(claim: ForecastClaim) -> bool`; `IntradayClaimWindow(start_at: datetime | None, end_at: datetime | None, mature: bool, reason: str | None, bars: tuple[IntradayPriceBar, ...])`; `resolve_intraday_claim_window(claim: ForecastClaim, video: Video, bars: Sequence[IntradayPriceBar], *, next_same_instrument_prediction_at: datetime | None = None, superseded_at: datetime | None = None, evaluated_at: datetime) -> IntradayClaimWindow`.

- [ ] **Step 1: Replace the existing skip assertions with strict-window tests**

```python
def test_intraday_window_excludes_hour_crossing_publication_time():
    video = _video(_at("2026-08-12T10:35:00+00:00"))
    claim = _claim(horizon_text="今天日内", minimum=0, point=0, maximum=0)
    bars = _hourly("2026-08-12T10:00:00+00:00", "2026-08-12T11:00:00+00:00")
    window = resolve_intraday_claim_window(
        claim, video, bars, evaluated_at=_at("2026-08-13T00:00:00+00:00")
    )
    assert [bar.started_at for bar in window.bars] == [_at("2026-08-12T11:00:00+00:00")]

def test_incomplete_hour_after_publication_is_not_observed():
    video = _video(_at("2026-08-12T10:35:00+00:00"))
    claim = _claim(horizon_text="未来两小时", minimum=0, point=0, maximum=0)
    window = resolve_intraday_claim_window(
        claim, video, _hourly("2026-08-12T11:00:00+00:00"),
        evaluated_at=_at("2026-08-12T11:30:00+00:00"),
    )
    assert window.bars == ()
    assert window.reason == "unresolved_intraday_data"
```

Add table-driven tests with literal UTC results for `今天/日内`, `今晚`, an explicit `deadline_at`, `未来2小时`, zero-day fallback, next-prediction cutoff, and supersession cutoff.

- [ ] **Step 2: Run the focused time tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_claim_time.py -k intraday -q`

Expected: FAIL because intraday claims still return `unresolved_intraday_data` from the daily resolver and the new resolver is absent.

- [ ] **Step 3: Implement marker classification and deadline resolution**

```python
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_HOUR_COUNT = re.compile(r"(?:未来|接下来)?\s*([0-9]+|一|二|两|三|四|五|六|七|八|九|十|十二)\s*个?小时")

def _intraday_deadline(claim: ForecastClaim, published_at: datetime) -> datetime:
    if claim.deadline_at is not None:
        return claim.deadline_at.astimezone(timezone.utc)
    local = published_at.astimezone(_SHANGHAI)
    hours = _hours_from_text(claim.horizon_text or "")
    if hours is not None:
        return published_at + timedelta(hours=hours)
    if any(word in (claim.horizon_text or "") for word in ("今晚", "晚间", "夜间")):
        six = local.replace(hour=6, minute=0, second=0, microsecond=0)
        if six <= local:
            six += timedelta(days=1)
        return six.astimezone(timezone.utc)
    midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc)
```

```python
complete = tuple(
    bar for bar in sorted(bars, key=lambda item: item.started_at)
    if bar.started_at >= video.published_at
    and bar.started_at + timedelta(minutes=bar.interval_minutes)
       <= min(effective_end, evaluated_at)
)
```

The effective end is the earliest of the semantic deadline, a next same-instrument prediction, and `superseded_at`. A supersession sets reason `superseded`; otherwise maturity is `evaluated_at >= semantic_deadline`.

- [ ] **Step 4: Keep daily behavior unchanged and expose `is_intraday_claim`**

Change `resolve_claim_window` to continue returning the existing unresolved reason when called incorrectly for an intraday claim, while `recompute_claim_evaluations` will route it to the new resolver in Task 4. This preserves callers until routing is installed.

- [ ] **Step 5: Run all time tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_claim_time.py -q`

Expected: PASS for daily and hourly windows.

- [ ] **Step 6: Commit Task 3**

```powershell
git add goldbook/claim_time.py tests/test_claim_time.py
git commit -m "feat: resolve strict post-publication intraday windows"
```

---

### Task 4: Timestamp-aware deterministic claim scoring

**Files:**
- Modify: `goldbook/claim_evaluation.py:42-510`
- Test: `tests/test_claim_evaluation.py`

**Interfaces:**
- Consumes: `IntradayClaimWindow` and cached hourly bars from Tasks 2–3.
- Produces: `evaluate_intraday_claim(claim, video, window, evaluated_at) -> ClaimEvaluation`; `recompute_claim_evaluations` automatically selects hourly or daily evaluation.

- [ ] **Step 1: Write post-publication direction, target, sequence, hold, and maturity tests**

```python
def test_intraday_direction_compares_first_eligible_open_to_final_close():
    video = _video(_utc("2026-08-12T10:35:00"))
    claim = _claim(ClaimType.DIRECTIONAL_MOVE, Direction.BULLISH, "未来2小时")
    window = IntradayClaimWindow(
        _utc("2026-08-12T11:00:00"), _utc("2026-08-12T13:00:00"), True, None,
        (
            _hour("2026-08-12T11:00:00", 4400, 4410, 4395, 4405),
            _hour("2026-08-12T12:00:00", 4405, 4430, 4400, 4420),
        ),
    )
    result = evaluate_intraday_claim(claim, video, window, evaluated_at=_utc("2026-08-12T13:00:00"))
    assert result.entry_price == 4400
    assert result.final_close == 4420
    assert result.verdict is EvaluationVerdict.HIT
    assert result.window_start_at == _utc("2026-08-12T11:00:00")

def test_intraday_target_can_hit_before_deadline_but_cannot_miss_early():
    hit = evaluate_intraday_claim(target_4450, video, immature_window(high=4451), evaluated_at=now)
    miss_so_far = evaluate_intraday_claim(target_4500, video, immature_window(high=4490), evaluated_at=now)
    assert (hit.verdict, hit.mature) == (EvaluationVerdict.HIT, True)
    assert (miss_so_far.verdict, miss_so_far.mature, miss_so_far.reason) == (
        EvaluationVerdict.UNRESOLVED, False, "intraday_horizon_not_mature",
    )
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_claim_evaluation.py -k intraday -q`

Expected: FAIL because no intraday evaluator or timestamped result exists.

- [ ] **Step 3: Generalize OHLC judgment helpers by observation moment**

Define:

```python
ObservedBar = PriceBar | IntradayPriceBar
ObservationMoment = date | datetime

def _bar_moment(bar: ObservedBar) -> ObservationMoment:
    return bar.started_at if isinstance(bar, IntradayPriceBar) else bar.trade_date
```

Change `_judge`, `_judge_single_leg`, `_judge_hold`, `_judge_range`, `_judge_sequence`, `_judge_volatility`, and closest-point helpers to use `_bar_moment(bar)` instead of `bar.trade_date`. Keep all threshold arithmetic and `_NEAR_THRESHOLD = 0.005` unchanged.

- [ ] **Step 4: Implement intraday maturity rules and repository routing**

```python
if is_intraday_claim(claim):
    window = resolve_intraday_claim_window(
        claim, video, intraday_bars,
        next_same_instrument_prediction_at=cutoff,
        superseded_at=min(superseding_dates) if superseding_dates else None,
        evaluated_at=evaluated_at,
    )
    result = evaluate_intraday_claim(claim, video, window, evaluated_at=evaluated_at)
else:
    window = resolve_claim_window(
        claim, video, daily_bars,
        next_same_instrument_prediction_at=cutoff,
        superseded_at=min(superseding_dates) if superseding_dates else None,
        evaluated_at=evaluated_at,
    )
    result = evaluate_claim(
        claim, video, daily_bars, window, evaluated_at=evaluated_at
    )
```

For immature windows, run `_judge` only for early-hit types `{TARGET_TOUCH, CROSS_ABOVE, CROSS_BELOW, BREAKOUT_EITHER_SIDE, SEQUENCE}`. Persist an early `HIT`; otherwise return unresolved with `intraday_horizon_not_mature`. Direction, range, hold, and volatility mature only at the deadline.

- [ ] **Step 5: Run scoring, fact overlay, and recomputation regressions**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_claim_evaluation.py tests\test_fact_check_agent.py tests\test_recompute.py -q`

Expected: PASS; fact-check overlays continue clearing both date and datetime observation fields when a condition is not triggered or evidence is insufficient.

- [ ] **Step 6: Commit Task 4**

```powershell
git add goldbook/claim_evaluation.py tests/test_claim_evaluation.py
git commit -m "feat: score intraday claims on hourly bars"
```

---

### Task 5: Refresh composition and cached backfill

**Files:**
- Modify: `goldbook/__main__.py:170-220`
- Modify: `goldbook/recompute.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_recompute.py`

**Interfaces:**
- Consumes: daily source, `XausIntradayMarketDataSource`, database caches, and deterministic recomputation.
- Produces: `refresh-prices` refreshes both granularities and prints separate provider/count summaries; `recompute_claim_evaluations` reads both caches without model calls.

- [ ] **Step 1: Write refresh ordering and cache-preservation tests**

```python
def test_refresh_prices_caches_daily_and_hourly_before_recomputing(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(main_module, "_market_data_source", lambda _client: DailyFake(events))
    monkeypatch.setattr(main_module, "_intraday_market_data_source", lambda _client: HourlyFake(events))
    monkeypatch.setattr(main_module, "recompute_cached_outcomes", lambda db: events.append("legacy") or summary())
    monkeypatch.setattr(main_module, "recompute_claim_evaluations", lambda db, evaluated_at: events.append("claims") or claim_summary())
    assert main_module._refresh_prices(settings(tmp_path), database(tmp_path)) == 0
    assert events == ["daily-fetch", "hourly-fetch", "daily-save", "hourly-save", "legacy", "claims"]

def test_hourly_fetch_failure_preserves_existing_cache(tmp_path):
    db = database_with_one_hour(tmp_path)
    with pytest.raises(httpx.HTTPError):
        refresh_with_failing_hourly_source(db)
    assert len(db.list_intraday_price_bars()) == 1
```

- [ ] **Step 2: Run focused CLI/recompute tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_cli.py tests\test_recompute.py -k "hourly or refresh_prices" -q`

Expected: FAIL because refresh only fetches daily data.

- [ ] **Step 3: Compose hourly refresh and explicit output**

```python
hourly_source = XausIntradayMarketDataSource(client)
now = datetime.now(timezone.utc)
hourly_bars = hourly_source.fetch(now - timedelta(days=366), now)
database.upsert_intraday_prices(hourly_bars)
```

Fetch both sets before mutating either cache. If either fetch fails, return nonzero with the existing sanitized CLI error path and leave both caches untouched. After both succeed, save daily, save hourly, then recompute legacy outcomes and latest claim evaluations.

- [ ] **Step 4: Run CLI and recomputation suites**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_cli.py tests\test_recompute.py -q`

Expected: PASS and the refresh message contains exact daily/hourly counts and provider names.

- [ ] **Step 5: Commit Task 5**

```powershell
git add goldbook/__main__.py goldbook/recompute.py tests/test_cli.py tests/test_recompute.py
git commit -m "feat: refresh and backfill hourly claim prices"
```

---

### Task 6: Exact-time explanations and focused hourly chart

**Files:**
- Modify: `goldbook/web.py:660-850`
- Modify: `goldbook/templates/video.html`
- Modify: `goldbook/templates/claim_results.html`
- Modify: `goldbook/static/app.js:20-80`
- Modify: `goldbook/static/app.css`
- Test: `tests/test_claim_web.py`

**Interfaces:**
- Consumes: timestamped `ClaimEvaluation` and cached `IntradayPriceBar` values.
- Produces: Shanghai-time decision text and chart payload with `granularity: "1h"`, ISO timestamps, target levels, publication/entry/hit/deadline markers.

- [ ] **Step 1: Write Web tests for exact post-publication evidence**

```python
def test_video_page_explains_intraday_window_in_shanghai_time(client, seeded_intraday_claim):
    response = client.get(f"/creators/42/videos/{seeded_intraday_claim.bvid}")
    body = response.get_data(as_text=True)
    assert "视频发布：2026-08-12 18:35" in body
    assert "首根完整小时线：2026-08-12 19:00" in body
    assert "首次命中：2026-08-12 21:00" in body
    assert "连续两小时收盘" in body

def test_intraday_chart_never_contains_prepublication_hour(client, seeded_intraday_claim):
    body = client.get(f"/creators/42/videos/{seeded_intraday_claim.bvid}").get_data(as_text=True)
    payload = json.loads(re.search(r'id="claim-price-chart" data-chart=\'([^\']+)\'', body).group(1))
    assert payload["granularity"] == "1h"
    assert payload["prices"][0]["at"] == "2026-08-12T11:00:00+00:00"
    assert all(point["at"] != "2026-08-12T10:00:00+00:00" for point in payload["prices"])
```

- [ ] **Step 2: Run Web tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_claim_web.py -k intraday -q`

Expected: FAIL because templates only render date fields and `_claim_chart` only returns daily data.

- [ ] **Step 3: Add Shanghai formatting and timestamp-aware decision copy**

Register a helper using `ZoneInfo("Asia/Shanghai")` that renders `YYYY-MM-DD HH:mm`. `_claim_decision_steps` must explicitly state:

```text
视频于 2026-08-12 18:35 发布；10:00 UTC 小时线跨越发布时间而被排除。
首根完整小时线为 2026-08-12 19:00（上海时间），入场价 4400.00。
窗口截止 2026-08-12 22:00；首次命中 2026-08-12 21:00。
```

Use actual values from the model; do not hardcode the example.

- [ ] **Step 4: Build hourly chart payload and render it**

For intraday rows, `_claim_chart` queries `Database.list_intraday_price_bars` for the union of timestamped windows and emits:

```python
markers = [{"kind": "publication", "at": video.published_at.isoformat()}]
for kind, moment in (
    ("entry", evaluation.window_start_at),
    ("hit", evaluation.first_hit_at),
    ("deadline", evaluation.window_end_at),
):
    if moment is not None:
        markers.append({"kind": kind, "at": moment.isoformat()})

payload = {
    "granularity": "1h",
    "prices": [{"at": bar.started_at.isoformat(), "open": bar.open,
                "high": bar.high, "low": bar.low, "close": bar.close}],
    "markers": markers,
    "targets": [
        {"operator": leg.operator, "low": leg.level_low, "high": leg.level_high}
        for claim in claims for leg in claim.legs
    ],
}
```

Update `app.js` to use a time/category axis with formatted Shanghai labels for `at`, while leaving the current daily `date` branch unchanged. Change “横轴为交易日” to “横轴为上海时间（小时）” only for an hourly payload.

- [ ] **Step 5: Run all Web and JavaScript payload tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_claim_web.py tests\test_web.py -q`

Expected: PASS; production CSRF, secret isolation, daily charts, filters, and review forms remain intact.

- [ ] **Step 6: Commit Task 6**

```powershell
git add goldbook/web.py goldbook/templates/video.html goldbook/templates/claim_results.html goldbook/static/app.js goldbook/static/app.css tests/test_claim_web.py
git commit -m "feat: explain intraday claim results by exact hour"
```

---

### Task 7: Live hourly backfill and completion verification

**Files:**
- Modify: `README.md`
- Create: `docs/intraday-verification.md`
- Test: no new test file; execute the verified commands below against the local private database.

**Interfaces:**
- Consumes: the completed implementation and `data/goldbook.db`.
- Produces: refreshed hourly cache, recomputed current evaluations, a running loopback dashboard, and an evidence-backed count report.

- [ ] **Step 1: Run all automated verification before touching live data**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q goldbook tests
git diff --check
```

Expected: all tests pass, compile exits 0, and diff check reports no whitespace errors.

- [ ] **Step 2: Refresh daily and hourly caches with the local environment**

Load `.env` through the same validated `scripts/start.ps1` parsing rules without echoing values, then run:

```powershell
.\.venv\Scripts\python.exe -m goldbook refresh-prices --data-dir data
```

Expected: output identifies both the daily provider and `XAUS (xaus.com; Yahoo Finance proxy)`, reports a positive hourly count, and reports claim recomputation counts. No key or raw provider response is printed.

- [ ] **Step 3: Audit intraday results by creator**

Run a read-only SQLite audit that counts latest intraday claims and groups them by creator UID, verdict, maturity, and reason. Record the literal output in `docs/intraday-verification.md`, including:

- total intraday claims;
- evaluated hit/partial/miss counts;
- `intraday_horizon_not_mature` count;
- `unresolved_intraday_data` count;
- minimum and maximum cached hourly timestamps;
- count of evaluations whose first eligible hour precedes publication, which must equal zero.

- [ ] **Step 4: Restart only the exact loopback service and smoke-test pages**

Resolve the PID listening on `127.0.0.1:8765`, stop only that PID, restart `scripts/start.ps1` hidden, and request:

```text
/
/creators/546630884
/creators/1847287889
/creators/546630884/claims
/creators/1847287889/claims
/api/status
```

Expected: every route returns HTTP 200; at least one intraday video page displays publication, first complete hour, deadline, and hit/miss reasoning; no response contains `MINIMAX_API_KEY` or a key prefix.

- [ ] **Step 5: Verify database integrity and absence of active jobs/media residue**

Run `PRAGMA integrity_check`, count jobs in `pending/running/paused`, and recursively scan the project task temp directory for `.wav`, `.m4a`, `.mp3`, or `.mp4`. Expected: `ok`, zero active jobs after refresh/recompute, and no media residue.

- [ ] **Step 6: Document operation and commit Task 7**

Add to README that daily claims use complete post-publication daily bars, intraday claims use complete post-publication hourly bars, and `refresh-prices` refreshes both. Then commit only the documentation and verification artifact:

```powershell
git add README.md docs/intraday-verification.md
git commit -m "docs: verify intraday claim backfill"
```

---

## Final acceptance checklist

- [ ] Every current intraday claim is routed to hourly evaluation rather than the daily skip branch.
- [ ] No hourly bar crossing or preceding publication is eligible.
- [ ] Immature forecasts cannot be marked miss; eligible target hits may mature early.
- [ ] Daily evaluation outputs are byte-for-byte compatible at the repository boundary.
- [ ] Both creator pages show exact-time explanations and focused hourly charts.
- [ ] Live backfill reports zero timestamp violations and does not call M3.
- [ ] Full pytest, compileall, diff check, SQLite integrity, loopback HTTP, secret scan, and media-residue checks pass.
