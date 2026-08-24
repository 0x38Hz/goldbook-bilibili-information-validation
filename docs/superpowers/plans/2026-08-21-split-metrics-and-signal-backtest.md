# 分项能力与 100 美元信号账户 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修正日内期限展示，分别统计方向与点位能力，并为每个 UP 主生成从 100 美元开始、严格按发布时间变仓的单账户回测和清晰判定链。

**Architecture:** `claim_metrics.py` 扩展为分组聚合但保留原总体指标；新建无数据库依赖的 `backtest.py` 纯计算模块；`web.py` 只读取数据库、组织判定链和调用纯函数；模板和 Chart.js 展示分组指标、账户曲线与逐笔明细。现有 SQLite schema 不需要迁移。

**Tech Stack:** Python 3.12、dataclasses、Flask/Jinja2、SQLite、Chart.js、pytest。

**Spec:** `docs/superpowers/specs/2026-08-21-split-metrics-and-signal-backtest-design.md`

## Global Constraints

- 所有入场必须使用视频发布后的首个完整交易日开盘价，不得使用同日价格。
- 中性、无信号和方向不明确在模拟中按做空处理。
- 同一入场交易日只采用发布时间最晚的视频。
- 初始资金固定 100 美元，1 倍全仓，不计手续费、滑点、融资成本和杠杆。
- 被排除、纯新闻和回顾性分析不得生成交易。
- 分组能力采用视频等权；命中 1 分、接近 0.5 分、未命中 0 分。
- 不增加网络请求，不读取或输出 MiniMax key。

---

### Task 1: 日内期限与不可评价说明

**Files:**
- Modify: `goldbook/web.py`
- Test: `tests/test_claim_web.py`

**Interfaces:**
- Consumes: `ForecastClaim.horizon_min_trading_days`, `horizon_point_trading_days`, `horizon_max_trading_days` 和 `ClaimEvaluation.reason`。
- Produces: `_format_claim_horizon(claim) -> str` 与 `_claim_explanation(claim, evaluation) -> str` 的新展示语义。

- [ ] **Step 1: Write the failing tests**

```python
def test_intraday_horizon_is_described_without_zero_trading_days():
    claim = replace(base_claim, horizon_min_trading_days=0,
                    horizon_point_trading_days=0,
                    horizon_max_trading_days=1,
                    horizon_text="今天晚上")
    assert _format_claim_horizon(claim) == "日内/次一交易日（需要发布后的匹配行情）"

def test_awaiting_first_bar_explains_that_same_day_data_cannot_be_used():
    text = _claim_explanation(claim, awaiting_evaluation)
    assert "并非没有黄金价格" in text
    assert "发布后的首根完整日线" in text
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests\test_claim_web.py -k "intraday_horizon or awaiting_first_bar" -q`

Expected: FAIL because the old formatter renders `0 个交易日（0–1）` and the old explanation only says no complete bar.

- [ ] **Step 3: Implement the display semantics**

```python
if point == 0 or minimum == 0:
    return "日内/次一交易日（需要发布后的匹配行情）"
```

Map `awaiting_first_complete_bar` to a sentence explaining that same-publication-day OHLC is rejected to prevent look-ahead and that the first later complete daily bar is still awaited. Keep `unresolved_intraday_data` separate and name the missing hourly/minute granularity.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests\test_claim_web.py -q`

Expected: all claim web tests PASS.

---

### Task 2: 方向与点位能力独立聚合

**Files:**
- Modify: `goldbook/claim_metrics.py`
- Test: `tests/test_claim_metrics.py`

**Interfaces:**
- Consumes: existing `VideoClaimMetrics` values and claim/evaluation pairs.
- Produces: `MetricBreakdown(total_claim_count, scoreable_count, score, exact_hit_rate, near_inclusive_rate, verdict_counts)`; new fields `directional_metrics` and `point_metrics` on both video and creator metrics.

- [ ] **Step 1: Write failing grouping tests**

```python
def test_direction_and_point_scores_are_independent():
    video = aggregate_video_claims("BV", [direction_claim, target_claim],
        [miss(direction_claim), hit(target_claim)])
    assert video.directional_metrics.score == 0.0
    assert video.point_metrics.score == 1.0
    assert video.directional_metrics.total_claim_count == 1
    assert video.point_metrics.total_claim_count == 1

def test_creator_breakdowns_remain_video_equal():
    metrics = aggregate_creator_claims([video_with_many_points, video_with_one_point])
    assert metrics.point_metrics.score == pytest.approx(0.5)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests\test_claim_metrics.py -q`

Expected: FAIL because `directional_metrics` and `point_metrics` do not exist.

- [ ] **Step 3: Add focused breakdown aggregation**

Define `_POINT_TYPES` as every executable non-`DIRECTIONAL_MOVE` type, including `RANGE`, `SEQUENCE`, and `BREAKOUT_EITHER_SIDE`. Build breakdowns from each group using the same `_SCORES`, then aggregate creator breakdown scores by averaging each scored video's group score rather than pooling claim counts.

- [ ] **Step 4: Verify focused and compatibility tests**

Run: `.venv\Scripts\python.exe -m pytest tests\test_claim_metrics.py tests\test_claim_web.py -q`

Expected: PASS with legacy overall fields unchanged.

---

### Task 3: 无未来数据的 100 美元账户引擎

**Files:**
- Create: `goldbook/backtest.py`
- Create: `tests/test_backtest.py`

**Interfaces:**
- Consumes: `backtest_creator(videos: Sequence[Video], analyses: Mapping[str, SignalAnalysis], bars: Sequence[PriceBar], initial_balance: float = 100.0) -> BacktestResult`.
- Produces: `BacktestTrade`, `EquityPoint`, and `BacktestResult` dataclasses. `BacktestResult` exposes `initial_balance`, `final_balance`, `total_return`, `max_drawdown`, `long_count`, `short_count`, `default_short_count`, `signal_count`, `trades`, and `equity_curve`.

- [ ] **Step 1: Write failing pure-function tests**

```python
def test_long_then_short_compounds_one_account_without_lookahead():
    result = backtest_creator([bull_video, bear_video], analyses, bars)
    assert result.trades[0].entry_date > bull_video.published_at.date()
    assert result.trades[0].direction is Direction.BULLISH
    assert result.trades[1].direction is Direction.BEARISH
    assert result.final_balance == pytest.approx(108.0)

def test_neutral_defaults_to_short_and_same_entry_day_uses_latest_video():
    result = backtest_creator([early_neutral, late_bullish], analyses, bars)
    assert result.signal_count == 1
    assert result.trades[0].bvid == late_bullish.bvid
    assert result.default_short_count == 0

def test_no_signal_keeps_one_hundred_dollars():
    result = backtest_creator([], {}, bars)
    assert result.final_balance == 100.0
    assert result.trades == ()
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests\test_backtest.py -q`

Expected: collection ERROR because `goldbook.backtest` is absent.

- [ ] **Step 3: Implement immutable account calculations**

For each eligible video, select the first `PriceBar.trade_date` strictly after its Shanghai publication date. Deduplicate signals by entry date, keeping the latest `published_at`. Close each segment at the next signal's entry open; close the final segment at the final bar close. Multiply balance by `1 + signed_return`, append a point after each close, and calculate maximum drawdown from the running equity peak.

Reject `initial_balance <= 0`, non-positive OHLC values, duplicate trade dates, and input bars not strictly increasing. Exclude analyses with `ReviewStatus.EXCLUDED`, `is_news_only`, or `is_retrospective`.

- [ ] **Step 4: Run pure tests and verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests\test_backtest.py -q`

Expected: all backtest tests PASS.

---

### Task 4: 可核验判定链与 UP 主回测页面

**Files:**
- Modify: `goldbook/web.py`
- Modify: `goldbook/templates/creator.html`
- Modify: `goldbook/templates/claim_results.html`
- Modify: `goldbook/templates/video.html`
- Modify: `goldbook/static/app.js`
- Modify: `goldbook/static/app.css`
- Test: `tests/test_claim_web.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `CreatorClaimMetrics.directional_metrics`, `.point_metrics`, and `backtest_creator(...)`.
- Produces: creator template contexts `backtest`, `backtest_chart`; claim row context `decision_steps` containing four labeled strings: `requirement`, `market_data`, `comparison`, `conclusion`.

- [ ] **Step 1: Write failing Web behavior tests**

```python
def test_creator_page_separates_direction_and_point_ability():
    body = client.get("/creators/42").get_data(as_text=True)
    assert "方向能力" in body and "点位能力" in body
    assert "100 美元信号账户" in body
    assert "最终余额" in body and "最大回撤" in body

def test_claim_page_shows_auditable_decision_chain():
    body = client.get("/creators/42/claims?verdict=hit").get_data(as_text=True)
    assert "预测要求" in body
    assert "使用行情" in body
    assert "阈值比较" in body
    assert "最终结论" in body
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests\test_claim_web.py tests\test_web.py -q`

Expected: FAIL because the split panels, account and decision-chain labels are missing.

- [ ] **Step 3: Assemble view models and templates**

Create `_claim_decision_steps(claim, evaluation)` with numeric statements such as `实际最高 4371.50 >= 上方阈值 4200.00` and dates for first hit. For misses, show the closest observed price and the exact remaining distance. For directional claims, show entry open, final close, signed change, and required sign. For unresolved claims, show the rejected same-day data rule or missing granularity.

Call `backtest_creator` from `creator_detail`, serialize `equity_curve` for Chart.js, render split metric cards, account headline metrics, disclosure text, curve and trade table. Add a dedicated `#backtest-chart` renderer with USD balance axis and sparse date ticks.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests\test_claim_web.py tests\test_web.py tests\test_backtest.py -q`

Expected: PASS.

---

### Task 5: 数据重算、完整验证与服务切换

**Files:**
- Modify only if a regression demands it: files from Tasks 1–4

**Interfaces:**
- Consumes: complete implementation.
- Produces: refreshed local pages for creators `1847287889` and `546630884`.

- [ ] **Step 1: Run all automated verification**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m compileall -q goldbook tests
git diff --check
```

Expected: pytest PASS, compileall exit 0, diff check exit 0 apart from existing CRLF warnings.

- [ ] **Step 2: Restart only this workspace's Goldbook process**

Identify the Python process whose executable is the workspace `.venv` and whose start chain runs `scripts/start.ps1`; terminate only that process chain, then launch `scripts/start.ps1` hidden with stdout/stderr redirected under `data/`.

- [ ] **Step 3: Run local HTTP smoke checks**

Request both creator pages, hit/miss/unresolved claim filters, and `BV1m68H6JEg9`. Assert HTTP 200, no `RuntimeError`, presence of split metrics, account results, decision-chain labels, `日内/次一交易日`, USD axes, and security headers.

- [ ] **Step 4: Report the two computed account outcomes**

Read the final rendered values for each UP and report initial balance, final balance, cumulative return, maximum drawdown, signal count, and the no-cost/no-slippage limitation. Do not describe the simulation as investment advice or realizable profit.
