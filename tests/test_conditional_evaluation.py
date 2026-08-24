from dataclasses import replace
from datetime import date, datetime, timezone

from goldbook.claim_evaluation import apply_fact_check_to_claim_evaluation
from goldbook.claim_metrics import aggregate_video_claims
from goldbook.fact_check import (
    BranchDecision,
    BranchPredicate,
    BranchStatus,
    FactCheckImpact,
    FactCheckResult,
)
from goldbook.models import (
    ClaimEvaluation,
    ClaimLeg,
    ClaimStatus,
    ClaimType,
    Direction,
    EvaluationVerdict,
    ForecastClaim,
    HorizonSource,
    Instrument,
)


NOW = datetime(2026, 8, 21, tzinfo=timezone.utc)


def _claim(index):
    return ForecastClaim(
        f"BV1CPI:1:{index}",
        "BV1CPI",
        1,
        index,
        Instrument.XAU_USD_SPOT,
        ClaimType.TARGET_TOUCH,
        Direction.BULLISH,
        (ClaimLeg(">=", 4400.0, None),),
        "若CPI利好则突破4400",
        "今晚",
        HorizonSource.EXPLICIT_RELATIVE,
        0,
        1,
        1,
        None,
        0.9,
        0.8,
        (),
        None,
        ClaimStatus.AUTO_VALIDATED,
        "MiniMax-M3",
        "claims-v2",
        "hash",
    )


def _evaluation(claim, verdict=EvaluationVerdict.MISS):
    return ClaimEvaluation(
        claim.claim_id,
        NOW,
        date(2026, 8, 13),
        date(2026, 8, 13),
        4380.0,
        4360.0,
        4395.0,
        4388.0,
        4395.0,
        date(2026, 8, 13),
        0.001,
        None,
        verdict,
        True,
        verdict.value,
    )


def _result(decision, impact=FactCheckImpact.NEUTRAL):
    return FactCheckResult(
        "Was CPI supportive?",
        "US CPI",
        datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc),
        (),
        impact,
        "The release matched expectations.",
        (),
        (decision,),
        0.8,
    )


def test_untriggered_conditional_claim_is_not_a_miss_or_coverage_penalty():
    conditional = _claim(1)
    ordinary = replace(_claim(2), condition_text="黄金目标4400")
    decision = BranchDecision(
        conditional.claim_id,
        BranchPredicate.SUPPORTIVE,
        BranchStatus.NOT_TRIGGERED,
        "CPI was neutral.",
    )

    excluded = apply_fact_check_to_claim_evaluation(
        conditional, _evaluation(conditional), _result(decision)
    )
    hit = _evaluation(ordinary, EvaluationVerdict.HIT)
    metrics = aggregate_video_claims("BV1CPI", (conditional, ordinary), (excluded, hit))

    assert excluded.verdict is EvaluationVerdict.NOT_TRIGGERED
    assert excluded.mature is False
    assert excluded.reason == "condition_not_triggered"
    assert metrics.scoreable_count == 1
    assert metrics.score == 1.0
    assert metrics.coverage_rate == 1.0
    assert metrics.verdict_counts["not_triggered"] == 1
    assert metrics.verdict_counts["miss"] == 0


def test_triggered_claim_keeps_its_programmatic_price_verdict():
    claim = _claim(1)
    decision = BranchDecision(
        claim.claim_id,
        BranchPredicate.NOT_SUPPORTIVE,
        BranchStatus.TRIGGERED,
        "Neutral activates the not-supportive branch.",
    )
    original = _evaluation(claim, EvaluationVerdict.HIT)
    assert apply_fact_check_to_claim_evaluation(claim, original, _result(decision)) == original


def test_conflicting_or_insufficient_fact_result_keeps_claim_unresolved():
    claim = _claim(1)
    decision = BranchDecision(
        claim.claim_id,
        BranchPredicate.SUPPORTIVE,
        BranchStatus.NOT_TRIGGERED,
        "Sources conflict.",
    )
    for impact, reason in (
        (FactCheckImpact.CONFLICTING, "fact_conflicting"),
        (FactCheckImpact.INSUFFICIENT, "fact_insufficient"),
    ):
        result = apply_fact_check_to_claim_evaluation(
            claim, _evaluation(claim), _result(decision, impact)
        )
        assert result.verdict is EvaluationVerdict.UNRESOLVED
        assert result.mature is False
        assert result.reason == reason
