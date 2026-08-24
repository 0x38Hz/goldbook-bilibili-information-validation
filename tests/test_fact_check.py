from dataclasses import replace
from datetime import datetime, timezone

import pytest

from goldbook.fact_check import (
    BranchDecision,
    BranchPredicate,
    BranchStatus,
    FactCheckImpact,
    FactCheckResult,
    FactCheckValidationError,
    FactValue,
    SearchEvidence,
    detect_fact_check_need,
    predicate_matches,
    validate_fact_check_result,
    validate_search_evidence,
)
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


def _video() -> Video:
    return Video(
        bvid="BV1uhuy6AEA6",
        creator_uid="1847287889",
        title="黄金CPI数据前瞻",
        published_at=datetime(2026, 8, 12, 0, 51, 5, tzinfo=timezone.utc),
        duration_sec=111,
        url="https://www.bilibili.com/video/BV1uhuy6AEA6",
    )


def _claim(condition: str) -> ForecastClaim:
    return ForecastClaim(
        claim_id="BV1uhuy6AEA6:1:1",
        bvid="BV1uhuy6AEA6",
        analysis_revision=1,
        claim_index=1,
        instrument=Instrument.XAU_USD_SPOT,
        claim_type=ClaimType.SEQUENCE,
        direction=Direction.BULLISH,
        legs=(ClaimLeg(">=", 4400.0, None), ClaimLeg(">=", 4450.0, None)),
        condition_text=condition,
        horizon_text="今晚CPI公布后",
        horizon_source=HorizonSource.EXPLICIT_RELATIVE,
        horizon_min_trading_days=0,
        horizon_max_trading_days=1,
        horizon_point_trading_days=1,
        deadline_at=None,
        time_confidence=0.9,
        confidence=0.85,
        evidence=({"start_sec": 10.0, "end_sec": 18.0, "quote": condition},),
        supersedes_claim_id=None,
        status=ClaimStatus.AUTO_VALIDATED,
        model_name="MiniMax-M3",
        prompt_version="claims-v2-primary-trend",
        transcript_hash="hash",
    )


def _evidence(identifier: str, domain: str) -> SearchEvidence:
    return SearchEvidence(
        evidence_id=identifier,
        query="US CPI August 12 2026 actual forecast",
        title=f"CPI evidence {identifier}",
        url=f"https://{domain}/cpi/{identifier}",
        domain=domain,
        published_at=NOW,
        snippet="Headline CPI was 0.1 percent versus a 0.1 percent consensus.",
        fetched_at=NOW,
    )


def test_gate_activates_only_for_external_conditional_claims():
    video = _video()
    cpi = _claim("若今晚CPI数据利好，金价先突破4400美元")
    transcript = (
        TranscriptSegment(10.0, 18.0, cpi.condition_text, bvid=video.bvid),
    )

    need = detect_fact_check_need(video, (cpi,), transcript)

    assert need.required is True
    assert need.event_description == "今晚CPI数据"
    assert need.claim_ids == (cpi.claim_id,)
    assert need.expected_start == datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc)
    assert need.expected_end == datetime(2026, 8, 12, 15, 59, 59, tzinfo=timezone.utc)

    ordinary = replace(cpi, condition_text="金价突破4400美元后上探4450美元")
    skipped = detect_fact_check_need(video, (ordinary,), transcript)
    assert skipped.required is False
    assert skipped.claim_ids == ()


def test_gate_does_not_treat_technical_analysis_as_an_external_fact():
    claim = _claim("若布林带收口后突破4400美元，金价将延续上涨")
    result = detect_fact_check_need(_video(), (claim,), ())
    assert result.required is False
    assert result.reason == "no_external_condition"


def test_not_supportive_includes_neutral_but_adverse_does_not():
    assert predicate_matches(BranchPredicate.NOT_SUPPORTIVE, FactCheckImpact.NEUTRAL)
    assert predicate_matches(BranchPredicate.NOT_SUPPORTIVE, FactCheckImpact.ADVERSE)
    assert not predicate_matches(BranchPredicate.ADVERSE, FactCheckImpact.NEUTRAL)
    assert predicate_matches(BranchPredicate.SUPPORTIVE, FactCheckImpact.SUPPORTIVE)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1:8765/api/status",
        "http://10.0.0.1/private",
        "http://169.254.169.254/latest/meta-data",
        "https://localhost/private",
    ],
)
def test_validator_rejects_non_public_evidence_urls(url):
    evidence = replace(_evidence("e1", "example.com"), url=url)
    with pytest.raises(FactCheckValidationError):
        validate_search_evidence(evidence)


def test_result_requires_cited_evidence_from_two_independent_domains():
    evidence = (_evidence("e1", "one.example"), _evidence("e2", "two.example"))
    result = FactCheckResult(
        question="Was CPI supportive for gold?",
        event_name="US CPI July 2026",
        event_time_utc=datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc),
        facts=(FactValue("headline_mom", "0.1", "0.1", "-0.4", "%"),),
        impact=FactCheckImpact.NEUTRAL,
        reasoning_summary="Actual matched consensus.",
        evidence_ids=("e1", "e2"),
        branch_decisions=(
            BranchDecision(
                claim_id="BV1uhuy6AEA6:1:1",
                predicate=BranchPredicate.SUPPORTIVE,
                status=BranchStatus.NOT_TRIGGERED,
                reason="The release was neutral.",
            ),
        ),
        confidence=0.88,
    )

    validated = validate_fact_check_result(
        result,
        evidence,
        current_claim_ids={"BV1uhuy6AEA6:1:1"},
    )
    assert validated == result

    with pytest.raises(FactCheckValidationError, match="independent sources"):
        validate_fact_check_result(
            replace(result, evidence_ids=("e1",)),
            evidence,
            current_claim_ids={"BV1uhuy6AEA6:1:1"},
        )


def test_insufficient_result_may_preserve_one_source_without_claiming_a_fact():
    evidence = (_evidence("e1", "one.example"),)
    result = FactCheckResult(
        question="Was CPI supportive for gold?",
        event_name="US CPI July 2026",
        event_time_utc=None,
        facts=(),
        impact=FactCheckImpact.INSUFFICIENT,
        reasoning_summary="Only one source was found.",
        evidence_ids=("e1",),
        branch_decisions=(),
        confidence=0.2,
    )
    assert validate_fact_check_result(result, evidence, current_claim_ids=set()) == result
