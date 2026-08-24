"""Transparent fixed-horizon signal scoring."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
import math

from goldbook.models import (
    CreatorMetricSample,
    Direction,
    Outcome,
    PriceBar,
    ReviewStatus,
    SignalAnalysis,
)

_LOW_CONFIDENCE_THRESHOLD = 0.70


@dataclass(frozen=True)
class CreatorMetrics:
    """Compounded performance over the priceable outcomes supplied in order."""

    scored_count: int
    mature_count: int
    compound_return_1d: float | None
    compound_return_5d: float | None
    compound_return_20d: float | None
    average_signed_return_1d: float | None
    average_signed_return_5d: float | None
    average_signed_return_20d: float | None
    hit_rate_1d: float | None
    hit_rate_5d: float | None
    hit_rate_20d: float | None
    bullish_count: int
    bearish_count: int
    best_sample: CreatorMetricSample | None
    worst_sample: CreatorMetricSample | None
    low_confidence_proportion: float | None
    manual_revision_proportion: float | None
    disposition_counts: dict[str, int]
    eligible_for_rank: bool


def score_signal(
    analysis: SignalAnalysis,
    published_at: datetime,
    bars: Sequence[PriceBar],
) -> Outcome:
    """Price a directional signal while ranking only approved, includable results."""
    if not _is_priceable(analysis):
        return _unpriced_outcome(analysis)

    ordered_bars = sorted(bars, key=lambda bar: bar.trade_date)
    entry_index = next(
        (
            index
            for index, bar in enumerate(ordered_bars)
            if bar.trade_date > published_at.date()
        ),
        None,
    )
    if entry_index is None:
        return _unpriced_outcome(analysis)

    entry_bar = ordered_bars[entry_index]
    direction_multiplier = 1.0 if analysis.direction is Direction.BULLISH else -1.0
    exit_1d = _exit_price(ordered_bars, entry_index, 1)
    exit_5d = _exit_price(ordered_bars, entry_index, 5)
    exit_20d = _exit_price(ordered_bars, entry_index, 20)

    return Outcome(
        direction=analysis.direction,
        entry_date=entry_bar.trade_date,
        entry_price=entry_bar.open,
        bvid=analysis.bvid,
        signal_id=analysis.bvid,
        review_status=analysis.review_status,
        included=_is_includable(analysis),
        exit_1d=exit_1d,
        exit_5d=exit_5d,
        exit_20d=exit_20d,
        return_1d=_signed_return(entry_bar.open, exit_1d, direction_multiplier),
        return_5d=_signed_return(entry_bar.open, exit_5d, direction_multiplier),
        return_20d=_signed_return(entry_bar.open, exit_20d, direction_multiplier),
        mature=exit_5d is not None,
    )


def aggregate_creator(outcomes: Sequence[Outcome | CreatorMetricSample]) -> CreatorMetrics:
    """Aggregate priceable outcomes, preserving the caller's publication order."""
    samples = [_as_metric_sample(outcome) for outcome in outcomes]
    priced_samples = _unique_eligible_samples(samples)
    return_1d = _compound(sample.return_1d for sample in priced_samples)
    return_5d = _compound(sample.return_5d for sample in priced_samples)
    return_20d = _compound(sample.return_20d for sample in priced_samples)
    mature_count = len(priced_samples)
    analyzed_samples = [sample for sample in samples if sample.review_status is not None]
    return CreatorMetrics(
        scored_count=mature_count,
        mature_count=mature_count,
        compound_return_1d=return_1d,
        compound_return_5d=return_5d,
        compound_return_20d=return_20d,
        average_signed_return_1d=_average(sample.return_1d for sample in priced_samples),
        average_signed_return_5d=_average(sample.return_5d for sample in priced_samples),
        average_signed_return_20d=_average(sample.return_20d for sample in priced_samples),
        hit_rate_1d=_hit_rate(sample.return_1d for sample in priced_samples),
        hit_rate_5d=_hit_rate(sample.return_5d for sample in priced_samples),
        hit_rate_20d=_hit_rate(sample.return_20d for sample in priced_samples),
        bullish_count=sum(sample.direction is Direction.BULLISH for sample in priced_samples),
        bearish_count=sum(sample.direction is Direction.BEARISH for sample in priced_samples),
        best_sample=_best_sample(priced_samples),
        worst_sample=_worst_sample(priced_samples),
        low_confidence_proportion=_proportion(
            sample.confidence is not None and sample.confidence < _LOW_CONFIDENCE_THRESHOLD
            for sample in analyzed_samples
        ),
        manual_revision_proportion=_proportion(
            sample.manual_revision for sample in analyzed_samples
        ),
        disposition_counts=_disposition_counts(samples),
        eligible_for_rank=mature_count >= 3,
    )


def _is_includable(analysis: SignalAnalysis) -> bool:
    return (
        analysis.review_status is ReviewStatus.APPROVED
        and analysis.direction in (Direction.BULLISH, Direction.BEARISH)
        and not analysis.is_retrospective
        and not analysis.is_news_only
    )


def _is_priceable(analysis: SignalAnalysis) -> bool:
    return (
        analysis.direction in (Direction.BULLISH, Direction.BEARISH)
        and analysis.review_status is not ReviewStatus.EXCLUDED
        and not analysis.is_retrospective
        and not analysis.is_news_only
    )


def _unpriced_outcome(analysis: SignalAnalysis) -> Outcome:
    return Outcome(
        direction=analysis.direction,
        bvid=analysis.bvid,
        signal_id=analysis.bvid,
        review_status=analysis.review_status,
        included=False,
    )


def _as_metric_sample(outcome: Outcome | CreatorMetricSample) -> CreatorMetricSample:
    if isinstance(outcome, CreatorMetricSample):
        return outcome
    return CreatorMetricSample(
        bvid=outcome.bvid or outcome.signal_id or "",
        signal_id=outcome.signal_id,
        direction=outcome.direction,
        review_status=outcome.review_status,
        included=outcome.included,
        mature=outcome.mature,
        entry_price=outcome.entry_price,
        return_1d=outcome.return_1d,
        return_5d=outcome.return_5d,
        return_20d=outcome.return_20d,
        confidence=None,
        manual_revision=False,
        disposition=outcome.review_status.value,
    )


def _unique_eligible_samples(samples: Sequence[CreatorMetricSample]) -> list[CreatorMetricSample]:
    unique_samples: list[CreatorMetricSample] = []
    seen_signal_ids: set[str] = set()
    for sample in samples:
        signal_id = sample.signal_id or sample.bvid
        if signal_id is None or signal_id in seen_signal_ids:
            continue
        if (
            sample.direction not in (Direction.BULLISH, Direction.BEARISH)
            or sample.review_status is not ReviewStatus.APPROVED
            or not sample.included
            or sample.entry_price is None
            or not sample.mature
            or sample.return_5d is None
        ):
            continue
        seen_signal_ids.add(signal_id)
        unique_samples.append(sample)
    return unique_samples


def _exit_price(bars: Sequence[PriceBar], entry_index: int, horizon: int) -> float | None:
    exit_index = entry_index + horizon - 1
    return bars[exit_index].close if exit_index < len(bars) else None


def _signed_return(entry_price: float, exit_price: float | None, multiplier: float) -> float | None:
    if exit_price is None:
        return None
    return ((exit_price - entry_price) / entry_price) * multiplier


def _compound(returns: Iterable[float | None]) -> float | None:
    values = [value for value in returns if value is not None]
    return math.prod(1 + value for value in values) - 1 if values else None


def _average(returns: Iterable[float | None]) -> float | None:
    values = [value for value in returns if value is not None]
    return sum(values) / len(values) if values else None


def _hit_rate(returns: Iterable[float | None]) -> float | None:
    values = [value for value in returns if value is not None]
    return sum(value > 0 for value in values) / len(values) if values else None


def _best_sample(samples: Sequence[CreatorMetricSample]) -> CreatorMetricSample | None:
    candidates = [sample for sample in samples if sample.return_5d is not None]
    return max(candidates, key=lambda sample: sample.return_5d) if candidates else None


def _worst_sample(samples: Sequence[CreatorMetricSample]) -> CreatorMetricSample | None:
    candidates = [sample for sample in samples if sample.return_5d is not None]
    return min(candidates, key=lambda sample: sample.return_5d) if candidates else None


def _proportion(matches: Iterable[bool]) -> float | None:
    values = list(matches)
    return sum(values) / len(values) if values else None


def _disposition_counts(samples: Sequence[CreatorMetricSample]) -> dict[str, int]:
    counts = {"approved": 0, "needs_review": 0, "excluded": 0, "unanalysed": 0}
    for sample in samples:
        counts[sample.disposition] = counts.get(sample.disposition, 0) + 1
    return counts
