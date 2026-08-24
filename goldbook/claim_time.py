"""Strict post-publication daily-bar windows for forecast claims."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
import re
from typing import Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from goldbook.models import (
    ClaimStatus,
    ClaimType,
    ForecastClaim,
    HorizonSource,
    IntradayPriceBar,
    PriceBar,
    Video,
)


try:
    _SHANGHAI = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:  # Windows embeddable Python may omit the tzdata wheel.
    _SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
_INTRADAY_MARKERS = ("今天", "今日", "今晚", "日内", "小时", "分钟", "午后", "早盘", "晚盘")
_HOUR_COUNT = re.compile(
    r"(?:未来|接下来)?\s*([0-9]+|一|二|两|三|四|五|六|七|八|九|十|十一|十二)\s*个?小时"
)


@dataclass(frozen=True)
class ClaimWindow:
    start_date: date | None
    end_date: date | None
    mature: bool
    reason: str | None


@dataclass(frozen=True)
class IntradayClaimWindow:
    start_at: datetime | None
    end_at: datetime | None
    mature: bool
    reason: str | None
    bars: tuple[IntradayPriceBar, ...]


def is_primary_trend(claim: ForecastClaim) -> bool:
    """Identify the one stable video-level trend emitted by the v2 extractor."""
    return (
        claim.claim_index == 0
        and claim.claim_type is ClaimType.DIRECTIONAL_MOVE
    )


def find_next_same_instrument_prediction(
    claim: ForecastClaim,
    video: Video,
    creator_videos: list[Video] | tuple[Video, ...],
    creator_claims: list[ForecastClaim] | tuple[ForecastClaim, ...],
) -> datetime | None:
    """Find the next actionable publication without changing explicit horizons."""
    videos = {item.bvid: item for item in creator_videos}
    primary_only = is_primary_trend(claim)
    candidates = []
    for candidate in creator_claims:
        candidate_video = videos.get(candidate.bvid)
        if (
            candidate_video is None
            or candidate_video.creator_uid != video.creator_uid
            or candidate_video.published_at <= video.published_at
            or candidate.instrument is not claim.instrument
            or (primary_only and not is_primary_trend(candidate))
            or candidate.status
            not in {ClaimStatus.AUTO_VALIDATED, ClaimStatus.HUMAN_CORRECTED}
        ):
            continue
        candidates.append(candidate_video.published_at)
    return min(candidates) if candidates else None


def resolve_claim_window(
    claim: ForecastClaim,
    video: Video,
    bars: list[PriceBar] | tuple[PriceBar, ...],
    *,
    next_same_instrument_prediction_at: datetime | None = None,
    superseded_at: datetime | None = None,
    evaluated_at: datetime,
) -> ClaimWindow:
    """Return only complete daily bars strictly after publication."""
    _require_aware(evaluated_at, "evaluated_at")
    _require_aware(next_same_instrument_prediction_at, "next prediction")
    _require_aware(superseded_at, "superseded_at")
    publication_date = video.published_at.astimezone(_SHANGHAI).date()
    evaluation_date = evaluated_at.astimezone(_SHANGHAI).date()
    eligible = tuple(
        bar
        for bar in sorted(bars, key=lambda value: value.trade_date)
        if publication_date < bar.trade_date < evaluation_date
    )

    if is_intraday_claim(claim):
        return ClaimWindow(None, None, False, "unresolved_intraday_data")

    if superseded_at is not None:
        cutoff = superseded_at.astimezone(_SHANGHAI).date()
        observed = tuple(bar for bar in eligible if bar.trade_date < cutoff)
        return ClaimWindow(
            None if not observed else observed[0].trade_date,
            None if not observed else observed[-1].trade_date,
            True,
            "superseded",
        )

    if claim.horizon_source is HorizonSource.UNKNOWN:
        return _resolve_unknown_horizon(
            eligible, next_same_instrument_prediction_at, evaluated_at
        )

    if claim.deadline_at is not None:
        return _resolve_deadline(eligible, claim.deadline_at, evaluated_at)

    maximum = claim.horizon_max_trading_days
    if maximum is None:
        return ClaimWindow(
            None if not eligible else eligible[0].trade_date,
            None if not eligible else eligible[-1].trade_date,
            False,
            "horizon_not_resolved",
        )
    if not eligible:
        return ClaimWindow(None, None, False, "awaiting_first_complete_bar")
    if len(eligible) < maximum:
        return ClaimWindow(
            eligible[0].trade_date,
            eligible[-1].trade_date,
            False,
            "horizon_not_mature",
        )
    return ClaimWindow(
        eligible[0].trade_date,
        eligible[maximum - 1].trade_date,
        True,
        None,
    )


def _resolve_unknown_horizon(
    eligible: tuple[PriceBar, ...],
    next_prediction_at: datetime | None,
    evaluated_at: datetime,
) -> ClaimWindow:
    if next_prediction_at is None:
        return ClaimWindow(
            None if not eligible else eligible[0].trade_date,
            None if not eligible else eligible[-1].trade_date,
            False,
            "horizon_unknown_awaiting_next_prediction",
        )
    cutoff = next_prediction_at.astimezone(_SHANGHAI).date()
    observed = tuple(bar for bar in eligible if bar.trade_date < cutoff)
    mature = evaluated_at >= next_prediction_at
    return ClaimWindow(
        None if not observed else observed[0].trade_date,
        None if not observed else observed[-1].trade_date,
        mature,
        None if mature and observed else "awaiting_first_complete_bar",
    )


def _resolve_deadline(
    eligible: tuple[PriceBar, ...], deadline_at: datetime, evaluated_at: datetime
) -> ClaimWindow:
    deadline_date = deadline_at.astimezone(_SHANGHAI).date()
    observed = tuple(bar for bar in eligible if bar.trade_date < deadline_date)
    mature = evaluated_at >= deadline_at
    return ClaimWindow(
        None if not observed else observed[0].trade_date,
        None if not observed else observed[-1].trade_date,
        mature,
        None if mature and observed else (
            "awaiting_first_complete_bar" if not observed else "horizon_not_mature"
        ),
    )


def is_intraday_claim(claim: ForecastClaim) -> bool:
    text = claim.horizon_text or ""
    return (
        claim.horizon_max_trading_days == 0
        or any(marker in text for marker in _INTRADAY_MARKERS)
    )


def is_unknown_horizon_claim(claim: ForecastClaim) -> bool:
    """Unknown horizons remain open only until the next comparable prediction."""
    return claim.horizon_source is HorizonSource.UNKNOWN


def resolve_unknown_horizon_intraday_window(
    claim: ForecastClaim,
    video: Video,
    bars: Sequence[IntradayPriceBar],
    *,
    next_same_instrument_prediction_at: datetime | None = None,
    superseded_at: datetime | None = None,
    evaluated_at: datetime,
) -> IntradayClaimWindow:
    """Evaluate an unbounded claim on complete hours until the next prediction."""
    _require_aware(evaluated_at, "evaluated_at")
    _require_aware(next_same_instrument_prediction_at, "next prediction")
    _require_aware(superseded_at, "superseded_at")
    if not is_unknown_horizon_claim(claim):
        raise ValueError("claim does not have an unknown horizon")

    cutoffs: list[tuple[datetime, str | None]] = [(evaluated_at, "horizon_unknown_awaiting_next_prediction")]
    if next_same_instrument_prediction_at is not None:
        cutoffs.append((next_same_instrument_prediction_at.astimezone(timezone.utc), None))
    if superseded_at is not None:
        cutoffs.append((superseded_at.astimezone(timezone.utc), "superseded"))
    effective_end, cutoff_reason = min(cutoffs, key=lambda item: item[0])
    observed = tuple(
        bar
        for bar in sorted(bars, key=lambda item: item.started_at)
        if bar.started_at >= video.published_at
        and bar.started_at + timedelta(minutes=bar.interval_minutes) <= effective_end
    )
    mature = (
        cutoff_reason != "horizon_unknown_awaiting_next_prediction"
        and evaluated_at >= effective_end
    )
    if cutoff_reason == "superseded" and mature:
        reason = "superseded"
    elif not observed:
        reason = "unresolved_intraday_data"
    elif not mature:
        reason = "horizon_unknown_awaiting_next_prediction"
    else:
        reason = None
    return IntradayClaimWindow(
        start_at=None if not observed else observed[0].started_at,
        end_at=effective_end,
        mature=mature,
        reason=reason,
        bars=observed,
    )


def is_event_activated_claim(claim: ForecastClaim) -> bool:
    """Identify a forecast that starts after a timestamped future event."""
    timing_text = f"{claim.horizon_text or ''}\n{claim.condition_text}"
    return (
        claim.deadline_at is not None
        and (claim.horizon_max_trading_days or 0) > 0
        and any(
            marker in timing_text
            for marker in ("公布后", "发布后", "出炉后", "揭晓后", "决议后")
        )
    )


def resolve_event_activated_claim_window(
    claim: ForecastClaim,
    video: Video,
    daily_bars: Sequence[PriceBar],
    intraday_bars: Sequence[IntradayPriceBar],
    *,
    next_same_instrument_prediction_at: datetime | None = None,
    superseded_at: datetime | None = None,
    evaluated_at: datetime,
) -> IntradayClaimWindow:
    """Use the event time as activation and the stated daily horizon as expiry."""
    if not is_event_activated_claim(claim):
        raise ValueError("claim is not activated by a timestamped event")
    daily_window = resolve_claim_window(
        replace(claim, deadline_at=None),
        video,
        tuple(daily_bars),
        next_same_instrument_prediction_at=next_same_instrument_prediction_at,
        superseded_at=superseded_at,
        evaluated_at=evaluated_at,
    )
    if daily_window.end_date is None:
        return IntradayClaimWindow(
            None, None, False, daily_window.reason or "awaiting_first_complete_bar", ()
        )

    activation_at = claim.deadline_at.astimezone(timezone.utc)
    local_end = datetime.combine(
        daily_window.end_date + timedelta(days=1), time.min, tzinfo=_SHANGHAI
    )
    end_at = local_end.astimezone(timezone.utc)
    if activation_at >= end_at:
        return IntradayClaimWindow(
            None, end_at, False, "invalid_event_activation_window", ()
        )
    observation_end = min(end_at, evaluated_at)
    observed = tuple(
        bar
        for bar in sorted(intraday_bars, key=lambda item: item.started_at)
        if bar.started_at >= activation_at
        and bar.started_at + timedelta(minutes=bar.interval_minutes) <= observation_end
    )
    mature = daily_window.mature and evaluated_at >= end_at
    if daily_window.reason == "superseded":
        reason = "superseded"
    elif not observed:
        reason = "unresolved_intraday_data"
    elif not mature:
        reason = "intraday_horizon_not_mature"
    else:
        reason = None
    return IntradayClaimWindow(
        start_at=None if not observed else observed[0].started_at,
        end_at=end_at,
        mature=mature,
        reason=reason,
        bars=observed,
    )


def resolve_intraday_claim_window(
    claim: ForecastClaim,
    video: Video,
    bars: Sequence[IntradayPriceBar],
    *,
    next_same_instrument_prediction_at: datetime | None = None,
    superseded_at: datetime | None = None,
    evaluated_at: datetime,
) -> IntradayClaimWindow:
    """Select only complete hours that begin at or after publication."""
    _require_aware(evaluated_at, "evaluated_at")
    _require_aware(next_same_instrument_prediction_at, "next prediction")
    _require_aware(superseded_at, "superseded_at")

    semantic_deadline = _intraday_deadline(claim, video.published_at)
    cutoffs: list[tuple[datetime, str | None]] = [(semantic_deadline, None)]
    if next_same_instrument_prediction_at is not None:
        cutoffs.append(
            (next_same_instrument_prediction_at.astimezone(timezone.utc), None)
        )
    if superseded_at is not None:
        cutoffs.append((superseded_at.astimezone(timezone.utc), "superseded"))
    effective_end, cutoff_reason = min(cutoffs, key=lambda item: item[0])
    observation_end = min(effective_end, evaluated_at)
    observed = tuple(
        bar
        for bar in sorted(bars, key=lambda item: item.started_at)
        if bar.started_at >= video.published_at
        and bar.started_at + timedelta(minutes=bar.interval_minutes) <= observation_end
    )
    mature = evaluated_at >= effective_end

    if cutoff_reason == "superseded" and mature:
        reason = "superseded"
    elif not observed:
        reason = "unresolved_intraday_data"
    elif not mature:
        reason = "intraday_horizon_not_mature"
    else:
        reason = None
    return IntradayClaimWindow(
        start_at=None if not observed else observed[0].started_at,
        end_at=effective_end,
        mature=mature,
        reason=reason,
        bars=observed,
    )


def _intraday_deadline(claim: ForecastClaim, published_at: datetime) -> datetime:
    if claim.deadline_at is not None:
        return claim.deadline_at.astimezone(timezone.utc)

    text = claim.horizon_text or ""
    hours = _hours_from_text(text)
    if hours is not None:
        return published_at.astimezone(timezone.utc) + timedelta(hours=hours)

    local = published_at.astimezone(_SHANGHAI)
    if any(word in text for word in ("今晚", "晚间", "夜间")):
        six = local.replace(hour=6, minute=0, second=0, microsecond=0)
        if six <= local:
            six += timedelta(days=1)
        return six.astimezone(timezone.utc)

    midnight = (local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return midnight.astimezone(timezone.utc)


def _hours_from_text(text: str) -> int | None:
    match = _HOUR_COUNT.search(text)
    if match is None:
        return None
    token = match.group(1)
    if token.isdigit():
        value = int(token)
    else:
        values = {
            "一": 1,
            "二": 2,
            "两": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
            "十": 10,
            "十一": 11,
            "十二": 12,
        }
        value = values[token]
    return value if value > 0 else None


def _require_aware(value: datetime | None, field_name: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field_name} must be timezone-aware")
