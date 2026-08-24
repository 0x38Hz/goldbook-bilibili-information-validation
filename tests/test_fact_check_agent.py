import json
import threading
import time
from datetime import datetime, timezone

import pytest

from goldbook.fact_check import (
    BranchStatus,
    FactCheckImpact,
    FactCheckNeed,
    FactCheckValidationError,
)
from goldbook.fact_check_agent import M3FactCheckAgent, FactCheckAgentError
from goldbook.minimax_search import SearchProviderError, SearchResult
from goldbook.models import (
    ClaimLeg,
    ClaimStatus,
    ClaimType,
    Direction,
    ForecastClaim,
    HorizonSource,
    Instrument,
    TranscriptSegment,
    Video,
)


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


class ScriptedModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.messages = []

    def complete_fact_check(self, messages):
        self.messages.append(messages)
        return self.replies.pop(0)


class FakeSearch:
    def __init__(self, *, delay=0.0, fail=False):
        self.delay = delay
        self.fail = fail
        self.calls = []
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()

    def search(self, query):
        if self.fail:
            raise SearchProviderError("MiniMax search provider unavailable")
        with self.lock:
            self.calls.append(query)
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(self.delay)
        with self.lock:
            self.active -= 1
        slug = str(len(self.calls))
        return (
            SearchResult(
                title=f"Evidence {slug}",
                url=f"https://source{slug}.example/cpi",
                snippet="CPI actual and forecast were both 0.1 percent.",
                published_text="2026-08-12",
            ),
        )


def _video():
    return Video(
        "BV1CPI",
        "1847287889",
        "黄金CPI数据前瞻",
        datetime(2026, 8, 12, 0, 51, 5, tzinfo=timezone.utc),
        111,
        "https://www.bilibili.com/video/BV1CPI",
    )


def _claim(index, condition):
    return ForecastClaim(
        claim_id=f"BV1CPI:1:{index}",
        bvid="BV1CPI",
        analysis_revision=1,
        claim_index=index,
        instrument=Instrument.XAU_USD_SPOT,
        claim_type=ClaimType.SEQUENCE if index == 1 else ClaimType.RANGE,
        direction=Direction.BULLISH if index == 1 else Direction.NEUTRAL,
        legs=(ClaimLeg(">=", 4400.0, None),) if index == 1 else (ClaimLeg("between", 4350.0, 4400.0),),
        condition_text=condition,
        horizon_text="今晚CPI公布后",
        horizon_source=HorizonSource.EXPLICIT_RELATIVE,
        horizon_min_trading_days=0,
        horizon_max_trading_days=1,
        horizon_point_trading_days=1,
        deadline_at=None,
        time_confidence=0.9,
        confidence=0.8,
        evidence=({"start_sec": 1.0, "end_sec": 3.0, "quote": condition},),
        supersedes_claim_id=None,
        status=ClaimStatus.AUTO_VALIDATED,
        model_name="MiniMax-M3",
        prompt_version="claims-v2-primary-trend",
        transcript_hash="hash",
    )


def _context():
    video = _video()
    claims = (
        _claim(1, "若今晚CPI数据利好，金价先突破4400美元"),
        _claim(2, "若今晚CPI数据不利好，金价维持4350到4400整理"),
    )
    need = FactCheckNeed(
        True,
        "今晚CPI数据",
        datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 12, 15, 59, 59, tzinfo=timezone.utc),
        tuple(claim.claim_id for claim in claims),
        "external_condition_detected",
    )
    segments = (TranscriptSegment(1.0, 5.0, "今晚CPI决定黄金方向", bvid=video.bvid),)
    return video, need, claims, segments


def test_agent_searches_then_returns_only_cited_facts():
    video, need, claims, segments = _context()
    search = FakeSearch()
    model = ScriptedModel(
        [
            {
                "status": "search",
                "queries": [
                    "US CPI August 12 2026 actual forecast",
                    "July 2026 core CPI consensus actual",
                ],
            },
            lambda evidence: {
                "status": "complete",
                "result": {
                    "question": "Was the CPI release supportive for gold?",
                    "event_name": "US CPI July 2026",
                    "event_time_utc": "2026-08-12T12:30:00+00:00",
                    "facts": [
                        {
                            "name": "headline_mom",
                            "actual": "0.1",
                            "forecast": "0.1",
                            "previous": "-0.4",
                            "unit": "%",
                        }
                    ],
                    "impact": "neutral",
                    "reasoning_summary": "Actual matched consensus.",
                    "evidence_ids": evidence,
                    "branch_decisions": [
                        {
                            "claim_id": claims[0].claim_id,
                            "predicate": "supportive",
                            "status": "not_triggered",
                            "reason": "The release was neutral.",
                        },
                        {
                            "claim_id": claims[1].claim_id,
                            "predicate": "not_supportive",
                            "status": "triggered",
                            "reason": "Neutral is not supportive.",
                        },
                    ],
                    "confidence": 0.88,
                },
            },
        ]
    )

    seen_messages = []

    class ResolvingModel:
        def complete_fact_check(self, messages):
            seen_messages.append(messages)
            reply = model.replies.pop(0)
            if callable(reply):
                evidence = json.loads(messages[-1]["content"])["evidence"]
                reply = reply([item["evidence_id"] for item in evidence])
            return reply

    result = M3FactCheckAgent(ResolvingModel(), search, clock=lambda: NOW).run(
        video, need, claims, segments
    )

    assert result.result.impact is FactCheckImpact.NEUTRAL
    assert len(result.evidence) == 2
    assert result.result.evidence_ids == tuple(item.evidence_id for item in result.evidence)
    assert result.result.branch_decisions[0].status is BranchStatus.NOT_TRIGGERED
    assert result.result.branch_decisions[1].status is BranchStatus.TRIGGERED
    initial = json.loads(seen_messages[0][1]["content"])
    assert initial["claims"][0]["expected_predicate"] == "supportive"
    assert initial["claims"][1]["expected_predicate"] == "not_supportive"
    assert "不搜索或判断事件后的黄金价格" in seen_messages[0][0]["content"]


def test_agent_never_exceeds_six_searches_or_three_concurrent_calls():
    video, need, claims, segments = _context()
    queries = [f"query {index}" for index in range(6)]
    search = FakeSearch(delay=0.03)
    model = ScriptedModel(
        [
            {"status": "search", "queries": queries},
            {
                "status": "complete",
                "result": {
                    "question": "Could the event be verified?",
                    "event_name": "US CPI July 2026",
                    "event_time_utc": None,
                    "facts": [],
                    "impact": "insufficient",
                    "reasoning_summary": "Search evidence was not conclusive.",
                    "evidence_ids": [],
                    "branch_decisions": [],
                    "confidence": 0.1,
                },
            },
        ]
    )

    M3FactCheckAgent(model, search, clock=lambda: NOW).run(video, need, claims, segments)

    assert sorted(search.calls) == queries
    assert search.peak == 3


def test_agent_rejects_a_model_request_for_more_than_six_searches():
    video, need, claims, segments = _context()
    model = ScriptedModel(
        [{"status": "search", "queries": [f"query {index}" for index in range(7)]}]
    )
    search = FakeSearch()
    with pytest.raises(FactCheckAgentError, match="search limit"):
        M3FactCheckAgent(model, search, clock=lambda: NOW).run(
            video, need, claims, segments
        )
    assert search.calls == []


def test_agent_preserves_provider_failure_as_a_retryable_safe_error():
    video, need, claims, segments = _context()
    model = ScriptedModel([{"status": "search", "queries": ["query"]}])
    with pytest.raises(FactCheckAgentError, match="search provider unavailable") as error:
        M3FactCheckAgent(model, FakeSearch(fail=True), clock=lambda: NOW).run(
            video, need, claims, segments
        )
    assert "MiniMax" not in str(error.value)


def test_agent_gives_m3_one_bounded_schema_correction_round():
    video, need, claims, segments = _context()
    search = FakeSearch()
    valid_result = {
        "question": "Was CPI supportive?",
        "event_name": "US CPI",
        "event_time_utc": "2026-08-12T12:30:00+00:00",
        "facts": [{"name": "headline_mom", "actual": "0.1", "forecast": "0.2", "previous": "0.3", "unit": "%"}],
        "impact": "supportive",
        "reasoning_summary": "Actual was below forecast.",
        "evidence_ids": ["ev"],
        "branch_decisions": [],
        "confidence": 0.8,
    }

    class CorrectingModel:
        def __init__(self):
            self.calls = []

        def complete_fact_check(self, messages):
            self.calls.append(messages)
            if len(self.calls) == 1:
                return {"status": "complete", "result": {**valid_result, "facts": [{"metric": "headline_mom"}]}}
            assert "schema_validation_failed" in messages[-1]["content"]
            return {
                "status": "complete",
                "result": {**valid_result, "impact": "insufficient", "facts": [], "evidence_ids": []},
            }

    model = CorrectingModel()
    result = M3FactCheckAgent(model, search, clock=lambda: NOW).run(
        video, need, claims, segments
    )

    assert result.result.impact is FactCheckImpact.INSUFFICIENT
    assert len(model.calls) == 2


def test_agent_can_correct_a_branch_status_that_contradicts_the_fact_impact():
    video, need, claims, segments = _context()
    search = FakeSearch()

    class CorrectingBranchModel:
        def __init__(self):
            self.calls = 0

        def complete_fact_check(self, messages):
            self.calls += 1
            if self.calls == 1:
                return {"status": "search", "queries": ["CPI release", "CPI consensus"]}
            evidence_ids = [item["evidence_id"] for item in json.loads(messages[-1]["content"])["evidence"]] if self.calls == 2 else None
            if self.calls == 3:
                assert "schema_validation_failed" in messages[-1]["content"]
                evidence_ids = [item["evidence_id"] for item in json.loads(messages[-3]["content"])["evidence"]]
            return {
                "status": "complete",
                "result": {
                    "question": "Was CPI supportive?", "event_name": "US CPI",
                    "event_time_utc": "2026-08-12T12:30:00+00:00",
                    "facts": [{"name": "headline", "actual": "0.1", "forecast": "0.2", "previous": "0.3", "unit": "%"}],
                    "impact": "supportive", "reasoning_summary": "Below forecast.",
                    "evidence_ids": evidence_ids,
                    "branch_decisions": [{
                        "claim_id": claims[0].claim_id, "predicate": "supportive",
                        "status": "not_triggered" if self.calls == 2 else "triggered",
                        "reason": "Supportive branch comparison.",
                    }, {
                        "claim_id": claims[1].claim_id, "predicate": "not_supportive",
                        "status": "not_triggered",
                        "reason": "Not-supportive branch comparison.",
                    }],
                    "confidence": 0.8,
                },
            }

    model = CorrectingBranchModel()
    result = M3FactCheckAgent(model, search, clock=lambda: NOW).run(video, need, claims, segments)

    assert result.result.branch_decisions[0].status is BranchStatus.TRIGGERED
    assert model.calls == 3


def test_agent_rejects_an_uncited_resolved_result():
    video, need, claims, segments = _context()
    model = ScriptedModel(
        [
            {
                "status": "complete",
                "result": {
                    "question": "Was CPI supportive?",
                    "event_name": "US CPI",
                    "event_time_utc": "2026-08-12T12:30:00+00:00",
                    "facts": [{"name": "headline", "actual": "0.1", "forecast": "0.1", "previous": "-0.4", "unit": "%"}],
                    "impact": "neutral",
                    "reasoning_summary": "It matched consensus.",
                    "evidence_ids": [],
                    "branch_decisions": [],
                    "confidence": 0.8,
                },
            }
        ]
    )
    with pytest.raises(FactCheckValidationError, match="independent sources"):
        M3FactCheckAgent(model, FakeSearch(), clock=lambda: NOW).run(
            video, need, claims, segments
        )
