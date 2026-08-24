from datetime import date, datetime, timezone

from goldbook.db import Database
from goldbook.models import Creator, Direction, PriceBar, ReviewStatus, SignalAnalysis, Video
from goldbook.recompute import recompute_cached_outcomes


def test_cached_price_recomputation_backfills_maturity_and_removes_ineligible_outcome(tmp_path):
    database = Database(tmp_path / "goldbook.db")
    database.initialize()
    database.upsert_creator(Creator("42", "测试UP", "https://space.bilibili.com/42"))
    video = Video(
        "BV1CACHE", "42", "黄金观点", datetime(2026, 8, 1, tzinfo=timezone.utc), 60,
        "https://www.bilibili.com/video/BV1CACHE",
    )
    database.upsert_video(video)
    database.save_analysis(
        SignalAnalysis(
            direction=Direction.BULLISH,
            strength=4,
            confidence=0.9,
            review_status=ReviewStatus.APPROVED,
            bvid=video.bvid,
            transcript_hash="transcript",
        )
    )

    database.replace_prices([_bar(1)])
    initial = recompute_cached_outcomes(database)

    assert initial.upserted == 0
    assert initial.deleted == 0
    assert database.get_outcome(video.bvid) is None

    database.replace_prices([_bar(day) for day in range(2, 7)])
    five_day = recompute_cached_outcomes(database)
    outcome_at_five_days = database.get_outcome(video.bvid)

    assert five_day.upserted == 1
    assert outcome_at_five_days is not None
    assert outcome_at_five_days.return_5d == 0.04
    assert outcome_at_five_days.return_20d is None

    database.replace_prices([_bar(day) for day in range(2, 22)])
    twenty_day = recompute_cached_outcomes(database)
    outcome_at_twenty_days = database.get_outcome(video.bvid)

    assert twenty_day.upserted == 1
    assert outcome_at_twenty_days is not None
    assert outcome_at_twenty_days.return_20d == 0.19
    assert recompute_cached_outcomes(database).upserted == 1
    assert database.get_outcome(video.bvid) == outcome_at_twenty_days

    database.save_analysis(
        SignalAnalysis(
            direction=Direction.BULLISH,
            strength=4,
            confidence=0.9,
            review_status=ReviewStatus.EXCLUDED,
            bvid=video.bvid,
            transcript_hash="transcript",
            revision=1,
        )
    )
    excluded = recompute_cached_outcomes(database)

    assert excluded.upserted == 0
    assert excluded.deleted == 1
    assert database.get_outcome(video.bvid) is None
    assert recompute_cached_outcomes(database).deleted == 0


def _bar(day: int) -> PriceBar:
    price = float(100 + day - 2)
    return PriceBar(date(2026, 8, day), price, price, price, price)
