"""Video-equal creator metrics for claim-level forecast evaluations."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean
from typing import Sequence

from goldbook.claim_time import is_primary_trend
from goldbook.models import (
    ClaimEvaluation,
    ClaimType,
    Direction,
    EvaluationVerdict,
    ForecastClaim,
)


_SCORES = {
    EvaluationVerdict.HIT: 1.0,
    EvaluationVerdict.PARTIAL_NEAR: 0.5,
    EvaluationVerdict.MISS: 0.0,
}
_TARGET_TYPES = {
    ClaimType.TARGET_TOUCH,
    ClaimType.CROSS_ABOVE,
    ClaimType.CROSS_BELOW,
    ClaimType.HOLD_ABOVE,
    ClaimType.HOLD_BELOW,
}
_CONDITION_TYPES = {
    ClaimType.HOLD_ABOVE,
    ClaimType.HOLD_BELOW,
    ClaimType.RANGE,
    ClaimType.SEQUENCE,
}
_POINT_TYPES = {
    ClaimType.TARGET_TOUCH,
    ClaimType.CROSS_ABOVE,
    ClaimType.CROSS_BELOW,
    ClaimType.HOLD_ABOVE,
    ClaimType.HOLD_BELOW,
    ClaimType.RANGE,
    ClaimType.SEQUENCE,
    ClaimType.BREAKOUT_EITHER_SIDE,
}


@dataclass(frozen=True)
class MetricBreakdown:
    total_claim_count: int
    scoreable_count: int
    score: float | None
    exact_hit_rate: float | None
    near_inclusive_rate: float | None
    verdict_counts: dict[str, int]


@dataclass(frozen=True)
class VideoClaimMetrics:
    bvid: str
    total_claim_count: int
    scoreable_count: int
    score: float | None
    exact_hit_rate: float | None
    near_inclusive_rate: float | None
    target_hit_rate: float | None
    directional_hit_rate: float | None
    condition_hit_rate: float | None
    mean_distance_pct: float | None
    coverage_rate: float | None
    verdict_counts: dict[str, int]
    horizon_scores: dict[str, float | None]
    directional_metrics: MetricBreakdown
    point_metrics: MetricBreakdown


@dataclass(frozen=True)
class CreatorClaimMetrics:
    video_count: int
    scored_video_count: int
    total_claim_count: int
    scoreable_count: int
    score: float | None
    exact_hit_rate: float | None
    near_inclusive_rate: float | None
    target_hit_rate: float | None
    directional_hit_rate: float | None
    condition_hit_rate: float | None
    mean_distance_pct: float | None
    coverage_rate: float | None
    verdict_counts: dict[str, int]
    horizon_scores: dict[str, float | None]
    directional_metrics: MetricBreakdown
    point_metrics: MetricBreakdown
    eligible_for_rank: bool


def aggregate_video_claims(
    bvid: str,
    claims: Sequence[ForecastClaim],
    evaluations: Sequence[ClaimEvaluation],
) -> VideoClaimMetrics:
    by_id = {value.claim_id: value for value in evaluations}
    rows = [
        (claim, by_id[claim.claim_id])
        for claim in claims
        if claim.claim_id in by_id
    ]
    scoreable = [row for row in rows if row[1].verdict in _SCORES]
    scores = [_SCORES[evaluation.verdict] for _claim, evaluation in scoreable]
    target = [row for row in scoreable if row[0].claim_type in _TARGET_TYPES]
    directional = [
        row for row in scoreable if _is_scoreable_primary_direction(row[0])
    ]
    conditions = [row for row in scoreable if row[0].claim_type in _CONDITION_TYPES]
    distances = [
        evaluation.distance_pct
        for _claim, evaluation in scoreable
        if evaluation.distance_pct is not None
    ]
    verdict_counts = {value.value: 0 for value in EvaluationVerdict}
    for _claim, evaluation in rows:
        verdict_counts[evaluation.verdict.value] += 1
    applicable_claim_count = len(claims) - verdict_counts[
        EvaluationVerdict.NOT_TRIGGERED.value
    ]
    horizon_groups: dict[str, list[float]] = {}
    for claim, evaluation in scoreable:
        horizon_groups.setdefault(_horizon_group(claim), []).append(
            _SCORES[evaluation.verdict]
        )
    return VideoClaimMetrics(
        bvid=bvid,
        total_claim_count=len(claims),
        scoreable_count=len(scoreable),
        score=_mean(scores),
        exact_hit_rate=_rate(scoreable, EvaluationVerdict.HIT),
        near_inclusive_rate=_rate(
            scoreable, EvaluationVerdict.HIT, EvaluationVerdict.PARTIAL_NEAR
        ),
        target_hit_rate=_rate(target, EvaluationVerdict.HIT),
        directional_hit_rate=_rate(directional, EvaluationVerdict.HIT),
        condition_hit_rate=_rate(conditions, EvaluationVerdict.HIT),
        mean_distance_pct=_mean(distances),
        coverage_rate=(
            len(scoreable) / applicable_claim_count
            if applicable_claim_count
            else None
        ),
        verdict_counts=verdict_counts,
        horizon_scores={key: _mean(values) for key, values in horizon_groups.items()},
        directional_metrics=_breakdown(
            [row for row in rows if _is_scoreable_primary_direction(row[0])]
        ),
        point_metrics=_breakdown(
            [row for row in rows if row[0].claim_type in _POINT_TYPES]
        ),
    )


def aggregate_creator_claims(
    videos: Sequence[VideoClaimMetrics],
) -> CreatorClaimMetrics:
    scored = [video for video in videos if video.score is not None]
    total_claims = sum(video.total_claim_count for video in videos)
    scoreable = sum(video.scoreable_count for video in videos)
    verdict_counts = {value.value: 0 for value in EvaluationVerdict}
    for video in videos:
        for verdict, count in video.verdict_counts.items():
            verdict_counts[verdict] += count
    applicable_claim_count = total_claims - verdict_counts[
        EvaluationVerdict.NOT_TRIGGERED.value
    ]
    horizon_groups: dict[str, list[float]] = {}
    for video in videos:
        for group, score in video.horizon_scores.items():
            if score is not None:
                horizon_groups.setdefault(group, []).append(score)
    return CreatorClaimMetrics(
        video_count=len(videos),
        scored_video_count=len(scored),
        total_claim_count=total_claims,
        scoreable_count=scoreable,
        score=_mean([video.score for video in scored if video.score is not None]),
        exact_hit_rate=_mean_not_none(video.exact_hit_rate for video in scored),
        near_inclusive_rate=_mean_not_none(
            video.near_inclusive_rate for video in scored
        ),
        target_hit_rate=_mean_not_none(video.target_hit_rate for video in scored),
        directional_hit_rate=_mean_not_none(
            video.directional_hit_rate for video in scored
        ),
        condition_hit_rate=_mean_not_none(
            video.condition_hit_rate for video in scored
        ),
        mean_distance_pct=_mean_not_none(
            video.mean_distance_pct for video in scored
        ),
        coverage_rate=(
            scoreable / applicable_claim_count if applicable_claim_count else None
        ),
        verdict_counts=verdict_counts,
        horizon_scores={key: _mean(values) for key, values in horizon_groups.items()},
        directional_metrics=_aggregate_breakdowns(
            [video.directional_metrics for video in videos]
        ),
        point_metrics=_aggregate_breakdowns(
            [video.point_metrics for video in videos]
        ),
        eligible_for_rank=len(scored) >= 3,
    )


def _breakdown(
    rows: Sequence[tuple[ForecastClaim, ClaimEvaluation]],
) -> MetricBreakdown:
    scoreable = [row for row in rows if row[1].verdict in _SCORES]
    counts = {value.value: 0 for value in EvaluationVerdict}
    for _claim, evaluation in rows:
        counts[evaluation.verdict.value] += 1
    return MetricBreakdown(
        total_claim_count=len(rows),
        scoreable_count=len(scoreable),
        score=_mean([_SCORES[evaluation.verdict] for _claim, evaluation in scoreable]),
        exact_hit_rate=_rate(scoreable, EvaluationVerdict.HIT),
        near_inclusive_rate=_rate(
            scoreable, EvaluationVerdict.HIT, EvaluationVerdict.PARTIAL_NEAR
        ),
        verdict_counts=counts,
    )


def _is_scoreable_primary_direction(claim: ForecastClaim) -> bool:
    return is_primary_trend(claim) and claim.direction in {
        Direction.BULLISH,
        Direction.BEARISH,
    }


def _aggregate_breakdowns(values: Sequence[MetricBreakdown]) -> MetricBreakdown:
    counts = {value.value: 0 for value in EvaluationVerdict}
    for breakdown in values:
        for verdict, count in breakdown.verdict_counts.items():
            counts[verdict] += count
    scored = [value for value in values if value.score is not None]
    return MetricBreakdown(
        total_claim_count=sum(value.total_claim_count for value in values),
        scoreable_count=sum(value.scoreable_count for value in values),
        score=_mean([value.score for value in scored if value.score is not None]),
        exact_hit_rate=_mean_not_none(value.exact_hit_rate for value in scored),
        near_inclusive_rate=_mean_not_none(
            value.near_inclusive_rate for value in scored
        ),
        verdict_counts=counts,
    )


def _rate(
    rows: Sequence[tuple[ForecastClaim, ClaimEvaluation]],
    *matches: EvaluationVerdict,
) -> float | None:
    if not rows:
        return None
    return sum(evaluation.verdict in matches for _claim, evaluation in rows) / len(rows)


def _mean(values: Sequence[float]) -> float | None:
    return fmean(values) if values else None


def _mean_not_none(values) -> float | None:
    present = [value for value in values if value is not None]
    return _mean(present)


def _horizon_group(claim: ForecastClaim) -> str:
    text = claim.horizon_text or ""
    if "短" in text or "日内" in text or "明天" in text:
        return "short"
    if "中" in text or "周" in text or "月" in text:
        return "medium"
    if "长" in text or "季" in text or "年" in text:
        return "long"
    return "unknown" if not text else "explicit"
