# 主要趋势提取与多空黄金叠加图 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为每个视频提取一条有字幕证据的主要趋势，重新评价全部历史趋势，并在 100 美元账户图中叠加真实黄金价格及明确多空背景。

**Architecture:** MiniMax 返回独立 `primary_trend` 和点位 `claims`，解析器将主要趋势转换为 index 0 的 `directional_move`，点位观点从 index 1 开始；趋势窗口、指标与页面只把这个稳定位置视为主要趋势。回测仍由纯 Python 计算，Web 只序列化余额、XAU/USD 与仓位区间，Chart.js 自定义插件绘制多空背景。

**Tech Stack:** Python 3.12、Flask/Jinja2、SQLite、httpx、MiniMax-M3、Chart.js、pytest。

**Spec:** `docs/superpowers/specs/2026-08-21-primary-trend-extraction-and-overlay-design.md`

## Global Constraints

- 每个成功提取的视频恰好一个 `primary_trend`；趋势与点位不能互相替代。
- 看涨/看跌/中性趋势必须有可定位的逐字字幕证据；纯资讯 `no_signal` 可无证据但不得进入方向命中率。
- 中性和无信号不能因回测的默认做空规则被改写成 bearish。
- 未明确期限的趋势截止到同一 UP 下一条主要趋势发布时间，不默认 20 天。
- 只使用视频北京时间发布日之后的完整行情，禁止使用同日已发生数据。
- 历史重分析只使用本地转写，不重新下载媒体；使用 MiniMax-M3，初始并发 5，429 按现有机制退避。
- 重分析前创建 SQLite 备份；失败视频保留旧修订。
- API key 不得出现在测试、日志、页面、命令输出或报告中。
- 当前工作区包含用户拥有的未提交修改；所有文件编辑使用 `apply_patch`，不自动执行合并、推送、重置或批量暂存。

---

### Task 1: MiniMax 主要趋势协议与严格解析

**Files:**
- Modify: `goldbook/minimax.py`
- Modify: `goldbook/claim_pipeline.py`
- Test: `tests/test_claim_extraction.py`
- Test: `tests/test_minimax.py`

**Interfaces:**
- Consumes: `MiniMaxClient.extract_claims(video, segments, revision, transcript_hash)` 的现有请求路径。
- Produces: `ClaimExtraction.claims` 中 index 0 为主要趋势 `ForecastClaim`; `CLAIM_PROMPT_VERSION = "claims-v2-primary-trend"`; 点位观点从 index 1 开始。

- [ ] **Step 1: Write failing parser tests**

```python
def test_primary_trend_is_first_and_point_claims_follow():
    payload = {
        "summary": "偏多，关注4700",
        "primary_trend": {
            "direction": "bullish",
            "condition_text": "回调继续看涨",
            "horizon_text": "短期",
            "horizon_source": "context_inferred",
            "horizon_min_trading_days": 1,
            "horizon_point_trading_days": 2,
            "horizon_max_trading_days": 3,
            "deadline_at": None,
            "time_confidence": 0.8,
            "confidence": 0.9,
            "evidence": [{"start_sec": 1, "end_sec": 3, "quote": "回调继续看涨"}],
            "status": "auto_validated",
        },
        "claims": [target_claim_payload],
    }
    result = parse_claim_response(json.dumps(payload), video, segments, 2, "hash")
    assert result.claims[0].claim_type is ClaimType.DIRECTIONAL_MOVE
    assert result.claims[0].direction is Direction.BULLISH
    assert result.claims[0].claim_index == 0
    assert result.claims[1].claim_type is ClaimType.TARGET_TOUCH
```

Add separate tests proving an evidence-backed neutral trend stays neutral, `no_signal` may be unresolved with empty evidence, bullish without locatable evidence is rejected, and a point claim cannot use index 0.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests\test_claim_extraction.py tests\test_minimax.py -q`

Expected: FAIL because the old top-level schema accepts only `summary` and `claims`, and prompt version is still `claims-v1`.

- [ ] **Step 3: Implement the v2 schema and parser**

Change the prompt to require top-level exact keys `summary`, `primary_trend`, `claims`. Parse the trend with a dedicated `_parse_primary_trend(...)` that constructs:

```python
ForecastClaim(
    claim_id=f"{video.bvid}:{revision}:0",
    claim_index=0,
    instrument=Instrument.XAU_USD_SPOT,
    claim_type=ClaimType.DIRECTIONAL_MOVE,
    direction=parsed_direction,
    legs=(),
    ...,
)
```

For `bullish|bearish|neutral`, require at least one locatable evidence quote. For `no_signal`, require `status=unresolved`, allow empty evidence, and force unknown horizon fields to `None`. Parse point claims with offsets beginning at 1 and reject point-level `directional_move` so each video cannot produce duplicate primary trends.

Update the Chinese prompt with explicit low-buy/high-sell/strong/weak examples and the rule that neutral/default-short are distinct. Update `_summary_analysis` to use claim index 0 for the video direction rather than combining point claim directions.

- [ ] **Step 4: Run extraction suites and verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests\test_claim_extraction.py tests\test_minimax.py tests\test_claim_pipeline.py -q`

Expected: PASS; older v1 fixture tests are updated to include an explicit primary trend rather than weakening exact-key validation.

---

### Task 2: 主要趋势时间窗口、评价与方向指标

**Files:**
- Modify: `goldbook/claim_time.py`
- Modify: `goldbook/claim_evaluation.py`
- Modify: `goldbook/claim_metrics.py`
- Test: `tests/test_claim_time.py`
- Test: `tests/test_claim_evaluation.py`
- Test: `tests/test_claim_metrics.py`

**Interfaces:**
- Consumes: index 0 `directional_move` as the primary trend.
- Produces: `is_primary_trend(claim) -> bool`; neutral/no-signal trend evaluation returns `UNRESOLVED` with `neutral_trend` or `no_signal`; creator direction breakdown counts only primary bullish/bearish trends.

- [ ] **Step 1: Write failing trend-window and evaluation tests**

```python
def test_unknown_primary_trend_ends_before_next_primary_trend():
    cutoff = find_next_same_instrument_prediction(first_trend, first_video,
        videos, [first_trend, unrelated_point, next_trend])
    assert cutoff == next_video.published_at

def test_neutral_primary_trend_is_not_scored_as_a_miss():
    result = evaluate_claim(neutral_trend, video, bars, mature_window,
                            evaluated_at=NOW)
    assert result.verdict is EvaluationVerdict.UNRESOLVED
    assert result.reason == "neutral_trend"
```

Add metrics tests where a point claim with bullish direction does not enter `directional_metrics`, while primary bullish hit and primary bearish miss do.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests\test_claim_time.py tests\test_claim_evaluation.py tests\test_claim_metrics.py -q`

Expected: FAIL because next-prediction lookup currently sees any same-instrument claim and neutral directional claims are treated as misses.

- [ ] **Step 3: Implement primary-trend semantics**

Add a shared predicate in `claim_time.py`:

```python
def is_primary_trend(claim: ForecastClaim) -> bool:
    return claim.claim_index == 0 and claim.claim_type is ClaimType.DIRECTIONAL_MOVE
```

When the current claim is primary, `find_next_same_instrument_prediction` considers only later primary trends. In `evaluate_claim`, return unresolved before `_judge` when the primary direction is `NEUTRAL` or `NO_SIGNAL`. In `claim_metrics.py`, directional breakdown filters with `is_primary_trend` and explicit bullish/bearish direction; point breakdown remains unchanged.

- [ ] **Step 4: Run all claim-domain tests and verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests\test_claim_time.py tests\test_claim_evaluation.py tests\test_claim_metrics.py tests\test_claim_web.py -q`

Expected: PASS with point-level scores unchanged.

---

### Task 3: 趋势覆盖、类型筛选与数字判定链

**Files:**
- Modify: `goldbook/web.py`
- Modify: `goldbook/templates/creator.html`
- Modify: `goldbook/templates/claim_results.html`
- Modify: `goldbook/templates/video.html`
- Test: `tests/test_claim_web.py`

**Interfaces:**
- Consumes: `is_primary_trend`, trend evaluations and split metrics.
- Produces: result query `kind=all|trend|price_level`; template fields `trend_coverage`, `row.kind_label`; trend decision chain includes entry price, final close and signed percentage.

- [ ] **Step 1: Write failing Web tests**

```python
def test_creator_discloses_primary_trend_coverage():
    body = client.get("/creators/42").get_data(as_text=True)
    assert "趋势覆盖" in body
    assert "1 / 1 个视频" in body

def test_results_filter_trend_separately_from_price_levels():
    trend = client.get("/creators/42/claims?kind=trend").get_data(as_text=True)
    levels = client.get("/creators/42/claims?kind=price_level").get_data(as_text=True)
    assert "主要趋势" in trend and "目标点位" not in trend
    assert "目标点位" in levels and "主要趋势" not in levels
```

Add a trend hit page assertion containing literal labels `首日开盘`, `截止收盘`, `实际变化`, and `要求看涨`.

- [ ] **Step 2: Run Web tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests\test_claim_web.py -q`

Expected: FAIL because `kind` is not accepted and coverage/labels do not exist.

- [ ] **Step 3: Implement view models and templates**

Validate `kind`; filter trend with `is_primary_trend` and price level as its inverse. Calculate coverage from videos with an index 0 trend. In `_claim_decision_steps`, render explicit trend comparison:

```text
首日开盘 4028.30 → 截止收盘 4100.10；实际变化 +1.78%；要求看涨（变化 > 0）。
```

Place the primary trend card before point claims on video pages. Add trend/point tabs without removing verdict filters.

- [ ] **Step 4: Run Web and compatibility tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_claim_web.py tests\test_web.py -q`

Expected: PASS.

---

### Task 4: 账户余额、真实黄金与三类仓位叠加图

**Files:**
- Modify: `goldbook/backtest.py`
- Modify: `goldbook/web.py`
- Modify: `goldbook/static/app.js`
- Modify: `goldbook/static/app.css`
- Modify: `goldbook/templates/creator.html`
- Test: `tests/test_backtest.py`
- Test: `tests/test_claim_web.py`

**Interfaces:**
- Consumes: `BacktestTrade.default_short`, trade entry/exit dates, daily price bars and equity curve.
- Produces: `BacktestPositionSegment(start_date, end_date, kind, bvid, title, direction_label, stage_return)`; `BacktestResult.position_segments`; Web chart JSON keys `equity`, `gold`, `positions`, `axes`.

- [ ] **Step 1: Write failing overlay-data tests**

```python
def test_position_segments_distinguish_explicit_and_default_shorts():
    result = backtest_creator(videos, analyses, bars)
    assert [segment.kind for segment in result.position_segments] == [
        "long", "short", "default_short"
    ]

def test_creator_chart_contains_equity_gold_and_position_backgrounds():
    payload = extract_chart_json(client.get("/creators/42"))
    assert payload["equity"]
    assert payload["gold"]
    assert {row["kind"] for row in payload["positions"]} >= {"long", "default_short"}
```

- [ ] **Step 2: Run backtest/Web tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests\test_backtest.py tests\test_claim_web.py -q`

Expected: FAIL because position segments and gold overlay data are absent.

- [ ] **Step 3: Implement overlay data and Chart.js rendering**

Create one position segment per trade. Serialize every cached bar as `{date, close}` and every equity point as `{date, balance}`. Add a Chart.js plugin whose `beforeDraw` resolves segment dates against x labels and paints:

- `rgba(22,128,93,.10)` for long;
- `rgba(189,63,58,.10)` for explicit short;
- a red translucent fill plus diagonal strokes for default short.

Render account balance on left axis `yBalance` and XAU/USD on right axis `yGold`; tooltip callbacks include position label and source title. Add a visible legend block explaining all three backgrounds and both lines.

- [ ] **Step 4: Run overlay and JavaScript checks**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_backtest.py tests\test_claim_web.py -q
node --check goldbook\static\app.js
```

Expected: tests PASS and Node exits 0.

---

### Task 5: 全量 M3 趋势重分析与安全持久化

**Files:**
- Modify: `goldbook/claim_pipeline.py` only if batch reporting needs v2 trend counts
- Modify: `goldbook/__main__.py` only if CLI output needs trend coverage
- Test: `tests/test_claim_pipeline.py`
- Test: `tests/test_cli.py`
- Data: `data/goldbook.db` after creating a timestamped sibling backup

**Interfaces:**
- Consumes: v2 parser, existing `reanalyse-claims` CLI, local transcripts, `.env` MiniMax-M3 settings.
- Produces: a new analysis revision for each successful video and a summary with `total`, `completed`, `skipped`, `failed`, `primary_trends`, `bullish`, `bearish`, `neutral`, `no_signal`.

- [ ] **Step 1: Write failing reporting/idempotence tests**

```python
def test_v2_backfill_reports_one_primary_trend_per_completed_video():
    summary = reanalyse_cached_claims(db, fake_client, evaluated_at=NOW)
    assert summary.primary_trends == summary.completed
    assert summary.bullish + summary.bearish + summary.neutral + summary.no_signal == summary.completed
```

Also prove a provider failure leaves the previous latest claims queryable and a repeated v2 run skips completed transcript/model/prompt identities.

- [ ] **Step 2: Run pipeline tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests\test_claim_pipeline.py tests\test_cli.py -q`

Expected: FAIL because summary lacks trend counters.

- [ ] **Step 3: Implement counters and backup the live database**

Count primary trends only from successful v2 results. Before the live command, create `data/goldbook-before-primary-trends-<timestamp>.db` with `Copy-Item -LiteralPath`; verify source and destination byte sizes are positive. Do not print `.env` or API values.

- [ ] **Step 4: Run live cached-transcript reanalysis**

Set the configured concurrency to 5 for the first batch. Run `.venv\Scripts\python.exe -m goldbook reanalyse-claims` using the existing local `.env`. Monitor sanitized progress only. If 429 occurs, reduce configured concurrency and resume; do not repeat already persisted v2 identities. Do not download audio or Whisper weights.

- [ ] **Step 5: Recompute and audit live data**

Verify exactly 99 latest video analyses exist, count v2 primary trends and directions, confirm every completed v2 video has one index 0 trend, confirm no neutral/no-signal trend is scored as hit/miss, and report failures without deleting old revisions.

---

### Task 6: 完整验证与本地服务切换

**Files:**
- Modify only files from Tasks 1–5 if a newly reproduced regression requires a tested fix.

**Interfaces:**
- Consumes: completed code and live v2 database.
- Produces: verified pages for creators `1847287889`, `546630884`, their trend/point result filters and overlaid signal accounts.

- [ ] **Step 1: Run complete verification**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m compileall -q goldbook tests
node --check goldbook\static\app.js
git diff --check
```

Expected: all tests pass, compile and Node exit 0, diff check has no errors beyond existing CRLF warnings.

- [ ] **Step 2: Verify secrets and media hygiene**

Scan tracked/project source while excluding `.env`, SQLite and backup files. Confirm no `sk-` value, raw provider response, downloaded media, audio, or video residue is present.

- [ ] **Step 3: Safely restart the verified 8765 service**

Resolve the `127.0.0.1:8765` listener, verify its command line and parent chain point to this workspace `.venv` and `scripts/start.ps1`, stop only that verified chain, and start the current script hidden with redirected logs.

- [ ] **Step 4: Run live HTTP and data smoke checks**

Request both creator pages, `kind=trend`, `kind=price_level`, verdict filters, representative video pages and `/api/status`. Assert HTTP 200, no `RuntimeError`, no key fields, security headers present, primary trend coverage visible, numerical decision chain visible, gold/right axis visible, and all three position legend states rendered when present.

- [ ] **Step 5: Report final evidence**

Report trend coverage before/after, bullish/bearish/neutral/no-signal counts, mature trend hit/miss counts, provider failures, both final simulated balances and the explicit limitation that default shorts are strategy assumptions rather than author bearish predictions.
