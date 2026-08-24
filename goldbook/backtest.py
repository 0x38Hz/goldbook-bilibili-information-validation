"""Deterministic creator-level signal account simulation using cached daily bars."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Mapping, Sequence

from goldbook.models import Direction, PriceBar, ReviewStatus, SignalAnalysis, Video


_SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


@dataclass(frozen=True)
class BacktestTrade:
    bvid: str
    title: str
    published_at: datetime
    direction: Direction
    is_cash: bool
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    return_pct: float
    balance_after: float


@dataclass(frozen=True)
class EquityPoint:
    trade_date: date
    balance: float


@dataclass(frozen=True)
class BacktestPositionSegment:
    start_date: date
    end_date: date
    kind: str
    bvid: str
    title: str
    direction_label: str
    stage_return: float


@dataclass(frozen=True)
class BacktestResult:
    initial_balance: float
    final_balance: float
    total_return: float
    max_drawdown: float
    long_count: int
    short_count: int
    cash_count: int
    signal_count: int
    start_date: date | None
    end_date: date | None
    trades: tuple[BacktestTrade, ...]
    equity_curve: tuple[EquityPoint, ...]
    position_segments: tuple[BacktestPositionSegment, ...]


@dataclass(frozen=True)
class _Signal:
    video: Video
    direction: Direction
    is_cash: bool
    entry_index: int


def backtest_creator(
    videos: Sequence[Video],
    analyses: Mapping[str, SignalAnalysis],
    bars: Sequence[PriceBar],
    initial_balance: float = 100.0,
) -> BacktestResult:
    """Run one next-complete-day 1x signal trade, otherwise staying in cash."""
    if initial_balance <= 0:
        raise ValueError("initial balance must be positive")
    ordered_bars = tuple(bars)
    _validate_bars(ordered_bars)
    if not ordered_bars:
        return _empty_result(initial_balance, None, None)

    by_entry_date: dict[date, _Signal] = {}
    for video in videos:
        analysis = analyses.get(video.bvid)
        if analysis is None or not _is_eligible(analysis):
            continue
        publication_date = video.published_at.astimezone(_SHANGHAI).date()
        entry_index = next(
            (
                index
                for index, bar in enumerate(ordered_bars)
                if bar.trade_date > publication_date
            ),
            None,
        )
        if entry_index is None:
            continue
        is_cash = analysis.direction not in {
            Direction.BULLISH,
            Direction.BEARISH,
        }
        candidate = _Signal(video, analysis.direction, is_cash, entry_index)
        existing = by_entry_date.get(ordered_bars[entry_index].trade_date)
        if existing is None or video.published_at > existing.video.published_at:
            by_entry_date[ordered_bars[entry_index].trade_date] = candidate

    signals = sorted(by_entry_date.values(), key=lambda value: value.entry_index)
    signal_by_entry_index = {signal.entry_index: signal for signal in signals}
    balance = initial_balance
    trades: list[BacktestTrade] = []
    curve: list[EquityPoint] = []
    position_segments: list[BacktestPositionSegment] = []
    for entry_index, entry_bar in enumerate(ordered_bars):
        signal = signal_by_entry_index.get(entry_index)
        direction = signal.direction if signal else Direction.NEUTRAL
        is_cash = signal.is_cash if signal else True
        multiplier = (
            0.0
            if is_cash
            else 1.0
            if direction is Direction.BULLISH
            else -1.0
        )
        exit_price = entry_bar.close
        segment_return = ((exit_price - entry_bar.open) / entry_bar.open) * multiplier
        entry_balance = balance
        balance = entry_balance * (1 + segment_return)
        _append_equity(curve, entry_bar.trade_date, balance)
        position_segments.append(
            BacktestPositionSegment(
                start_date=entry_bar.trade_date,
                end_date=entry_bar.trade_date,
                kind=(
                    "cash"
                    if is_cash
                    else "long"
                    if direction is Direction.BULLISH
                    else "short"
                ),
                bvid=signal.video.bvid if signal else "",
                title=(
                    signal.video.title
                    if signal
                    else "没有可执行信号，资金保持现金"
                ),
                direction_label=(
                    "空仓现金"
                    if is_cash
                    else "做多"
                    if direction is Direction.BULLISH
                    else "做空"
                ),
                stage_return=segment_return,
            )
        )
        if signal:
            trades.append(
                BacktestTrade(
                    bvid=signal.video.bvid,
                    title=signal.video.title,
                    published_at=signal.video.published_at,
                    direction=direction,
                    is_cash=is_cash,
                    entry_date=entry_bar.trade_date,
                    exit_date=entry_bar.trade_date,
                    entry_price=entry_bar.open,
                    exit_price=exit_price,
                    return_pct=segment_return,
                    balance_after=balance,
                )
            )
    return BacktestResult(
        initial_balance=initial_balance,
        final_balance=balance,
        total_return=(balance / initial_balance) - 1,
        max_drawdown=_max_drawdown(curve),
        long_count=sum(trade.direction is Direction.BULLISH for trade in trades),
        short_count=sum(trade.direction is Direction.BEARISH for trade in trades),
        cash_count=sum(segment.kind == "cash" for segment in position_segments),
        signal_count=len(trades),
        start_date=ordered_bars[0].trade_date,
        end_date=ordered_bars[-1].trade_date,
        trades=tuple(trades),
        equity_curve=tuple(curve),
        position_segments=tuple(position_segments),
    )


def _validate_bars(bars: tuple[PriceBar, ...]) -> None:
    for index, bar in enumerate(bars):
        if min(bar.open, bar.high, bar.low, bar.close) <= 0:
            raise ValueError("price bars must be positive")
        if index and bars[index - 1].trade_date >= bar.trade_date:
            raise ValueError("price bars must be strictly increasing")


def _is_eligible(analysis: SignalAnalysis) -> bool:
    return (
        analysis.review_status is not ReviewStatus.EXCLUDED
        and not analysis.is_news_only
        and not analysis.is_retrospective
    )


def _append_equity(points: list[EquityPoint], trade_date: date, balance: float) -> None:
    point = EquityPoint(trade_date, balance)
    if points and points[-1].trade_date == trade_date:
        points[-1] = point
    else:
        points.append(point)


def _max_drawdown(points: Sequence[EquityPoint]) -> float:
    peak = points[0].balance
    maximum = 0.0
    for point in points:
        peak = max(peak, point.balance)
        maximum = max(maximum, (peak - point.balance) / peak)
    return maximum


def _empty_result(
    initial_balance: float, start_date: date | None, end_date: date | None
) -> BacktestResult:
    curve = (
        ()
        if start_date is None
        else (
            EquityPoint(start_date, initial_balance),
            *((EquityPoint(end_date, initial_balance),) if end_date != start_date else ()),
        )
    )
    return BacktestResult(
        initial_balance=initial_balance,
        final_balance=initial_balance,
        total_return=0.0,
        max_drawdown=0.0,
        long_count=0,
        short_count=0,
        cash_count=0,
        signal_count=0,
        start_date=start_date,
        end_date=end_date,
        trades=(),
        equity_curve=curve,
        position_segments=(),
    )
