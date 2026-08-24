from datetime import datetime, timezone

import pytest

from goldbook.backtest import backtest_creator
from goldbook.models import Direction, PriceBar, ReviewStatus, SignalAnalysis, Video


def _video(bvid: str, day: int, hour: int = 8) -> Video:
    return Video(
        bvid,
        "42",
        f"观点 {bvid}",
        datetime(2026, 8, day, hour, tzinfo=timezone.utc),
        60,
        f"https://www.bilibili.com/video/{bvid}",
    )


def _analysis(bvid: str, direction: Direction) -> SignalAnalysis:
    return SignalAnalysis(
        bvid=bvid,
        transcript_hash="hash",
        direction=direction,
        strength=3,
        confidence=0.8,
        summary="测试信号",
        review_status=ReviewStatus.APPROVED,
        revision=1,
    )


BARS = [
    PriceBar("2026-08-01", 95, 101, 94, 100),
    PriceBar("2026-08-02", 100, 112, 99, 105),
    PriceBar("2026-08-03", 110, 112, 95, 100),
    PriceBar("2026-08-04", 90, 92, 84, 85),
    PriceBar("2026-08-05", 82, 85, 79, 80),
]


def test_each_signal_opens_and_closes_within_its_next_complete_trading_day():
    bullish = _video("BVBULL", 1, 1)
    bearish = _video("BVBEAR", 2, 8)

    result = backtest_creator(
        [bullish, bearish],
        {
            bullish.bvid: _analysis(bullish.bvid, Direction.BULLISH),
            bearish.bvid: _analysis(bearish.bvid, Direction.BEARISH),
        },
        BARS,
    )

    assert [trade.direction for trade in result.trades] == [
        Direction.BULLISH,
        Direction.BEARISH,
    ]
    assert result.trades[0].entry_date.isoformat() == "2026-08-02"
    assert result.trades[0].exit_date.isoformat() == "2026-08-02"
    assert result.trades[0].exit_price == 105
    assert result.trades[0].balance_after == pytest.approx(105)
    assert result.trades[1].entry_date.isoformat() == "2026-08-03"
    assert result.trades[1].exit_date.isoformat() == "2026-08-03"
    assert result.trades[1].exit_price == 100
    assert result.final_balance == pytest.approx(105 * (1 + (110 - 100) / 110))


def test_neutral_moves_account_to_cash_and_same_entry_day_uses_latest_video():
    early = _video("BVEARLY", 1, 1)
    late = _video("BVLATE", 1, 12)

    result = backtest_creator(
        [early, late],
        {
            early.bvid: _analysis(early.bvid, Direction.BULLISH),
            late.bvid: _analysis(late.bvid, Direction.NEUTRAL),
        },
        BARS,
    )

    assert result.signal_count == 1
    assert result.trades[0].bvid == late.bvid
    assert result.trades[0].direction is Direction.NEUTRAL
    assert result.trades[0].is_cash is True
    assert result.trades[0].return_pct == 0
    assert result.final_balance == 100
    assert result.cash_count == len(BARS)


def test_no_signal_keeps_one_hundred_dollars():
    result = backtest_creator([], {}, BARS)

    assert result.final_balance == 100.0
    assert result.total_return == 0.0
    assert result.max_drawdown == 0.0
    assert result.trades == ()


def test_backtest_rejects_duplicate_or_non_positive_price_bars():
    with pytest.raises(ValueError, match="strictly increasing"):
        backtest_creator([], {}, [BARS[0], BARS[0]])
    with pytest.raises(ValueError, match="positive"):
        backtest_creator([], {}, [PriceBar("2026-08-01", 0, 1, 1, 1)])


def test_position_segments_show_cash_on_days_without_a_video_signal():
    bullish = _video("BVLONG", 1, 1)
    bearish = _video("BVSHORT", 2, 8)
    neutral = _video("BVDEFAULT", 3, 8)

    result = backtest_creator(
        [bullish, bearish, neutral],
        {
            bullish.bvid: _analysis(bullish.bvid, Direction.BULLISH),
            bearish.bvid: _analysis(bearish.bvid, Direction.BEARISH),
            neutral.bvid: _analysis(neutral.bvid, Direction.NEUTRAL),
        },
        BARS,
    )

    assert [segment.kind for segment in result.position_segments] == [
        "cash",
        "long",
        "short",
        "cash",
        "cash",
    ]
    assert result.position_segments[0].start_date.isoformat() == "2026-08-01"
    assert result.position_segments[0].title == "没有可执行信号，资金保持现金"
    assert result.position_segments[1].start_date.isoformat() == "2026-08-02"
    assert result.position_segments[1].end_date.isoformat() == "2026-08-02"
    assert result.position_segments[2].start_date.isoformat() == "2026-08-03"
    assert result.position_segments[2].end_date.isoformat() == "2026-08-03"
    assert result.position_segments[3].start_date.isoformat() == "2026-08-04"
    assert result.position_segments[-1].start_date.isoformat() == "2026-08-05"
    assert result.position_segments[-1].end_date.isoformat() == "2026-08-05"
