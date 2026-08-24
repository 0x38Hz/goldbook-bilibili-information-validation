"""Deterministic OHLC evaluation for transcript-grounded forecast claims."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Sequence

from goldbook.claim_time import (
    ClaimWindow,
    IntradayClaimWindow,
    find_next_same_instrument_prediction,
    is_event_activated_claim,
    is_intraday_claim,
    is_unknown_horizon_claim,
    is_primary_trend,
    resolve_claim_window,
    resolve_event_activated_claim_window,
    resolve_intraday_claim_window,
    resolve_unknown_horizon_intraday_window,
)
from goldbook.db import Database
from goldbook.fact_check import BranchStatus, FactCheckImpact, FactCheckResult
from goldbook.models import (
    ClaimEvaluation,
    ClaimLeg,
    ClaimStatus,
    ClaimType,
    Direction,
    EvaluationVerdict,
    ForecastClaim,
    Instrument,
    IntradayPriceBar,
    PriceBar,
    Video,
)


_NEAR_THRESHOLD = 0.005
ObservedBar = PriceBar | IntradayPriceBar
ObservationMoment = date | datetime


@dataclass(frozen=True)
class ClaimRecomputationSummary:
    evaluated: int
    unresolved: int
    deleted: int
    failed: int


def recompute_claim_evaluations(
    database: Database, *, evaluated_at: datetime
) -> ClaimRecomputationSummary:
    """Rebuild latest claim evaluations from cached OHLC without model calls."""
    bars = database.list_price_bars()
    intraday_bars = database.list_intraday_price_bars()
    live_ids: set[str] = set()
    evaluated = 0
    unresolved = 0
    failed = 0
    for creator in database.list_creators():
        videos = database.list_videos(creator.uid)
        videos_by_bvid = {video.bvid: video for video in videos}
        claims = database.list_creator_forecast_claims(creator.uid)
        for claim in claims:
            live_ids.add(claim.claim_id)
            video = videos_by_bvid.get(claim.bvid)
            if video is None:
                failed += 1
                continue
            cutoff = find_next_same_instrument_prediction(
                claim, video, videos, claims
            )
            superseding_dates = [
                videos_by_bvid[candidate.bvid].published_at
                for candidate in claims
                if candidate.supersedes_claim_id == claim.claim_id
                and candidate.bvid in videos_by_bvid
            ]
            superseded_at = min(superseding_dates) if superseding_dates else None
            if is_event_activated_claim(claim):
                intraday_window = IntradayClaimWindow(
                    None, None, False, "invalid_claim_structure", ()
                )
                try:
                    intraday_window = resolve_event_activated_claim_window(
                        claim,
                        video,
                        bars,
                        intraday_bars,
                        next_same_instrument_prediction_at=cutoff,
                        superseded_at=superseded_at,
                        evaluated_at=evaluated_at,
                    )
                    result = evaluate_intraday_claim(
                        claim,
                        video,
                        intraday_window,
                        evaluated_at=evaluated_at,
                    )
                except ValueError:
                    failed += 1
                    result = _unresolved_intraday(
                        claim,
                        evaluated_at,
                        intraday_window,
                        EvaluationVerdict.UNRESOLVED,
                        "invalid_claim_structure",
                    )
            elif is_unknown_horizon_claim(claim) or is_intraday_claim(claim):
                intraday_window = IntradayClaimWindow(
                    None, None, False, "invalid_claim_structure", ()
                )
                try:
                    intraday_window = (
                        resolve_unknown_horizon_intraday_window(
                            claim,
                            video,
                            intraday_bars,
                            next_same_instrument_prediction_at=cutoff,
                            superseded_at=superseded_at,
                            evaluated_at=evaluated_at,
                        )
                        if is_unknown_horizon_claim(claim)
                        else resolve_intraday_claim_window(
                            claim,
                            video,
                            intraday_bars,
                            next_same_instrument_prediction_at=cutoff,
                            superseded_at=superseded_at,
                            evaluated_at=evaluated_at,
                        )
                    )
                    result = evaluate_intraday_claim(
                        claim,
                        video,
                        intraday_window,
                        evaluated_at=evaluated_at,
                    )
                except ValueError:
                    failed += 1
                    result = _unresolved_intraday(
                        claim,
                        evaluated_at,
                        intraday_window,
                        EvaluationVerdict.UNRESOLVED,
                        "invalid_claim_structure",
                    )
            else:
                window = ClaimWindow(None, None, False, "invalid_claim_structure")
                try:
                    window = resolve_claim_window(
                        claim,
                        video,
                        bars,
                        next_same_instrument_prediction_at=cutoff,
                        superseded_at=superseded_at,
                        evaluated_at=evaluated_at,
                    )
                    result = evaluate_claim(
                        claim, video, bars, window, evaluated_at=evaluated_at
                    )
                except ValueError:
                    failed += 1
                    result = _unresolved(
                        claim,
                        evaluated_at,
                        window,
                        EvaluationVerdict.UNRESOLVED,
                        "invalid_claim_structure",
                    )
            stored_fact_check = database.get_current_fact_check(claim.bvid)
            if stored_fact_check is not None:
                result = apply_fact_check_to_claim_evaluation(
                    claim, result, stored_fact_check.result
                )
            database.save_claim_evaluation(result)
            if result.verdict in {
                EvaluationVerdict.UNRESOLVED,
                EvaluationVerdict.NOT_TRIGGERED,
            }:
                unresolved += 1
            else:
                evaluated += 1
    deleted = database.delete_claim_evaluations_except(live_ids)
    return ClaimRecomputationSummary(evaluated, unresolved, deleted, failed)


def apply_fact_check_to_claim_evaluation(
    claim: ForecastClaim,
    evaluation: ClaimEvaluation,
    fact_result: FactCheckResult,
) -> ClaimEvaluation:
    """Overlay external-condition activation without changing price arithmetic."""
    if claim.claim_id != evaluation.claim_id:
        raise ValueError("claim and evaluation identities do not match")
    decision = next(
        (
            candidate
            for candidate in fact_result.branch_decisions
            if candidate.claim_id == claim.claim_id
        ),
        None,
    )
    if decision is None:
        return evaluation
    if fact_result.impact is FactCheckImpact.CONFLICTING:
        return _clear_observation(evaluation, "fact_conflicting")
    if fact_result.impact is FactCheckImpact.INSUFFICIENT:
        return _clear_observation(evaluation, "fact_insufficient")
    if decision.status is BranchStatus.NOT_TRIGGERED:
        return replace(
            _clear_observation(evaluation, "condition_not_triggered"),
            verdict=EvaluationVerdict.NOT_TRIGGERED,
        )
    return evaluation


def _clear_observation(
    evaluation: ClaimEvaluation, reason: str
) -> ClaimEvaluation:
    return replace(
        evaluation,
        window_start=None,
        window_end=None,
        entry_price=None,
        observed_min=None,
        observed_max=None,
        final_close=None,
        closest_price=None,
        closest_date=None,
        distance_pct=None,
        first_hit_date=None,
        window_start_at=None,
        window_end_at=None,
        closest_at=None,
        first_hit_at=None,
        verdict=EvaluationVerdict.UNRESOLVED,
        mature=False,
        reason=reason,
    )


def evaluate_claim(
    claim: ForecastClaim,
    video: Video,
    bars: Sequence[PriceBar],
    window: ClaimWindow,
    *,
    evaluated_at: datetime,
) -> ClaimEvaluation:
    """Evaluate one claim using only the window chosen by the time engine."""
    if claim.status is ClaimStatus.EXCLUDED:
        return _unresolved(
            claim, evaluated_at, window, EvaluationVerdict.EXCLUDED, "excluded"
        )
    if claim.instrument is not Instrument.XAU_USD_SPOT:
        return _unresolved(
            claim,
            evaluated_at,
            window,
            EvaluationVerdict.UNRESOLVED,
            "unresolved_instrument",
        )
    if is_primary_trend(claim) and claim.direction in {
        Direction.NEUTRAL,
        Direction.NO_SIGNAL,
    }:
        return _unresolved(
            claim,
            evaluated_at,
            window,
            EvaluationVerdict.UNRESOLVED,
            "neutral_trend"
            if claim.direction is Direction.NEUTRAL
            else "no_signal",
        )
    if not window.mature:
        return _unresolved(
            claim,
            evaluated_at,
            window,
            EvaluationVerdict.UNRESOLVED,
            window.reason or "horizon_not_mature",
        )
    if window.start_date is None or window.end_date is None:
        verdict = (
            EvaluationVerdict.SUPERSEDED
            if window.reason == "superseded"
            else EvaluationVerdict.UNRESOLVED
        )
        return _unresolved(
            claim,
            evaluated_at,
            window,
            verdict,
            window.reason or "no_complete_daily_bars",
        )

    window_bars = tuple(
        bar
        for bar in sorted(bars, key=lambda value: value.trade_date)
        if window.start_date <= bar.trade_date <= window.end_date
    )
    if not window_bars:
        return _unresolved(
            claim,
            evaluated_at,
            window,
            EvaluationVerdict.UNRESOLVED,
            "no_complete_daily_bars",
        )

    entry_price = window_bars[0].open
    observed_min = min(bar.low for bar in window_bars)
    observed_max = max(bar.high for bar in window_bars)
    final_close = window_bars[-1].close
    verdict, hit_date, closest_price, closest_date, distance, reason = _judge(
        claim, window_bars, entry_price, final_close
    )
    if window.reason == "superseded":
        verdict = EvaluationVerdict.SUPERSEDED
        reason = "superseded"
        hit_date = None
    return ClaimEvaluation(
        claim_id=claim.claim_id,
        evaluated_at=evaluated_at,
        window_start=window.start_date,
        window_end=window.end_date,
        entry_price=entry_price,
        observed_min=observed_min,
        observed_max=observed_max,
        final_close=final_close,
        closest_price=closest_price,
        closest_date=closest_date,
        distance_pct=distance,
        first_hit_date=hit_date,
        verdict=verdict,
        mature=True,
        reason=reason,
    )


def evaluate_intraday_claim(
    claim: ForecastClaim,
    video: Video,
    window: IntradayClaimWindow,
    *,
    evaluated_at: datetime,
) -> ClaimEvaluation:
    """Evaluate one claim on exact complete post-publication hourly bars."""
    if claim.status is ClaimStatus.EXCLUDED:
        return _unresolved_intraday(
            claim, evaluated_at, window, EvaluationVerdict.EXCLUDED, "excluded"
        )
    if claim.instrument is not Instrument.XAU_USD_SPOT:
        return _unresolved_intraday(
            claim,
            evaluated_at,
            window,
            EvaluationVerdict.UNRESOLVED,
            "unresolved_instrument",
        )
    if is_primary_trend(claim) and claim.direction in {
        Direction.NEUTRAL,
        Direction.NO_SIGNAL,
    }:
        return _unresolved_intraday(
            claim,
            evaluated_at,
            window,
            EvaluationVerdict.UNRESOLVED,
            "neutral_trend"
            if claim.direction is Direction.NEUTRAL
            else "no_signal",
        )
    if window.reason == "superseded":
        return _unresolved_intraday(
            claim,
            evaluated_at,
            window,
            EvaluationVerdict.SUPERSEDED,
            "superseded",
        )
    if not window.bars:
        return _unresolved_intraday(
            claim,
            evaluated_at,
            window,
            EvaluationVerdict.UNRESOLVED,
            window.reason or "unresolved_intraday_data",
        )

    bars = window.bars
    entry_price = bars[0].open
    final_close = bars[-1].close
    early_hit_types = {
        ClaimType.TARGET_TOUCH,
        ClaimType.CROSS_ABOVE,
        ClaimType.CROSS_BELOW,
        ClaimType.BREAKOUT_EITHER_SIDE,
        ClaimType.SEQUENCE,
    }
    if not window.mature and claim.claim_type not in early_hit_types:
        return _unresolved_intraday(
            claim,
            evaluated_at,
            window,
            EvaluationVerdict.UNRESOLVED,
            window.reason or "intraday_horizon_not_mature",
        )

    verdict, hit_at, closest_price, closest_at, distance, reason = _judge(
        claim, bars, entry_price, final_close
    )
    if not window.mature and verdict is not EvaluationVerdict.HIT:
        return _unresolved_intraday(
            claim,
            evaluated_at,
            window,
            EvaluationVerdict.UNRESOLVED,
            window.reason or "intraday_horizon_not_mature",
        )

    return ClaimEvaluation(
        claim_id=claim.claim_id,
        evaluated_at=evaluated_at,
        window_start=None,
        window_end=None,
        entry_price=entry_price,
        observed_min=min(bar.low for bar in bars),
        observed_max=max(bar.high for bar in bars),
        final_close=final_close,
        closest_price=closest_price,
        closest_date=None,
        distance_pct=distance,
        first_hit_date=None,
        verdict=verdict,
        mature=window.mature or verdict is EvaluationVerdict.HIT,
        reason=reason,
        window_start_at=window.start_at,
        window_end_at=window.end_at,
        closest_at=closest_at if isinstance(closest_at, datetime) else None,
        first_hit_at=hit_at if isinstance(hit_at, datetime) else None,
    )


def _judge(
    claim: ForecastClaim,
    bars: tuple[ObservedBar, ...],
    entry_price: float,
    final_close: float,
) -> tuple[
    EvaluationVerdict,
    ObservationMoment | None,
    float | None,
    ObservationMoment | None,
    float | None,
    str,
]:
    if claim.claim_type is ClaimType.DIRECTIONAL_MOVE:
        hit = (
            claim.direction is Direction.BULLISH and final_close > entry_price
        ) or (
            claim.direction is Direction.BEARISH and final_close < entry_price
        )
        return (
            EvaluationVerdict.HIT if hit else EvaluationVerdict.MISS,
            _bar_moment(bars[-1]) if hit else None,
            final_close,
            _bar_moment(bars[-1]),
            abs(final_close - entry_price) / entry_price,
            "direction matched" if hit else "direction missed",
        )
    if claim.claim_type in {ClaimType.HOLD_ABOVE, ClaimType.HOLD_BELOW}:
        return _judge_hold(claim, bars)
    if claim.claim_type is ClaimType.RANGE:
        return _judge_range(claim, bars)
    if claim.claim_type is ClaimType.SEQUENCE:
        return _judge_sequence(claim, bars)
    if claim.claim_type is ClaimType.BREAKOUT_EITHER_SIDE:
        return _judge_either_side_breakout(claim, bars)
    if claim.claim_type is ClaimType.VOLATILITY:
        return _judge_volatility(claim, bars)
    return _judge_single_leg(claim.legs[0], bars)


def _judge_either_side_breakout(
    claim: ForecastClaim, bars: tuple[ObservedBar, ...]
) -> tuple[EvaluationVerdict, ObservationMoment | None, float | None, ObservationMoment | None, float | None, str]:
    if len(claim.legs) != 2:
        raise ValueError("either-side breakout requires two levels")
    upper = next((leg for leg in claim.legs if leg.operator == ">="), None)
    lower = next((leg for leg in claim.legs if leg.operator == "<="), None)
    if upper is None or lower is None:
        raise ValueError("either-side breakout requires upper and lower legs")
    for bar in bars:
        if _leg_matches(upper, bar):
            return EvaluationVerdict.HIT, _bar_moment(bar), bar.high, _bar_moment(bar), 0.0, "upper breakout reached first"
        if _leg_matches(lower, bar):
            return EvaluationVerdict.HIT, _bar_moment(bar), bar.low, _bar_moment(bar), 0.0, "lower breakout reached first"
    upper_price, upper_date, upper_distance = _closest_across_bars(upper, bars)
    lower_price, lower_date, lower_distance = _closest_across_bars(lower, bars)
    if upper_distance is None or (lower_distance is not None and lower_distance < upper_distance):
        closest, closest_date, distance = lower_price, lower_date, lower_distance
    else:
        closest, closest_date, distance = upper_price, upper_date, upper_distance
    verdict = EvaluationVerdict.PARTIAL_NEAR if distance is not None and distance <= _NEAR_THRESHOLD else EvaluationVerdict.MISS
    reason = "either breakout nearly reached" if verdict is EvaluationVerdict.PARTIAL_NEAR else "neither breakout level reached"
    return verdict, None, closest, closest_date, distance, reason


def _judge_single_leg(
    leg: ClaimLeg, bars: tuple[ObservedBar, ...]
) -> tuple[EvaluationVerdict, ObservationMoment | None, float | None, ObservationMoment | None, float | None, str]:
    for bar in bars:
        if _leg_matches(leg, bar):
            closest, distance = _closest_for_leg(leg, (bar,))
            return EvaluationVerdict.HIT, _bar_moment(bar), closest, _bar_moment(bar), distance, "condition satisfied"
    closest, closest_date, distance = _closest_across_bars(leg, bars)
    verdict = (
        EvaluationVerdict.PARTIAL_NEAR
        if distance is not None and distance <= _NEAR_THRESHOLD
        else EvaluationVerdict.MISS
    )
    return verdict, None, closest, closest_date, distance, (
        "target nearly reached" if verdict is EvaluationVerdict.PARTIAL_NEAR else "condition missed"
    )


def _judge_hold(
    claim: ForecastClaim, bars: tuple[ObservedBar, ...]
) -> tuple[EvaluationVerdict, ObservationMoment | None, float | None, ObservationMoment | None, float | None, str]:
    level = _require_level(claim.legs[0].level_low)
    above = claim.claim_type is ClaimType.HOLD_ABOVE
    for index in range(1, len(bars)):
        prior, current = bars[index - 1], bars[index]
        if (
            (prior.close >= level and current.close >= level)
            if above
            else (prior.close <= level and current.close <= level)
        ):
            return EvaluationVerdict.HIT, _bar_moment(current), current.close, _bar_moment(current), 0.0, "held for two closes"
    closes = tuple(bar.close for bar in bars)
    closest = max(closes) if above else min(closes)
    closest_bar = next(bar for bar in bars if bar.close == closest)
    distance = abs(closest - level) / abs(level)
    verdict = EvaluationVerdict.PARTIAL_NEAR if distance <= _NEAR_THRESHOLD or (
        (above and closest >= level) or (not above and closest <= level)
    ) else EvaluationVerdict.MISS
    return verdict, None, closest, _bar_moment(closest_bar), distance, (
        "level reached but not held" if verdict is EvaluationVerdict.PARTIAL_NEAR else "hold missed"
    )


def _judge_range(
    claim: ForecastClaim, bars: tuple[ObservedBar, ...]
) -> tuple[EvaluationVerdict, ObservationMoment | None, float | None, ObservationMoment | None, float | None, str]:
    leg = claim.legs[0]
    low = _require_level(leg.level_low)
    high = _require_level(leg.level_high)
    hit = all(bar.low >= low and bar.high <= high for bar in bars)
    excess = max(
        max(0.0, low - bar.low, bar.high - high) for bar in bars
    ) / max(abs(low), abs(high))
    return (
        EvaluationVerdict.HIT if hit else EvaluationVerdict.MISS,
        _bar_moment(bars[-1]) if hit else None,
        bars[-1].close,
        _bar_moment(bars[-1]),
        excess,
        "range held" if hit else "range broken",
    )


def _judge_sequence(
    claim: ForecastClaim, bars: tuple[ObservedBar, ...]
) -> tuple[EvaluationVerdict, ObservationMoment | None, float | None, ObservationMoment | None, float | None, str]:
    leg_index = 0
    final_search_start = len(bars)
    for bar_index, bar in enumerate(bars):
        if _leg_matches(claim.legs[leg_index], bar):
            leg_index += 1
            if leg_index == len(claim.legs):
                closest, distance = _closest_for_leg(claim.legs[-1], (bar,))
                return EvaluationVerdict.HIT, _bar_moment(bar), closest, _bar_moment(bar), distance, "sequence satisfied"
            if leg_index == len(claim.legs) - 1:
                final_search_start = bar_index + 1
    if leg_index == len(claim.legs) - 1 and final_search_start < len(bars):
        closest, closest_date, distance = _closest_across_bars(
            claim.legs[-1], bars[final_search_start:]
        )
        if distance is not None and distance <= _NEAR_THRESHOLD:
            return EvaluationVerdict.PARTIAL_NEAR, None, closest, closest_date, distance, "final sequence leg nearly reached"
    return EvaluationVerdict.MISS, None, None, None, None, "sequence missed"


def _judge_volatility(
    claim: ForecastClaim, bars: tuple[ObservedBar, ...]
) -> tuple[EvaluationVerdict, ObservationMoment | None, float | None, ObservationMoment | None, float | None, str]:
    if not claim.legs or claim.legs[0].level_low is None:
        return EvaluationVerdict.UNRESOLVED, None, None, None, None, "qualitative_volatility"
    required_range = _require_level(claim.legs[0].level_low)
    observed_range = max(bar.high for bar in bars) - min(bar.low for bar in bars)
    hit = observed_range >= required_range
    return (
        EvaluationVerdict.HIT if hit else EvaluationVerdict.MISS,
        _bar_moment(bars[-1]) if hit else None,
        observed_range,
        _bar_moment(bars[-1]),
        abs(observed_range - required_range) / abs(required_range),
        "volatility matched" if hit else "volatility missed",
    )


def _leg_matches(leg: ClaimLeg, bar: ObservedBar) -> bool:
    if leg.operator == ">=":
        return bar.high >= _require_level(leg.level_low)
    if leg.operator == "<=":
        return bar.low <= _require_level(leg.level_low)
    if leg.operator == "between":
        return bar.low >= _require_level(leg.level_low) and bar.high <= _require_level(leg.level_high)
    raise ValueError("unsupported claim operator")


def _closest_across_bars(
    leg: ClaimLeg, bars: tuple[ObservedBar, ...]
) -> tuple[float | None, ObservationMoment | None, float | None]:
    if not bars:
        return None, None, None
    candidates = [(_closest_for_leg(leg, (bar,))[1], bar) for bar in bars]
    distance, bar = min(candidates, key=lambda item: item[0])
    closest, _distance = _closest_for_leg(leg, (bar,))
    return closest, _bar_moment(bar), distance


def _closest_for_leg(leg: ClaimLeg, bars: tuple[ObservedBar, ...]) -> tuple[float, float]:
    level = _require_level(leg.level_low)
    if leg.operator == ">=":
        closest = max(bar.high for bar in bars)
        return closest, max(0.0, level - closest) / abs(level)
    if leg.operator == "<=":
        closest = min(bar.low for bar in bars)
        return closest, max(0.0, closest - level) / abs(level)
    low = _require_level(leg.level_low)
    high = _require_level(leg.level_high)
    closest = bars[-1].close
    distance = 0.0 if low <= closest <= high else min(abs(closest - low), abs(closest - high)) / max(abs(low), abs(high))
    return closest, distance


def _bar_moment(bar: ObservedBar) -> ObservationMoment:
    return bar.started_at if isinstance(bar, IntradayPriceBar) else bar.trade_date


def _require_level(value: float | None) -> float:
    if value is None or value == 0:
        raise ValueError("claim level must be a non-zero number")
    return value


def _unresolved(
    claim: ForecastClaim,
    evaluated_at: datetime,
    window: ClaimWindow,
    verdict: EvaluationVerdict,
    reason: str,
) -> ClaimEvaluation:
    return ClaimEvaluation(
        claim_id=claim.claim_id,
        evaluated_at=evaluated_at,
        window_start=window.start_date,
        window_end=window.end_date,
        entry_price=None,
        observed_min=None,
        observed_max=None,
        final_close=None,
        closest_price=None,
        closest_date=None,
        distance_pct=None,
        first_hit_date=None,
        verdict=verdict,
        mature=False,
        reason=reason,
    )


def _unresolved_intraday(
    claim: ForecastClaim,
    evaluated_at: datetime,
    window: IntradayClaimWindow,
    verdict: EvaluationVerdict,
    reason: str,
) -> ClaimEvaluation:
    return ClaimEvaluation(
        claim_id=claim.claim_id,
        evaluated_at=evaluated_at,
        window_start=None,
        window_end=None,
        entry_price=None,
        observed_min=None,
        observed_max=None,
        final_close=None,
        closest_price=None,
        closest_date=None,
        distance_pct=None,
        first_hit_date=None,
        verdict=verdict,
        mature=False,
        reason=reason,
        window_start_at=window.start_at,
        window_end_at=window.end_at,
        closest_at=None,
        first_hit_at=None,
    )
