from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from goldbook.claim_metrics import aggregate_creator_claims, aggregate_video_claims
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


def _claim(bvid: str, index: int, claim_type=ClaimType.TARGET_TOUCH) -> ForecastClaim:
    return ForecastClaim(
        claim_id=f"{bvid}:1:{index}",
        bvid=bvid,
        analysis_revision=1,
        claim_index=index,
        instrument=Instrument.XAU_USD_SPOT,
        claim_type=claim_type,
        direction=Direction.BULLISH,
        legs=(ClaimLeg(">=", 4700.0, None),),
        condition_text="目标4700",
        horizon_text="短期",
        horizon_source=HorizonSource.CONTEXT_INFERRED,
        horizon_min_trading_days=1,
        horizon_max_trading_days=3,
        horizon_point_trading_days=2,
        deadline_at=None,
        time_confidence=0.8,
        confidence=0.9,
        evidence=({"start_sec": 0.0, "end_sec": 1.0, "quote": "目标4700"},),
        supersedes_claim_id=None,
        status=ClaimStatus.AUTO_VALIDATED,
        model_name="MiniMax-M3",
        prompt_version="claims-v1",
        transcript_hash="hash",
    )


def _evaluation(claim: ForecastClaim, verdict: EvaluationVerdict) -> ClaimEvaluation:
    return ClaimEvaluation(
        claim_id=claim.claim_id,
        evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        window_start=date(2026, 8, 4),
        window_end=date(2026, 8, 6),
        entry_price=4660.0 if verdict is not EvaluationVerdict.UNRESOLVED else None,
        observed_min=4650.0 if verdict is not EvaluationVerdict.UNRESOLVED else None,
        observed_max=4702.0 if verdict is not EvaluationVerdict.UNRESOLVED else None,
        final_close=4698.0 if verdict is not EvaluationVerdict.UNRESOLVED else None,
        closest_price=4700.0 if verdict is not EvaluationVerdict.UNRESOLVED else None,
        closest_date=date(2026, 8, 5) if verdict is not EvaluationVerdict.UNRESOLVED else None,
        distance_pct=0.0 if verdict is not EvaluationVerdict.UNRESOLVED else None,
        first_hit_date=date(2026, 8, 5) if verdict is EvaluationVerdict.HIT else None,
        verdict=verdict,
        mature=verdict is not EvaluationVerdict.UNRESOLVED,
        reason=verdict.value,
    )


def _video(bvid: str, verdicts: list[EvaluationVerdict]):
    claims = [_claim(bvid, index) for index in range(len(verdicts))]
    return aggregate_video_claims(
        bvid, claims, [_evaluation(claim, verdict) for claim, verdict in zip(claims, verdicts)]
    )


def test_many_claims_in_one_video_do_not_outweigh_other_videos():
    first = _video("BV1", [EvaluationVerdict.HIT] * 10)
    second = _video("BV2", [EvaluationVerdict.MISS])
    third = _video("BV3", [EvaluationVerdict.MISS])

    metrics = aggregate_creator_claims([first, second, third])

    assert first.score == 1.0
    assert metrics.score == pytest.approx(1 / 3)
    assert metrics.scored_video_count == 3
    assert metrics.eligible_for_rank is True


def test_unresolved_claim_reduces_coverage_without_reducing_accuracy():
    video = _video(
        "BV1",
        [EvaluationVerdict.HIT, EvaluationVerdict.UNRESOLVED],
    )

    assert video.score == 1.0
    assert video.exact_hit_rate == 1.0
    assert video.coverage_rate == 0.5
    assert video.verdict_counts["unresolved"] == 1


def test_partial_near_scores_half_and_creator_needs_three_scored_videos():
    first = _video("BV1", [EvaluationVerdict.PARTIAL_NEAR])
    second = _video("BV2", [EvaluationVerdict.HIT])

    metrics = aggregate_creator_claims([first, second])

    assert first.score == 0.5
    assert first.exact_hit_rate == 0.0
    assert first.near_inclusive_rate == 1.0
    assert metrics.score == 0.75
    assert metrics.eligible_for_rank is False


def test_direction_and_point_scores_are_independent():
    directional = replace(
        _claim("BV1", 0, ClaimType.DIRECTIONAL_MOVE), legs=()
    )
    point = _claim("BV1", 1, ClaimType.TARGET_TOUCH)

    video = aggregate_video_claims(
        "BV1",
        [directional, point],
        [
            _evaluation(directional, EvaluationVerdict.MISS),
            _evaluation(point, EvaluationVerdict.HIT),
        ],
    )

    assert video.directional_metrics.score == 0.0
    assert video.directional_metrics.total_claim_count == 1
    assert video.directional_metrics.verdict_counts["miss"] == 1
    assert video.point_metrics.score == 1.0
    assert video.point_metrics.total_claim_count == 1
    assert video.point_metrics.verdict_counts["hit"] == 1


def test_directional_metrics_count_only_primary_bullish_or_bearish_trend():
    primary = replace(
        _claim("BV1", 0, ClaimType.DIRECTIONAL_MOVE), legs=()
    )
    duplicate_point_direction = replace(
        _claim("BV1", 1, ClaimType.DIRECTIONAL_MOVE), legs=()
    )
    neutral_primary = replace(
        _claim("BV2", 0, ClaimType.DIRECTIONAL_MOVE),
        direction=Direction.NEUTRAL,
        legs=(),
    )

    first = aggregate_video_claims(
        "BV1",
        [primary, duplicate_point_direction],
        [
            _evaluation(primary, EvaluationVerdict.MISS),
            _evaluation(duplicate_point_direction, EvaluationVerdict.HIT),
        ],
    )
    second = aggregate_video_claims(
        "BV2",
        [neutral_primary],
        [_evaluation(neutral_primary, EvaluationVerdict.UNRESOLVED)],
    )

    assert first.directional_metrics.total_claim_count == 1
    assert first.directional_metrics.score == 0.0
    assert second.directional_metrics.total_claim_count == 0


def test_creator_point_breakdown_remains_video_equal():
    many_hits = _video("BV1", [EvaluationVerdict.HIT] * 8)
    one_miss = _video("BV2", [EvaluationVerdict.MISS])

    metrics = aggregate_creator_claims([many_hits, one_miss])

    assert metrics.point_metrics.score == pytest.approx(0.5)
    assert metrics.point_metrics.total_claim_count == 9
    assert metrics.point_metrics.scoreable_count == 9
