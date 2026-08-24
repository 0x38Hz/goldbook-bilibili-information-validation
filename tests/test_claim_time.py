from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from goldbook.claim_time import (
    ClaimWindow,
    IntradayClaimWindow,
    find_next_same_instrument_prediction,
    is_event_activated_claim,
    is_intraday_claim,
    resolve_claim_window,
    resolve_intraday_claim_window,
    resolve_unknown_horizon_intraday_window,
)
from goldbook.models import (
    ClaimLeg,
    ClaimStatus,
    ClaimType,
    Direction,
    ForecastClaim,
    HorizonSource,
    Instrument,
    IntradayPriceBar,
    PriceBar,
    Video,
)


SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=SHANGHAI)


def _video(published_at: datetime = _at("2026-08-03T12:00:00")) -> Video:
    return Video(
        "BV1TIME",
        "42",
        "黄金周期",
        published_at,
        60,
        "https://www.bilibili.com/video/BV1TIME",
    )


def _claim(
    *,
    source: HorizonSource = HorizonSource.CONTEXT_INFERRED,
    minimum: int | None = 1,
    point: int | None = 2,
    maximum: int | None = 3,
    horizon_text: str | None = "短期",
) -> ForecastClaim:
    return ForecastClaim(
        claim_id="BV1TIME:1:0",
        bvid="BV1TIME",
        analysis_revision=1,
        claim_index=0,
        instrument=Instrument.XAU_USD_SPOT,
        claim_type=ClaimType.TARGET_TOUCH,
        direction=Direction.BULLISH,
        legs=(ClaimLeg(">=", 4700.0, None),),
        condition_text="看4700",
        horizon_text=horizon_text,
        horizon_source=source,
        horizon_min_trading_days=minimum,
        horizon_max_trading_days=maximum,
        horizon_point_trading_days=point,
        deadline_at=None,
        time_confidence=0.8,
        confidence=0.9,
        evidence=({"start_sec": 0.0, "end_sec": 2.0, "quote": "看4700"},),
        supersedes_claim_id=None,
        status=ClaimStatus.AUTO_VALIDATED,
        model_name="MiniMax-M3",
        prompt_version="claims-v1",
        transcript_hash="hash",
    )


def _bars(*days: str) -> list[PriceBar]:
    return [PriceBar(day, 4600.0, 4710.0, 4590.0, 4680.0) for day in days]


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _hourly(*hours: str) -> list[IntradayPriceBar]:
    return [
        IntradayPriceBar(_utc(hour), 60, 4400, 4410, 4390, 4405, "XAUS")
        for hour in hours
    ]


def test_publish_day_and_evaluation_day_bars_are_never_observed():
    result = resolve_claim_window(
        _claim(),
        _video(),
        _bars("2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"),
        evaluated_at=_at("2026-08-07T12:00:00"),
    )

    assert result == ClaimWindow(
        start_date=date(2026, 8, 4),
        end_date=date(2026, 8, 6),
        mature=True,
        reason=None,
    )


def test_intraday_claim_is_unresolved_when_only_daily_bars_exist():
    claim = _claim(
        source=HorizonSource.UNKNOWN,
        minimum=None,
        point=None,
        maximum=None,
        horizon_text="今晚",
    )

    result = resolve_claim_window(
        claim,
        _video(),
        _bars("2026-08-04"),
        evaluated_at=_at("2026-08-05T12:00:00"),
    )

    assert result.reason == "unresolved_intraday_data"
    assert result.mature is False


def test_zero_trading_day_horizon_is_unresolved_with_daily_bars():
    claim = _claim(
        source=HorizonSource.EXPLICIT_RELATIVE,
        minimum=0,
        point=0,
        maximum=0,
        horizon_text="今天盘中",
    )

    result = resolve_claim_window(
        claim,
        _video(),
        _bars("2026-08-04"),
        evaluated_at=_at("2026-08-05T12:00:00"),
    )

    assert result.reason == "unresolved_intraday_data"
    assert result.mature is False


def test_intraday_window_excludes_hour_crossing_publication_time():
    video = _video(_utc("2026-08-12T10:35:00"))
    claim = _claim(minimum=0, point=0, maximum=0, horizon_text="今天日内")
    bars = _hourly("2026-08-12T10:00:00", "2026-08-12T11:00:00")

    window = resolve_intraday_claim_window(
        claim, video, bars, evaluated_at=_utc("2026-08-12T16:00:00")
    )

    assert [bar.started_at for bar in window.bars] == [
        _utc("2026-08-12T11:00:00")
    ]
    assert window.start_at == _utc("2026-08-12T11:00:00")
    assert window.end_at == _utc("2026-08-12T16:00:00")


def test_incomplete_hour_after_publication_is_not_observed():
    video = _video(_utc("2026-08-12T10:35:00"))
    claim = _claim(minimum=0, point=0, maximum=0, horizon_text="未来2小时")

    window = resolve_intraday_claim_window(
        claim,
        video,
        _hourly("2026-08-12T11:00:00"),
        evaluated_at=_utc("2026-08-12T11:30:00"),
    )

    assert window.bars == ()
    assert window.reason == "unresolved_intraday_data"
    assert window.mature is False


def test_intraday_deadline_priority_and_shanghai_semantics():
    video = _video(_utc("2026-08-12T10:35:00"))
    cases = (
        (
            replace(
                _claim(minimum=0, point=0, maximum=0, horizon_text="今天"),
                deadline_at=_utc("2026-08-12T13:20:00"),
            ),
            _utc("2026-08-12T13:20:00"),
        ),
        (
            _claim(minimum=0, point=0, maximum=0, horizon_text="未来2小时"),
            _utc("2026-08-12T12:35:00"),
        ),
        (
            _claim(minimum=0, point=0, maximum=0, horizon_text="今天日内"),
            _utc("2026-08-12T16:00:00"),
        ),
        (
            _claim(minimum=None, point=None, maximum=None, horizon_text="今晚"),
            _utc("2026-08-12T22:00:00"),
        ),
        (
            _claim(minimum=0, point=0, maximum=0, horizon_text=None),
            _utc("2026-08-12T16:00:00"),
        ),
    )

    for claim, expected_end in cases:
        window = resolve_intraday_claim_window(
            claim,
            video,
            _hourly("2026-08-12T11:00:00"),
            evaluated_at=_utc("2026-08-13T00:00:00"),
        )
        assert window.end_at == expected_end


def test_intraday_window_uses_earliest_prediction_or_supersession_cutoff():
    video = _video(_utc("2026-08-12T10:35:00"))
    claim = _claim(minimum=0, point=0, maximum=0, horizon_text="今天")
    bars = _hourly(
        "2026-08-12T11:00:00",
        "2026-08-12T12:00:00",
        "2026-08-12T13:00:00",
    )

    next_window = resolve_intraday_claim_window(
        claim,
        video,
        bars,
        next_same_instrument_prediction_at=_utc("2026-08-12T12:30:00"),
        evaluated_at=_utc("2026-08-12T14:00:00"),
    )
    superseded_window = resolve_intraday_claim_window(
        claim,
        video,
        bars,
        superseded_at=_utc("2026-08-12T13:30:00"),
        evaluated_at=_utc("2026-08-12T14:00:00"),
    )

    assert [bar.started_at for bar in next_window.bars] == [
        _utc("2026-08-12T11:00:00")
    ]
    assert next_window.end_at == _utc("2026-08-12T12:30:00")
    assert next_window.mature is True
    assert [bar.started_at for bar in superseded_window.bars] == [
        _utc("2026-08-12T11:00:00"),
        _utc("2026-08-12T12:00:00"),
    ]
    assert superseded_window.reason == "superseded"
    assert superseded_window.mature is True


def test_intraday_classifier_is_public_and_does_not_capture_daily_claims():
    assert is_intraday_claim(
        _claim(minimum=None, point=None, maximum=None, horizon_text="今晚")
    )
    assert is_intraday_claim(
        _claim(minimum=0, point=0, maximum=0, horizon_text=None)
    )
    assert is_intraday_claim(
        _claim(minimum=0, point=0, maximum=1, horizon_text="今天晚上")
    )
    assert not is_intraday_claim(_claim(horizon_text="下周"))


def test_event_activation_requires_explicit_release_wording_not_any_later_step():
    base = replace(
        _claim(minimum=1, point=1, maximum=1, horizon_text="明天"),
        deadline_at=_utc("2026-08-12T12:30:00"),
    )
    event_claim = replace(
        base,
        condition_text="CPI数据公布后黄金突破450",
    )
    price_sequence = replace(
        base,
        condition_text="突破4450后逼空行情延续",
    )

    assert is_event_activated_claim(event_claim)
    assert not is_event_activated_claim(price_sequence)


def test_unknown_horizon_ends_before_next_same_instrument_video():
    claim = _claim(
        source=HorizonSource.UNKNOWN,
        minimum=None,
        point=None,
        maximum=None,
        horizon_text=None,
    )

    result = resolve_claim_window(
        claim,
        _video(),
        _bars("2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"),
        next_same_instrument_prediction_at=_at("2026-08-06T10:00:00"),
        evaluated_at=_at("2026-08-08T12:00:00"),
    )

    assert result == ClaimWindow(
        start_date=date(2026, 8, 4),
        end_date=date(2026, 8, 5),
        mature=True,
        reason=None,
    )


def test_unknown_horizon_uses_complete_hours_until_next_prediction():
    """Catches daily uploads leaving an otherwise verifiable unknown-horizon claim blank."""
    claim = _claim(
        source=HorizonSource.UNKNOWN,
        minimum=None,
        point=None,
        maximum=None,
        horizon_text=None,
    )
    video = _video(_utc("2026-08-03T10:35:00"))

    result = resolve_unknown_horizon_intraday_window(
        claim,
        video,
        _hourly(
            "2026-08-03T10:00:00",
            "2026-08-03T11:00:00",
            "2026-08-03T12:00:00",
        ),
        next_same_instrument_prediction_at=_utc("2026-08-04T10:35:00"),
        evaluated_at=_utc("2026-08-05T00:00:00"),
    )

    assert result.start_at == _utc("2026-08-03T11:00:00")
    assert result.end_at == _utc("2026-08-04T10:35:00")
    assert [bar.started_at for bar in result.bars] == [
        _utc("2026-08-03T11:00:00"),
        _utc("2026-08-03T12:00:00"),
    ]
    assert result.mature is True
    assert result.reason is None


def test_explicit_long_horizon_is_not_cut_off_by_another_daily_video():
    days = [f"2026-08-{day:02d}" for day in range(4, 25)]
    claim = _claim(minimum=10, point=15, maximum=20, horizon_text="长期")

    result = resolve_claim_window(
        claim,
        _video(),
        _bars(*days),
        next_same_instrument_prediction_at=_at("2026-08-05T10:00:00"),
        evaluated_at=_at("2026-08-25T12:00:00"),
    )

    assert result.start_date == date(2026, 8, 4)
    assert result.end_date == date(2026, 8, 23)
    assert result.mature is True


def test_saturday_publication_starts_on_monday_and_skips_missing_weekdays():
    saturday = _video(_at("2026-08-22T12:00:00"))
    one_day = _claim(minimum=1, point=1, maximum=1, horizon_text="下个交易日")

    result = resolve_claim_window(
        one_day,
        saturday,
        _bars("2026-08-24", "2026-08-26"),
        evaluated_at=_at("2026-08-27T12:00:00"),
    )

    assert result.start_date == date(2026, 8, 24)
    assert result.end_date == date(2026, 8, 24)


def test_explicit_supersession_uses_only_complete_bars_before_new_video():
    result = resolve_claim_window(
        _claim(minimum=5, point=10, maximum=15),
        _video(),
        _bars("2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"),
        superseded_at=_at("2026-08-06T10:00:00"),
        evaluated_at=_at("2026-08-08T12:00:00"),
    )

    assert result == ClaimWindow(
        start_date=date(2026, 8, 4),
        end_date=date(2026, 8, 5),
        mature=True,
        reason="superseded",
    )


def test_next_prediction_ignores_other_instruments_and_excluded_claims():
    current_video = _video()
    other_video = replace(
        _video(_at("2026-08-04T10:00:00")), bvid="BVOTHER"
    )
    excluded_video = replace(
        _video(_at("2026-08-05T10:00:00")), bvid="BVEXCLUDED"
    )
    matching_video = replace(
        _video(_at("2026-08-06T10:00:00")), bvid="BVMATCH"
    )
    current = _claim(
        source=HorizonSource.UNKNOWN,
        minimum=None,
        point=None,
        maximum=None,
        horizon_text=None,
    )
    other = replace(
        _claim(),
        claim_id="BVOTHER:1:0",
        bvid="BVOTHER",
        instrument=Instrument.COMEX_GC,
    )
    excluded = replace(
        _claim(),
        claim_id="BVEXCLUDED:1:0",
        bvid="BVEXCLUDED",
        status=ClaimStatus.EXCLUDED,
    )
    matching = replace(
        _claim(), claim_id="BVMATCH:1:0", bvid="BVMATCH"
    )

    result = find_next_same_instrument_prediction(
        current,
        current_video,
        [current_video, other_video, excluded_video, matching_video],
        [current, other, excluded, matching],
    )

    assert result == matching_video.published_at


def test_unknown_primary_trend_ignores_point_claim_before_next_primary_trend():
    current_video = _video()
    point_video = replace(_video(_at("2026-08-04T10:00:00")), bvid="BVPOINT")
    next_video = replace(_video(_at("2026-08-06T10:00:00")), bvid="BVNEXT")
    current = replace(
        _claim(source=HorizonSource.UNKNOWN, minimum=None, point=None, maximum=None),
        claim_type=ClaimType.DIRECTIONAL_MOVE,
        legs=(),
    )
    unrelated_point = replace(
        _claim(),
        claim_id="BVPOINT:1:1",
        bvid="BVPOINT",
        claim_index=1,
        claim_type=ClaimType.TARGET_TOUCH,
    )
    next_trend = replace(
        current,
        claim_id="BVNEXT:1:0",
        bvid="BVNEXT",
    )

    cutoff = find_next_same_instrument_prediction(
        current,
        current_video,
        [current_video, point_video, next_video],
        [current, unrelated_point, next_trend],
    )

    assert cutoff == next_video.published_at
