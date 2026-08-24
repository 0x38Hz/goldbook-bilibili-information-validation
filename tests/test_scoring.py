from datetime import date, datetime, timezone

import pytest

from goldbook.models import CreatorMetricSample, Direction, Outcome, PriceBar, ReviewStatus, SignalAnalysis
from goldbook.scoring import aggregate_creator, score_signal


def analysis(
    direction: Direction,
    *,
    bvid: str | None = None,
    revision: int = 0,
    review_status: ReviewStatus = ReviewStatus.APPROVED,
    is_retrospective: bool = False,
    is_news_only: bool = False,
) -> SignalAnalysis:
    return SignalAnalysis(
        direction=direction,
        strength=4,
        confidence=0.9,
        conditions=(),
        is_retrospective=is_retrospective,
        is_news_only=is_news_only,
        evidence=(),
        summary="测试",
        review_status=review_status,
        bvid=bvid,
        revision=revision,
    )


def bars(count: int = 20) -> list[PriceBar]:
    return [
        PriceBar(date(2026, 8, day), 100.0, 102.0, 99.0, 100.0 + (day - 2) * 5)
        for day in range(3, 3 + count)
    ]


def test_uses_next_bar_open_and_entry_day_close_for_one_day_exit():
    result = score_signal(
        analysis(Direction.BULLISH), datetime(2026, 8, 3, 12, tzinfo=timezone.utc), bars()
    )

    assert result.entry_date == date(2026, 8, 4)
    assert result.entry_price == 100.0
    assert result.exit_1d == 110.0
    assert result.return_1d == 0.1


def test_uses_fifth_and_twentieth_bars_including_entry_for_maturity():
    result = score_signal(
        analysis(Direction.BULLISH), datetime(2026, 8, 2, 12, tzinfo=timezone.utc), bars()
    )

    assert result.exit_5d == 125.0
    assert result.return_5d == 0.25
    assert result.exit_20d == 200.0
    assert result.return_20d == 1.0
    assert result.mature is True


def test_flips_bearish_returns():
    published = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
    long_result = score_signal(analysis(Direction.BULLISH), published, bars())
    short_result = score_signal(analysis(Direction.BEARISH), published, bars())

    assert long_result.return_5d == -short_result.return_5d


def test_five_day_matured_result_can_lack_twenty_day_return():
    result = score_signal(
        analysis(Direction.BULLISH), datetime(2026, 8, 2, 12, tzinfo=timezone.utc), bars(5)
    )

    assert result.return_5d is not None
    assert result.return_20d is None
    assert result.mature is True


def test_prices_unapproved_directional_analysis_but_keeps_it_out_of_ranking():
    unapproved_result = score_signal(
        analysis(Direction.BULLISH, review_status=ReviewStatus.NEEDS_REVIEW),
        datetime(2026, 8, 2, 12, tzinfo=timezone.utc),
        bars(),
    )
    neutral_result = score_signal(
        analysis(Direction.NEUTRAL), datetime(2026, 8, 2, 12, tzinfo=timezone.utc), bars()
    )

    assert unapproved_result.entry_price == 100.0
    assert unapproved_result.return_5d == 0.25
    assert unapproved_result.included is False
    assert neutral_result.entry_price is None


@pytest.mark.parametrize("kwargs", [{"is_retrospective": True}, {"is_news_only": True}])
def test_excludes_retrospective_and_news_only_analyses(kwargs):
    result = score_signal(
        analysis(Direction.BULLISH, **kwargs),
        datetime(2026, 8, 2, 12, tzinfo=timezone.utc),
        bars(),
    )

    assert result.entry_price is None


def test_does_not_use_a_same_day_or_earlier_bar_when_no_future_bar_exists():
    result = score_signal(
        analysis(Direction.BULLISH), datetime(2026, 8, 3, 12, tzinfo=timezone.utc), bars(1)
    )

    assert result.entry_price is None


def _outcome(
    signal_id: str,
    *,
    return_5d: float | None = 0.1,
    return_20d: float | None = None,
    review_status: ReviewStatus = ReviewStatus.APPROVED,
    included: bool = True,
) -> Outcome:
    return Outcome(
        Direction.BULLISH,
        entry_price=100.0,
        signal_id=signal_id,
        review_status=review_status,
        included=included,
        return_1d=0.05,
        return_5d=return_5d,
        return_20d=return_20d,
        mature=return_5d is not None,
    )


def test_duplicate_signal_cannot_satisfy_formal_rank_threshold():
    repeated = _outcome("BV1TEST:0")

    metrics = aggregate_creator([repeated, repeated, repeated])

    assert metrics.scored_count == 1
    assert metrics.eligible_for_rank is False


def test_three_unique_five_day_matured_signals_are_rank_eligible_without_twenty_days():
    metrics = aggregate_creator([_outcome("BV1:0"), _outcome("BV2:0"), _outcome("BV3:0")])

    assert metrics.scored_count == 3
    assert metrics.mature_count == 3
    assert metrics.compound_return_20d is None
    assert metrics.eligible_for_rank is True


def test_score_signal_emits_a_stable_identifier_from_video_and_revision():
    result = score_signal(
        analysis(Direction.BULLISH, bvid="BV1TEST", revision=2),
        datetime(2026, 8, 2, 12, tzinfo=timezone.utc),
        bars(),
    )

    assert result.signal_id == "BV1TEST"


def test_aggregates_only_approved_included_priced_five_day_matured_values():
    outcomes = [
        Outcome(
            Direction.BULLISH,
            entry_price=100.0,
            signal_id="BV1:0",
            review_status=ReviewStatus.APPROVED,
            included=True,
            return_1d=0.1,
            return_5d=0.2,
            mature=True,
        ),
        Outcome(
            Direction.BEARISH,
            entry_price=100.0,
            signal_id="BV2:0",
            review_status=ReviewStatus.APPROVED,
            included=True,
            return_1d=-0.05,
            return_5d=-0.1,
            mature=True,
        ),
        _outcome("BV3:0", review_status=ReviewStatus.NEEDS_REVIEW),
        _outcome("BV4:0", included=False),
        _outcome("BV5:0", return_5d=None),
        Outcome(
            Direction.NEUTRAL,
            entry_price=100.0,
            signal_id="BV6:0",
            review_status=ReviewStatus.APPROVED,
            included=True,
            return_1d=0.6,
            return_5d=0.6,
            mature=True,
        ),
    ]

    metrics = aggregate_creator(outcomes)

    assert metrics.scored_count == 2
    assert metrics.compound_return_1d == pytest.approx(0.045)
    assert metrics.compound_return_5d == pytest.approx(0.08)
    assert metrics.compound_return_20d is None


def test_aggregate_creator_exposes_horizon_metrics_samples_and_transparent_proportions():
    approved_bullish = CreatorMetricSample(
        bvid="BV1", signal_id="BV1", direction=Direction.BULLISH,
        review_status=ReviewStatus.APPROVED, included=True, mature=True,
        entry_price=100.0, return_1d=0.1, return_5d=0.2, return_20d=-0.1,
        confidence=0.8, manual_revision=False, disposition="approved",
    )
    approved_bearish = CreatorMetricSample(
        bvid="BV2", signal_id="BV2", direction=Direction.BEARISH,
        review_status=ReviewStatus.APPROVED, included=True, mature=True,
        entry_price=100.0, return_1d=-0.2, return_5d=0.0, return_20d=0.3,
        confidence=0.69, manual_revision=True, disposition="approved",
    )
    awaiting_review = CreatorMetricSample(
        bvid="BV3", signal_id="BV3", direction=Direction.BULLISH,
        review_status=ReviewStatus.NEEDS_REVIEW, included=False, mature=False,
        entry_price=None, return_1d=None, return_5d=None, return_20d=None,
        confidence=0.2, manual_revision=False, disposition="needs_review",
    )

    metrics = aggregate_creator([approved_bullish, approved_bearish, awaiting_review])

    assert metrics.bullish_count == 1
    assert metrics.bearish_count == 1
    assert metrics.hit_rate_1d == 0.5
    assert metrics.hit_rate_5d == 0.5
    assert metrics.hit_rate_20d == 0.5
    assert metrics.average_signed_return_1d == pytest.approx(-0.05)
    assert metrics.average_signed_return_5d == pytest.approx(0.1)
    assert metrics.average_signed_return_20d == pytest.approx(0.1)
    assert metrics.best_sample == approved_bullish
    assert metrics.worst_sample == approved_bearish
    assert metrics.low_confidence_proportion == pytest.approx(2 / 3)
    assert metrics.manual_revision_proportion == pytest.approx(1 / 3)
    assert metrics.disposition_counts == {
        "approved": 2, "needs_review": 1, "excluded": 0, "unanalysed": 0,
    }


def test_aggregate_creator_uses_none_for_zero_denominators_and_zero_for_counts():
    metrics = aggregate_creator([])

    assert metrics.bullish_count == 0
    assert metrics.bearish_count == 0
    assert metrics.hit_rate_1d is None
    assert metrics.hit_rate_5d is None
    assert metrics.hit_rate_20d is None
    assert metrics.average_signed_return_1d is None
    assert metrics.average_signed_return_5d is None
    assert metrics.average_signed_return_20d is None
    assert metrics.best_sample is None
    assert metrics.worst_sample is None
    assert metrics.low_confidence_proportion is None
    assert metrics.manual_revision_proportion is None
    assert metrics.disposition_counts == {
        "approved": 0, "needs_review": 0, "excluded": 0, "unanalysed": 0,
    }
