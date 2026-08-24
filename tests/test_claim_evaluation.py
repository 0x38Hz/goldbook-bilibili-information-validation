from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from goldbook.claim_evaluation import (
    evaluate_claim,
    evaluate_intraday_claim,
    recompute_claim_evaluations,
)
from goldbook.claim_time import ClaimWindow, IntradayClaimWindow
from goldbook.db import Database
from goldbook.models import (
    ClaimLeg,
    ClaimStatus,
    ClaimType,
    Creator,
    Direction,
    EvaluationVerdict,
    ForecastClaim,
    HorizonSource,
    Instrument,
    IntradayPriceBar,
    PriceBar,
    Video,
)


NOW = datetime(2026, 8, 10, tzinfo=timezone.utc)
VIDEO = Video(
    "BV1EVAL",
    "42",
    "黄金目标",
    datetime(2026, 8, 3, tzinfo=timezone.utc),
    60,
    "https://www.bilibili.com/video/BV1EVAL",
)
WINDOW = ClaimWindow(date(2026, 8, 4), date(2026, 8, 5), True, None)


def _claim(
    claim_type: ClaimType = ClaimType.TARGET_TOUCH,
    *,
    direction: Direction | None = Direction.BULLISH,
    legs: tuple[ClaimLeg, ...] = (ClaimLeg(">=", 4700.0, None),),
    instrument: Instrument = Instrument.XAU_USD_SPOT,
) -> ForecastClaim:
    return ForecastClaim(
        claim_id="BV1EVAL:1:0",
        bvid="BV1EVAL",
        analysis_revision=1,
        claim_index=0,
        instrument=instrument,
        claim_type=claim_type,
        direction=direction,
        legs=legs,
        condition_text="目标4700",
        horizon_text="短期",
        horizon_source=HorizonSource.CONTEXT_INFERRED,
        horizon_min_trading_days=1,
        horizon_max_trading_days=2,
        horizon_point_trading_days=2,
        deadline_at=None,
        time_confidence=0.8,
        confidence=0.9,
        evidence=({"start_sec": 0.0, "end_sec": 2.0, "quote": "目标4700"},),
        supersedes_claim_id=None,
        status=ClaimStatus.AUTO_VALIDATED,
        model_name="MiniMax-M3",
        prompt_version="claims-v1",
        transcript_hash="hash",
    )


def _bars(*rows: tuple[str, float, float, float, float]) -> list[PriceBar]:
    return [PriceBar(*row) for row in rows]


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _hour(
    value: str, open_price: float, high: float, low: float, close: float
) -> IntradayPriceBar:
    return IntradayPriceBar(
        _utc(value), 60, open_price, high, low, close, "XAUS"
    )


def test_target_touch_uses_high_and_records_first_hit():
    result = evaluate_claim(
        _claim(),
        VIDEO,
        _bars(
            ("2026-08-04", 4660, 4690, 4650, 4680),
            ("2026-08-05", 4680, 4702, 4670, 4698),
        ),
        WINDOW,
        evaluated_at=NOW,
    )

    assert result.verdict is EvaluationVerdict.HIT
    assert result.first_hit_date == date(2026, 8, 5)
    assert result.observed_min == 4650
    assert result.observed_max == 4702
    assert result.entry_price == 4660


def test_exact_break_is_partial_near_not_hit_when_price_stays_within_half_percent():
    result = evaluate_claim(
        _claim(ClaimType.CROSS_ABOVE),
        VIDEO,
        _bars(
            ("2026-08-04", 4660, 4680, 4650, 4670),
            ("2026-08-05", 4670, 4685, 4660, 4680),
        ),
        WINDOW,
        evaluated_at=NOW,
    )

    assert result.verdict is EvaluationVerdict.PARTIAL_NEAR
    assert result.closest_price == 4685
    assert result.distance_pct == pytest.approx((4700 - 4685) / 4700)
    assert result.first_hit_date is None


def test_hold_above_requires_two_consecutive_closes():
    one_close = evaluate_claim(
        _claim(ClaimType.HOLD_ABOVE),
        VIDEO,
        _bars(
            ("2026-08-04", 4680, 4720, 4670, 4710),
            ("2026-08-05", 4710, 4720, 4680, 4690),
        ),
        WINDOW,
        evaluated_at=NOW,
    )
    two_closes = evaluate_claim(
        _claim(ClaimType.HOLD_ABOVE),
        VIDEO,
        _bars(
            ("2026-08-04", 4680, 4720, 4670, 4710),
            ("2026-08-05", 4710, 4730, 4700, 4720),
        ),
        WINDOW,
        evaluated_at=NOW,
    )

    assert one_close.verdict is EvaluationVerdict.PARTIAL_NEAR
    assert two_closes.verdict is EvaluationVerdict.HIT
    assert two_closes.first_hit_date == date(2026, 8, 5)


def test_sequence_fails_when_target_arrives_before_pullback():
    result = evaluate_claim(
        _claim(
            ClaimType.SEQUENCE,
            legs=(ClaimLeg("<=", 4650.0, None), ClaimLeg(">=", 4700.0, None)),
        ),
        VIDEO,
        _bars(
            ("2026-08-04", 4680, 4710, 4670, 4700),
            ("2026-08-05", 4700, 4705, 4640, 4660),
        ),
        WINDOW,
        evaluated_at=NOW,
    )

    assert result.verdict is EvaluationVerdict.MISS
    assert result.first_hit_date is None


def test_unsupported_instrument_and_immature_window_are_not_counted_as_misses():
    unsupported = evaluate_claim(
        _claim(instrument=Instrument.COMEX_GC),
        VIDEO,
        _bars(("2026-08-04", 4660, 4702, 4650, 4698)),
        WINDOW,
        evaluated_at=NOW,
    )
    immature = evaluate_claim(
        _claim(),
        VIDEO,
        _bars(("2026-08-04", 4660, 4702, 4650, 4698)),
        ClaimWindow(date(2026, 8, 4), date(2026, 8, 4), False, "horizon_not_mature"),
        evaluated_at=NOW,
    )

    assert unsupported.verdict is EvaluationVerdict.UNRESOLVED
    assert unsupported.reason == "unresolved_instrument"
    assert immature.verdict is EvaluationVerdict.UNRESOLVED
    assert immature.reason == "horizon_not_mature"


def test_directional_claim_compares_deadline_close_with_first_complete_bar_open():
    bullish = evaluate_claim(
        _claim(ClaimType.DIRECTIONAL_MOVE, legs=()),
        VIDEO,
        _bars(
            ("2026-08-04", 4660, 4690, 4650, 4670),
            ("2026-08-05", 4670, 4720, 4660, 4710),
        ),
        WINDOW,
        evaluated_at=NOW,
    )
    bearish = evaluate_claim(
        _claim(ClaimType.DIRECTIONAL_MOVE, direction=Direction.BEARISH, legs=()),
        VIDEO,
        _bars(
            ("2026-08-04", 4660, 4690, 4650, 4670),
            ("2026-08-05", 4670, 4720, 4660, 4710),
        ),
        WINDOW,
        evaluated_at=NOW,
    )

    assert bullish.verdict is EvaluationVerdict.HIT
    assert bearish.verdict is EvaluationVerdict.MISS


def test_intraday_direction_compares_first_eligible_open_to_final_close():
    video = replace(VIDEO, published_at=_utc("2026-08-12T10:35:00"))
    claim = replace(
        _claim(ClaimType.DIRECTIONAL_MOVE, legs=()),
        horizon_text="未来2小时",
        horizon_min_trading_days=0,
        horizon_max_trading_days=0,
        horizon_point_trading_days=0,
    )
    window = IntradayClaimWindow(
        _utc("2026-08-12T11:00:00"),
        _utc("2026-08-12T13:00:00"),
        True,
        None,
        (
            _hour("2026-08-12T11:00:00", 4400, 4410, 4395, 4405),
            _hour("2026-08-12T12:00:00", 4405, 4430, 4400, 4420),
        ),
    )

    result = evaluate_intraday_claim(
        claim, video, window, evaluated_at=_utc("2026-08-12T13:00:00")
    )

    assert result.entry_price == 4400
    assert result.final_close == 4420
    assert result.verdict is EvaluationVerdict.HIT
    assert result.window_start_at == _utc("2026-08-12T11:00:00")
    assert result.first_hit_at == _utc("2026-08-12T12:00:00")
    assert result.window_start is None


def test_intraday_target_can_hit_before_deadline_but_cannot_miss_early():
    video = replace(VIDEO, published_at=_utc("2026-08-12T10:35:00"))
    target_4450 = replace(
        _claim(),
        legs=(ClaimLeg(">=", 4450.0, None),),
        horizon_text="今天",
        horizon_min_trading_days=0,
        horizon_max_trading_days=0,
        horizon_point_trading_days=0,
    )
    target_4500 = replace(target_4450, legs=(ClaimLeg(">=", 4500.0, None),))
    hit_window = IntradayClaimWindow(
        _utc("2026-08-12T11:00:00"),
        _utc("2026-08-12T16:00:00"),
        False,
        "intraday_horizon_not_mature",
        (_hour("2026-08-12T11:00:00", 4400, 4451, 4390, 4440),),
    )

    hit = evaluate_intraday_claim(
        target_4450, video, hit_window, evaluated_at=_utc("2026-08-12T12:00:00")
    )
    miss_so_far = evaluate_intraday_claim(
        target_4500, video, hit_window, evaluated_at=_utc("2026-08-12T12:00:00")
    )

    assert (hit.verdict, hit.mature) == (EvaluationVerdict.HIT, True)
    assert (miss_so_far.verdict, miss_so_far.mature, miss_so_far.reason) == (
        EvaluationVerdict.UNRESOLVED,
        False,
        "intraday_horizon_not_mature",
    )


def test_intraday_hold_requires_two_complete_hourly_closes_and_deadline():
    claim = replace(
        _claim(ClaimType.HOLD_ABOVE),
        legs=(ClaimLeg(">=", 4400.0, None),),
        horizon_text="未来2小时",
        horizon_min_trading_days=0,
        horizon_max_trading_days=0,
        horizon_point_trading_days=0,
    )
    bars = (
        _hour("2026-08-12T11:00:00", 4390, 4410, 4385, 4405),
        _hour("2026-08-12T12:00:00", 4405, 4420, 4400, 4418),
    )
    immature = evaluate_intraday_claim(
        claim,
        VIDEO,
        IntradayClaimWindow(bars[0].started_at, _utc("2026-08-12T13:00:00"), False, "intraday_horizon_not_mature", bars),
        evaluated_at=_utc("2026-08-12T12:30:00"),
    )
    mature = evaluate_intraday_claim(
        claim,
        VIDEO,
        IntradayClaimWindow(bars[0].started_at, _utc("2026-08-12T13:00:00"), True, None, bars),
        evaluated_at=_utc("2026-08-12T13:00:00"),
    )

    assert immature.verdict is EvaluationVerdict.UNRESOLVED
    assert mature.verdict is EvaluationVerdict.HIT
    assert mature.first_hit_at == _utc("2026-08-12T12:00:00")


@pytest.mark.parametrize(
    ("direction", "reason"),
    [
        (Direction.NEUTRAL, "neutral_trend"),
        (Direction.NO_SIGNAL, "no_signal"),
    ],
)
def test_non_directional_primary_trends_are_unresolved_not_misses(direction, reason):
    result = evaluate_claim(
        _claim(ClaimType.DIRECTIONAL_MOVE, direction=direction, legs=()),
        VIDEO,
        _bars(
            ("2026-08-04", 4660, 4690, 4650, 4670),
            ("2026-08-05", 4670, 4720, 4660, 4710),
        ),
        WINDOW,
        evaluated_at=NOW,
    )

    assert result.verdict is EvaluationVerdict.UNRESOLVED
    assert result.reason == reason
    assert result.mature is False


def test_either_side_breakout_hits_when_the_upper_level_is_reached_first():
    claim = _claim(
        ClaimType.BREAKOUT_EITHER_SIDE,
        direction=Direction.NEUTRAL,
        legs=(ClaimLeg(">=", 4200.0, None), ClaimLeg("<=", 3940.0, None)),
    )

    result = evaluate_claim(
        claim,
        VIDEO,
        _bars(
            ("2026-08-04", 4100, 4188, 4026.5, 4160),
            ("2026-08-05", 4160, 4371.5, 4120, 4340.7),
        ),
        WINDOW,
        evaluated_at=NOW,
    )

    assert result.verdict is EvaluationVerdict.HIT
    assert result.first_hit_date == date(2026, 8, 5)
    assert result.closest_price == 4371.5
    assert result.reason == "upper breakout reached first"


def test_cached_recomputation_is_idempotent_and_matures_after_price_refresh(tmp_path):
    database = Database(tmp_path / "goldbook.db")
    database.initialize()
    database.upsert_creator(Creator("42", "测试UP", "https://space.bilibili.com/42"))
    database.upsert_video(VIDEO)
    database.replace_forecast_claims("BV1EVAL", 1, [_claim()])
    database.replace_prices(
        [
            PriceBar("2026-08-04", 4660, 4690, 4650, 4680),
        ]
    )

    first = recompute_claim_evaluations(
        database, evaluated_at=datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
    )

    assert first.evaluated == 0
    assert first.unresolved == 1
    assert database.get_claim_evaluation("BV1EVAL:1:0").verdict is EvaluationVerdict.UNRESOLVED

    database.replace_prices(
        [
            PriceBar("2026-08-04", 4660, 4690, 4650, 4680),
            PriceBar("2026-08-05", 4680, 4702, 4670, 4698),
        ]
    )
    mature = recompute_claim_evaluations(
        database, evaluated_at=datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    )
    repeated = recompute_claim_evaluations(
        database, evaluated_at=datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    )

    assert mature.evaluated == 1
    assert mature.unresolved == 0
    assert repeated == mature
    assert database.get_claim_evaluation("BV1EVAL:1:0").verdict is EvaluationVerdict.HIT


def test_cached_recomputation_routes_intraday_claims_to_hourly_prices(tmp_path):
    database = Database(tmp_path / "goldbook.db")
    database.initialize()
    database.upsert_creator(Creator("42", "测试UP", "https://space.bilibili.com/42"))
    video = replace(VIDEO, published_at=_utc("2026-08-12T10:35:00"))
    database.upsert_video(video)
    claim = replace(
        _claim(),
        horizon_text="未来3小时",
        horizon_min_trading_days=0,
        horizon_max_trading_days=0,
        horizon_point_trading_days=0,
        legs=(ClaimLeg(">=", 4450.0, None),),
    )
    database.replace_forecast_claims(video.bvid, 1, (claim,))
    database.upsert_intraday_prices(
        (
            _hour("2026-08-12T10:00:00", 4440, 4500, 4430, 4490),
            _hour("2026-08-12T11:00:00", 4400, 4430, 4390, 4420),
            _hour("2026-08-12T12:00:00", 4420, 4451, 4410, 4448),
        )
    )

    summary = recompute_claim_evaluations(
        database, evaluated_at=_utc("2026-08-12T14:00:00")
    )
    result = database.get_claim_evaluation(claim.claim_id)

    assert summary.evaluated == 1
    assert result is not None
    assert result.verdict is EvaluationVerdict.HIT
    assert result.window_start_at == _utc("2026-08-12T11:00:00")
    assert result.first_hit_at == _utc("2026-08-12T12:00:00")


def test_cached_recomputation_uses_hourly_bars_for_unknown_horizon_until_next_video(tmp_path):
    """Catches unknown-horizon claims being stranded when a creator uploads daily."""
    database = Database(tmp_path / "goldbook.db")
    database.initialize()
    database.upsert_creator(Creator("42", "测试UP", "https://space.bilibili.com/42"))
    video = replace(VIDEO, published_at=_utc("2026-08-03T10:35:00"))
    next_video = replace(
        VIDEO,
        bvid="BV1NEXT",
        published_at=_utc("2026-08-04T10:35:00"),
    )
    database.upsert_video(video)
    database.upsert_video(next_video)
    claim = replace(
        _claim(),
        horizon_text=None,
        horizon_source=HorizonSource.UNKNOWN,
        horizon_min_trading_days=None,
        horizon_point_trading_days=None,
        horizon_max_trading_days=None,
        legs=(ClaimLeg(">=", 4450.0, None),),
    )
    next_claim = replace(claim, claim_id="BV1NEXT:1:0", bvid=next_video.bvid)
    database.replace_forecast_claims(video.bvid, 1, (claim,))
    database.replace_forecast_claims(next_video.bvid, 1, (next_claim,))
    database.upsert_intraday_prices(
        (
            _hour("2026-08-03T10:00:00", 4440, 4500, 4430, 4490),
            _hour("2026-08-03T11:00:00", 4400, 4430, 4390, 4420),
            _hour("2026-08-03T12:00:00", 4420, 4451, 4410, 4448),
        )
    )

    recompute_claim_evaluations(database, evaluated_at=_utc("2026-08-05T00:00:00"))
    result = database.get_claim_evaluation(claim.claim_id)

    assert result is not None
    assert result.verdict is EvaluationVerdict.HIT
    assert result.window_start_at == _utc("2026-08-03T11:00:00")
    assert result.first_hit_at == _utc("2026-08-03T12:00:00")


def test_event_release_time_activates_post_event_hourly_evaluation(tmp_path):
    """Catches an event timestamp being mistaken for a cutoff before its forecast starts."""
    database = Database(tmp_path / "goldbook.db")
    database.initialize()
    database.upsert_creator(Creator("42", "测试UP", "https://space.bilibili.com/42"))
    video = replace(VIDEO, published_at=_utc("2026-08-11T11:24:04"))
    database.upsert_video(video)
    claim = replace(
        _claim(ClaimType.CROSS_ABOVE),
        condition_text="明天CPI数据公布后黄金上攻并突破4450美元关键压力位",
        horizon_text="明天的CPI数据",
        horizon_source=HorizonSource.EXPLICIT_RELATIVE,
        horizon_min_trading_days=1,
        horizon_point_trading_days=1,
        horizon_max_trading_days=1,
        deadline_at=_utc("2026-08-12T12:30:00"),
        legs=(ClaimLeg(">=", 4450.0, None),),
    )
    database.replace_forecast_claims(video.bvid, 1, (claim,))
    database.replace_prices(
        (PriceBar("2026-08-12", 4406.5, 4434.0, 4406.3, 4408.9),)
    )
    database.upsert_intraday_prices(
        (
            _hour("2026-08-12T12:00:00", 4472.2, 4499.3, 4440.0, 4498.3),
            _hour("2026-08-12T13:00:00", 4497.8, 4502.7, 4468.0, 4487.4),
            _hour("2026-08-12T14:00:00", 4487.6, 4499.8, 4475.8, 4481.3),
        )
    )

    summary = recompute_claim_evaluations(
        database, evaluated_at=_utc("2026-08-13T00:00:00")
    )
    result = database.get_claim_evaluation(claim.claim_id)

    assert summary.evaluated == 1
    assert result is not None
    assert result.verdict is EvaluationVerdict.HIT
    assert result.window_start_at == _utc("2026-08-12T13:00:00")
    assert result.first_hit_at == _utc("2026-08-12T13:00:00")
    assert result.observed_max == 4502.7
    assert result.reason == "condition satisfied"


def test_invalid_claim_structure_still_gets_an_unresolved_evaluation_row(tmp_path):
    database = Database(tmp_path / "goldbook.db")
    database.initialize()
    database.upsert_creator(Creator("42", "测试UP", "https://space.bilibili.com/42"))
    database.upsert_video(VIDEO)
    invalid_range = _claim(
        ClaimType.RANGE,
        legs=(ClaimLeg("<=", 4200.0, None), ClaimLeg(">=", 3940.0, None)),
    )
    database.replace_forecast_claims("BV1EVAL", 1, [invalid_range])
    database.replace_prices(
        [
            PriceBar("2026-08-04", 4660, 4690, 4650, 4680),
            PriceBar("2026-08-05", 4680, 4702, 4670, 4698),
        ]
    )

    summary = recompute_claim_evaluations(
        database, evaluated_at=datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    )

    evaluation = database.get_claim_evaluation("BV1EVAL:1:0")
    assert summary.failed == 1
    assert summary.unresolved == 1
    assert evaluation.verdict is EvaluationVerdict.UNRESOLVED
    assert evaluation.reason == "invalid_claim_structure"
