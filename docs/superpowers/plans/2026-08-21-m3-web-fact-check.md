# M3 Web Fact Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a reusable M3-driven web fact-check workflow that activates only for externally conditioned predictions, preserves cited search evidence, selects the applicable branch, and evaluates only triggered price claims.

**Architecture:** A pure domain layer identifies external conditions and validates cited results. A bounded client calls the MiniMax generic search endpoint used by its official MCP, while M3 orchestrates the search workflow in a background job. SQLite stores auditable runs, evidence, results, and claim decisions; the existing evaluation and Web layers consume only the latest result for the current analysis revision.

**Tech Stack:** Python 3.12, dataclasses/enums, SQLite, httpx, MiniMax-M3, MiniMax Coding Plan search, Flask/Jinja, pytest.

**Spec:** `docs/superpowers/specs/2026-08-21-m3-web-fact-check-design.md`

## Global Constraints

- Start a web fact check only when a forecast depends on an external event or unpublished fact.
- Use the existing `MINIMAX_API_KEY`; never persist, log, render, or return it.
- Allow no more than 6 searches per run and no more than 3 independent searches concurrently.
- Every factual conclusion must cite saved HTTP(S) evidence; block loopback, private-network, `file:`, and non-HTTP(S) URLs.
- `not_triggered`, `conflicting`, and `insufficient` outcomes never count as forecast misses.
- Ordinary trends and unconditional point claims keep their current offline evaluation path.
- Network work runs only in the background worker; Web requests only enqueue work.
- Tests use deterministic fake M3/search transports. A live probe is separate and cannot substitute for tests.

---

### Task 1: Fact-check domain model and activation gate

**Files:**
- Create: `goldbook/fact_check.py`
- Modify: `goldbook/models.py`
- Create: `tests/test_fact_check.py`

**Interfaces:**
- Consumes: `Video`, `ForecastClaim`, `TranscriptSegment`, `Direction` from `goldbook.models`.
- Produces: `FactCheckNeed`, `FactCheckImpact`, `FactCheckStatus`, `BranchPredicate`, `BranchDecision`, `SearchEvidence`, `FactCheckResult`, `detect_fact_check_need(...)`, `validate_fact_check_result(...)`, and `predicate_matches(...)`.

- [ ] **Step 1: Write failing gate and branch-semantic tests**

```python
def test_gate_activates_only_for_external_conditional_claims():
    need = detect_fact_check_need(video, (cpi_claim,), transcript)
    assert need.required is True
    assert need.event_description == "今晚CPI数据"
    assert need.claim_ids == (cpi_claim.claim_id,)

    ordinary = detect_fact_check_need(video, (plain_target_claim,), transcript)
    assert ordinary.required is False


def test_not_supportive_includes_neutral_but_adverse_does_not():
    assert predicate_matches(BranchPredicate.NOT_SUPPORTIVE, FactCheckImpact.NEUTRAL)
    assert not predicate_matches(BranchPredicate.ADVERSE, FactCheckImpact.NEUTRAL)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_fact_check.py -q`

Expected: collection fails because `goldbook.fact_check` does not exist.

- [ ] **Step 3: Implement immutable types, deterministic gate, URL validation, and predicates**

```python
class FactCheckImpact(str, Enum):
    SUPPORTIVE = "supportive"
    ADVERSE = "adverse"
    NEUTRAL = "neutral"
    CONFLICTING = "conflicting"
    INSUFFICIENT = "insufficient"


class BranchPredicate(str, Enum):
    SUPPORTIVE = "supportive"
    ADVERSE = "adverse"
    NOT_SUPPORTIVE = "not_supportive"


@dataclass(frozen=True)
class FactCheckNeed:
    required: bool
    event_description: str | None
    expected_start: datetime | None
    expected_end: datetime | None
    claim_ids: tuple[str, ...]
    reason: str


def predicate_matches(predicate: BranchPredicate, impact: FactCheckImpact) -> bool:
    if predicate is BranchPredicate.NOT_SUPPORTIVE:
        return impact in {FactCheckImpact.ADVERSE, FactCheckImpact.NEUTRAL}
    return predicate.value == impact.value
```

The gate must require conditional language plus an external-event term found in claim text or locatable transcript evidence. It must not classify technical-analysis terms such as support, resistance, Bollinger bands, or moving averages as external facts.

- [ ] **Step 4: Add evidence-validation tests and make them green**

```python
@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "http://127.0.0.1:8765/api/status",
    "http://10.0.0.1/private",
    "http://169.254.169.254/latest/meta-data",
])
def test_validator_rejects_non_public_evidence_urls(url):
    with pytest.raises(FactCheckValidationError):
        validate_search_evidence(SearchEvidence(
            evidence_id="e1", query="q", title="title", url=url,
            domain="example", published_at=None, snippet="fact", fetched_at=NOW,
        ))
```

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_fact_check.py -q`

Expected: all Task 1 tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add goldbook/fact_check.py goldbook/models.py tests/test_fact_check.py
git commit -m "feat: model externally conditioned fact checks"
```

### Task 2: Bounded MiniMax web-search client

**Files:**
- Create: `goldbook/minimax_search.py`
- Create: `tests/test_minimax_search.py`

**Interfaces:**
- Consumes: `MINIMAX_API_KEY` and `MINIMAX_API_HOST` from server-side settings.
- Produces: `SearchResult`, `WebSearchProvider.search(query: str) -> tuple[SearchResult, ...]`, `MiniMaxWebSearchClient`, and `SearchProviderError`.

- [ ] **Step 1: Write fake-transport tests for protocol, limits, and redaction**

```python
def test_client_calls_official_search_endpoint_and_normalizes_results():
    transport = FakeHttpTransport({
        "organic": [{"title": "CPI release", "link": "https://example.com/cpi", "snippet": "actual 0.1%"}],
        "base_resp": {"status_code": 0, "status_msg": "success"},
    })
    client = MiniMaxWebSearchClient("https://api.minimaxi.com/v1", "secret", http_client=transport.client)
    assert client.search("2026 August US CPI actual forecast")[0].title == "CPI release"
    assert transport.paths == ["/v1/coding_plan/search"]


def test_search_error_never_contains_key_or_provider_body():
    client = MiniMaxWebSearchClient("https://api.minimaxi.com/v1", "sk-private", http_client=FailingClient("sk-private raw body"))
    with pytest.raises(SearchProviderError, match="search provider unavailable") as error:
        client.search("query")
    assert "sk-private" not in str(error.value)
```

- [ ] **Step 2: Run Task 2 tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_minimax_search.py -q`

Expected: collection fails because `goldbook.minimax_search` does not exist.

- [ ] **Step 3: Implement the official search HTTP boundary with an injected client**

```python
class WebSearchProvider(Protocol):
    def search(self, query: str) -> tuple[SearchResult, ...]:
        raise NotImplementedError


class MiniMaxWebSearchClient:
    def __init__(self, base_url: str, api_key: str, *, http_client: httpx.Client) -> None:
        if not api_key:
            raise ValueError("MINIMAX_API_KEY is required for web search")
        self._url = f"{base_url.rstrip('/')}/coding_plan/search"
        self._api_key = api_key
        self._http_client = http_client

    def search(self, query: str) -> tuple[SearchResult, ...]:
        normalized = " ".join(query.split())
        if not normalized or len(normalized) > 300:
            raise ValueError("search query must contain 1 to 300 characters")
        response = self._http_client.post(self._url, json={"q": normalized}, timeout=20.0)
        return _parse_search_payload(response.json())
```

The production client copies the official MCP's `Authorization: Bearer` and `MM-API-Source` behavior, applies a 20-second deadline, performs at most one retry for transport/429/5xx failures, and emits only fixed safe errors.

- [ ] **Step 4: Run Task 2 tests and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_minimax_search.py -q`

Expected: all focused tests pass.

```powershell
git add goldbook/minimax_search.py tests/test_minimax_search.py
git commit -m "feat: add bounded MiniMax web search client"
```

### Task 3: M3 search agent and cited-result parser

**Files:**
- Create: `goldbook/fact_check_agent.py`
- Create: `tests/test_fact_check_agent.py`
- Modify: `goldbook/minimax.py`

**Interfaces:**
- Consumes: `FactCheckNeed`, current claims, transcript excerpts, `WebSearchProvider`, and `MiniMaxClient.complete_with_tools(messages, tools)`.
- Produces: `M3FactCheckAgent.run(...) -> FactCheckResult` and `FactCheckAgentError`.

- [ ] **Step 1: Write a RED test for a two-round search loop**

```python
def test_agent_searches_then_returns_only_cited_facts():
    model = ScriptedModel([
        {"tool_calls": [
            {"name": "web_search", "arguments": {"query": "US CPI August 12 2026 actual forecast"}},
            {"name": "web_search", "arguments": {"query": "July 2026 core CPI consensus actual"}},
        ]},
        resolved_fact_payload(evidence_ids=["e1", "e2"]),
    ])
    result = M3FactCheckAgent(model, FakeSearch()).run(video, need, claims, segments)
    assert result.impact is FactCheckImpact.NEUTRAL
    assert result.evidence_ids == ("e1", "e2")
    assert result.branch_decisions[0].status is BranchStatus.NOT_TRIGGERED
```

- [ ] **Step 2: Run Task 3 tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_fact_check_agent.py -q`

Expected: collection fails because `goldbook.fact_check_agent` does not exist.

- [ ] **Step 3: Add a generic JSON/tool completion method to MiniMaxClient**

```python
def complete_with_tools(
    self,
    messages: Sequence[Mapping[str, object]],
    tools: Sequence[Mapping[str, object]],
) -> Mapping[str, object]:
    response = self._post_json_with_retries({
        "model": self._model,
        "messages": list(messages),
        "tools": list(tools),
    })
    return _mapping_content_from_response(response)
```

Reuse the existing semaphore, retryable 429/5xx/transport handling, fixed provider error, and server-side Authorization behavior. Preserve existing analysis and claim extraction behavior.

- [ ] **Step 4: Implement the bounded agent loop and strict parser**

```python
for round_index in range(MAX_AGENT_ROUNDS):
    reply = model.complete_with_tools(messages, (_WEB_SEARCH_TOOL,))
    calls = parse_search_calls(reply)
    if not calls:
        return validate_fact_check_result(parse_final_result(reply), evidence)
    for call in deduplicate_calls(calls, remaining=MAX_SEARCHES - search_count):
        found = search_provider.search(call.query)
        evidence.extend(normalize_evidence(call.query, found, fetched_at=clock()))
        search_count += 1
raise FactCheckAgentError("fact-check search limit exhausted")
```

Use `ThreadPoolExecutor(max_workers=3)` only for tool calls returned in the same M3 response. Reject final facts whose evidence IDs are absent, reject unknown claim IDs, and require every branch decision to name one current claim.

- [ ] **Step 5: Add conflict, limit, malformed-schema, and provider-failure tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_fact_check_agent.py tests/test_minimax.py -q`

Expected: all focused tests pass, including a measured maximum search concurrency of 3 and total call count of 6.

- [ ] **Step 6: Commit Task 3**

```powershell
git add goldbook/fact_check_agent.py goldbook/minimax.py tests/test_fact_check_agent.py tests/test_minimax.py
git commit -m "feat: let M3 orchestrate cited web fact checks"
```

### Task 4: Persistence, revision invalidation, and background jobs

**Files:**
- Modify: `goldbook/db.py`
- Modify: `goldbook/jobs.py`
- Create: `tests/test_fact_check_db.py`
- Modify: `tests/test_jobs.py`

**Interfaces:**
- Consumes: Task 1 domain types and `M3FactCheckAgent`.
- Produces: `Database.create_or_get_fact_check_job`, `save_fact_check_run`, `save_fact_check_evidence`, `save_fact_check_result`, `get_current_fact_check(bvid)`, `PipelineService.enqueue_fact_check`, and worker support for job kind `fact_check`.

- [ ] **Step 1: Write RED persistence tests**

```python
def test_fact_check_round_trip_is_bound_to_analysis_revision(database, video, claim):
    run = database.create_fact_check_run(video.bvid, analysis_revision=1, event_description="CPI")
    database.save_fact_check_evidence(run.run_id, (evidence_one, evidence_two))
    database.save_fact_check_result(run.run_id, resolved_result)
    assert database.get_current_fact_check(video.bvid).result == resolved_result

    database.save_analysis(replace(latest_analysis, revision=2))
    assert database.get_current_fact_check(video.bvid) is None
```

- [ ] **Step 2: Run persistence tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_fact_check_db.py -q`

Expected: `Database` has no fact-check methods.

- [ ] **Step 3: Add idempotent SQLite schema and repository methods**

```sql
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
    query TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
    domain TEXT NOT NULL, published_at TEXT, snippet TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_check_results (
    run_id TEXT PRIMARY KEY REFERENCES fact_check_runs(run_id) ON DELETE CASCADE,
    result_json TEXT NOT NULL
);
```

Serialize enums and datetimes explicitly. Read only a result whose `analysis_revision` equals the latest analysis revision.

- [ ] **Step 4: Write RED background-job tests**

```python
def test_web_enqueue_creates_one_fact_check_job_and_worker_runs_agent(pipeline, runner):
    first = pipeline.enqueue_fact_check("BV1CPI")
    second = pipeline.enqueue_fact_check("BV1CPI")
    assert first.id == second.id
    runner.start()
    wait_until(lambda: pipeline.db.get_job(first.id).status == "complete")
    assert pipeline.db.get_current_fact_check("BV1CPI").result.impact is FactCheckImpact.NEUTRAL
```

- [ ] **Step 5: Implement job creation, CAS transitions, cancellation, retry, and worker dispatch**

Fact-check jobs use the existing job table with `kind='fact_check'`, `video_bvid=<bvid>`, and stage values `detecting`, `searching`, `validating`, `evaluating`, `complete`. The worker catches provider failures with the existing fixed safe summary and leaves failed jobs retryable.

- [ ] **Step 6: Run DB/jobs suites and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_fact_check_db.py tests/test_jobs.py tests/test_db.py -q`

Expected: all focused tests pass.

```powershell
git add goldbook/db.py goldbook/jobs.py tests/test_fact_check_db.py tests/test_jobs.py
git commit -m "feat: persist and run fact checks in background"
```

### Task 5: Conditional claim evaluation and metric exclusion

**Files:**
- Modify: `goldbook/claim_evaluation.py`
- Modify: `goldbook/claim_metrics.py`
- Modify: `goldbook/models.py`
- Create: `tests/test_conditional_evaluation.py`
- Modify: `tests/test_claim_metrics.py`

**Interfaces:**
- Consumes: current `FactCheckResult`, `BranchDecision`, claims, and price bars.
- Produces: `EvaluationVerdict.NOT_TRIGGERED`, reason codes `condition_not_triggered`, `fact_conflicting`, `fact_insufficient`, and `apply_fact_check_to_claim_evaluations(...)`.

- [ ] **Step 1: Write RED tests for non-triggered and triggered branches**

```python
def test_untriggered_conditional_claim_is_not_a_miss():
    evaluation = apply_fact_check_to_claim_evaluation(claim, existing, not_triggered_decision)
    assert evaluation.verdict is EvaluationVerdict.NOT_TRIGGERED
    assert evaluation.mature is False
    metrics = aggregate_video_claims(claim.bvid, (claim,), (evaluation,))
    assert metrics.scoreable_count == 0
    assert metrics.verdict_counts["miss"] == 0


def test_triggered_claim_keeps_programmatic_price_verdict():
    evaluation = apply_fact_check_to_claim_evaluation(claim, price_hit, triggered_decision)
    assert evaluation.verdict is EvaluationVerdict.HIT
```

- [ ] **Step 2: Run conditional tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_conditional_evaluation.py tests/test_claim_metrics.py -q`

Expected: `EvaluationVerdict.NOT_TRIGGERED` is missing.

- [ ] **Step 3: Implement verdict and aggregation rules**

```python
if decision.status is BranchStatus.NOT_TRIGGERED:
    return replace(
        evaluation,
        verdict=EvaluationVerdict.NOT_TRIGGERED,
        mature=False,
        reason="condition_not_triggered",
    )
if result.impact is FactCheckImpact.CONFLICTING:
    return replace(evaluation, verdict=EvaluationVerdict.UNRESOLVED, mature=False, reason="fact_conflicting")
if result.impact is FactCheckImpact.INSUFFICIENT:
    return replace(evaluation, verdict=EvaluationVerdict.UNRESOLVED, mature=False, reason="fact_insufficient")
return evaluation
```

Add a separate `not_triggered` display count but exclude it from hit/miss denominators and coverage penalties. Existing unconditional metrics must remain byte-for-byte equivalent in their tests.

- [ ] **Step 4: Run all evaluation/metrics tests and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_conditional_evaluation.py tests/test_claim_evaluation.py tests/test_claim_metrics.py -q`

Expected: all focused tests pass.

```powershell
git add goldbook/claim_evaluation.py goldbook/claim_metrics.py goldbook/models.py tests/test_conditional_evaluation.py tests/test_claim_metrics.py
git commit -m "feat: separate condition activation from price accuracy"
```

### Task 6: Video-page workflow and fact-check evidence UI

**Files:**
- Modify: `goldbook/web.py`
- Modify: `goldbook/templates/video.html`
- Modify: `goldbook/static/app.css`
- Modify: `tests/test_claim_web.py`
- Modify: `tests/test_web.py`

**Interfaces:**
- Consumes: `Database.get_current_fact_check`, `PipelineService.enqueue_fact_check`, existing CSRF middleware, job controls, and claim decision rows.
- Produces: POST `/creators/<uid>/videos/<bvid>/fact-check`, a fact-check card, cited source list, branch badges, and safe status/error rendering.

- [ ] **Step 1: Write RED route and rendering tests**

```python
def test_fact_check_post_only_enqueues_background_work(web_app, csrf_token):
    response = web_app.post(
        "/creators/1847287889/videos/BV1CPI/fact-check",
        data={"csrf_token": csrf_token},
    )
    assert response.status_code == 303
    assert pipeline.fact_check_calls == ["BV1CPI"]
    assert search_provider.calls == []


def test_video_page_shows_fact_sources_and_untriggered_branch(client, resolved_fact_check):
    response = client.get("/creators/1847287889/videos/BV1CPI")
    assert "联网事实核查" in response.text
    assert "实际值 / 市场预期 / 前值" in response.text
    assert "条件未触发（不计为未命中）" in response.text
    assert "https://example.com/cpi" in response.text
    assert "sk-" not in response.text
```

- [ ] **Step 2: Run Web tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_claim_web.py tests/test_web.py -q`

Expected: fact-check route returns 404 and fact-check card is absent.

- [ ] **Step 3: Implement route, view model, template card, and styles**

The card order is: status and rerun action; event/time; actual/forecast/previous table; impact and confidence; branch decisions; source links; warning. Render only sanitized domain/title/snippet fields. Add `rel="noopener noreferrer"` to source links. Use existing verdict colors and a distinct gray `not-triggered` badge.

- [ ] **Step 4: Add production-mode CSRF, unknown video, secret, and RuntimeError regressions**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_claim_web.py tests/test_web.py -q`

Expected: all focused tests pass.

- [ ] **Step 5: Commit Task 6**

```powershell
git add goldbook/web.py goldbook/templates/video.html goldbook/static/app.css tests/test_claim_web.py tests/test_web.py
git commit -m "feat: show cited event fact checks on video pages"
```

### Task 7: CLI, controlled CPI live probe, and final verification

**Files:**
- Modify: `goldbook/__main__.py`
- Modify: `README.md`
- Create: `tests/test_fact_check_cli.py`
- Create: `docs/verification-fact-check.md`

**Interfaces:**
- Consumes: composition root settings, DB, M3 client, MCP search client, and pipeline.
- Produces: `python -m goldbook fact-check --bvid <BVID>`, current CPI result in the local DB, and verification evidence.

- [ ] **Step 1: Write RED CLI dispatch tests**

```python
def test_fact_check_cli_requires_bvid_and_dispatches_once(monkeypatch):
    calls = []
    monkeypatch.setattr(main_module, "_fact_check_video", lambda settings, bvid: calls.append(bvid))
    assert main_module.main(["fact-check", "--bvid", "BV1uhuy6AEA6"]) == 0
    assert calls == ["BV1uhuy6AEA6"]
```

- [ ] **Step 2: Run CLI test and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_fact_check_cli.py -q`

Expected: parser rejects the unknown `fact-check` command.

- [ ] **Step 3: Implement CLI composition and README instructions**

The command validates the video exists, runs or retries only its fact-check job, waits for that job to reach a terminal state, prints safe status/search-count/result summary, and never prints evidence snippets or environment values to the terminal.

- [ ] **Step 4: Run the complete offline verification suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q goldbook tests
git diff --check
```

Expected: all tests pass; compileall and diff check exit 0.

- [ ] **Step 5: Run one controlled real CPI fact check**

Run: `.\scripts\start.ps1` only if the service is not already running, then execute:

```powershell
.\.venv\Scripts\python.exe -m goldbook fact-check --bvid BV1uhuy6AEA6
```

Verify in the database and browser that the result:

- names the 2026-08-12 U.S. CPI event and its release time;
- lists headline/core values without merging their units or periods;
- cites at least two saved public URLs or explicitly reports insufficient evidence;
- distinguishes `supportive` from `not_supportive` branches;
- does not score an untriggered branch as a miss;
- contains no `RuntimeError`, key prefix, raw Authorization value, private URL, or media residue.

- [ ] **Step 6: Restart one local service and run HTTP smoke tests**

Check `/`, both creator pages, `/creators/1847287889/videos/BV1uhuy6AEA6`, and `/api/status`. Require 200, baseline security headers, one listener on `127.0.0.1:8765`, and visible fact-check source links.

- [ ] **Step 7: Record evidence and commit Task 7**

```powershell
git add goldbook/__main__.py README.md tests/test_fact_check_cli.py docs/verification-fact-check.md
git commit -m "feat: deliver M3 event fact checking"
```
