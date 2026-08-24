import sqlite3
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from goldbook.db import Database
from goldbook.models import (
    ClaimEvaluation,
    ClaimLeg,
    ClaimStatus,
    ClaimType,
    CreatorMetricSample,
    Creator,
    Direction,
    EvaluationVerdict,
    ForecastClaim,
    HorizonSource,
    Instrument,
    IntradayPriceBar,
    Outcome,
    PriceBar,
    ReviewStatus,
    SignalAnalysis,
    TranscriptSegment,
    Video,
)


def test_review_status_exposes_approved_contract():
    assert ReviewStatus.APPROVED.value == "approved"


def test_signal_analysis_accepts_task_two_and_three_structured_contract():
    analysis = SignalAnalysis(
        direction=Direction.BULLISH,
        strength=4,
        confidence=0.9,
        horizon_text="下周",
        target_price=None,
        stop_price=2400.0,
        conditions=("跌破2400重新评估",),
        is_retrospective=False,
        is_news_only=False,
        evidence=(),
        summary="明确看多黄金",
        review_status=ReviewStatus.NEEDS_REVIEW,
    )

    assert analysis.summary == "明确看多黄金"


def test_timestamp_only_transcript_and_date_pricebar_match_downstream_contracts():
    segment = TranscriptSegment(10.0, 18.0, "黄金看涨")
    bar = PriceBar(date(2026, 8, 1), 2400.0, 2420.0, 2390.0, 2410.0)

    assert segment.bvid is None
    assert bar.trade_date.isoformat() == "2026-08-01"


def test_outcome_new_qualification_fields_keep_legacy_positional_order():
    outcome = Outcome(
        Direction.BULLISH,
        "2026-08-03",
        2400.0,
        "BV1TEST",
        2410.0,
        2420.0,
        2430.0,
        0.01,
        0.02,
        0.03,
        True,
    )

    assert outcome.exit_1d == 2410.0
    assert outcome.return_20d == 0.03
    assert outcome.mature is True
    assert outcome.signal_id is None
    assert outcome.review_status is ReviewStatus.NEEDS_REVIEW
    assert outcome.included is False


def test_database_is_idempotent_for_creator_and_video(tmp_path):
    db = Database(tmp_path / "goldbook.db")
    db.initialize()
    creator = Creator(uid="42", name="测试UP", space_url="https://space.bilibili.com/42")
    db.upsert_creator(creator)
    db.upsert_creator(creator)
    video = Video(
        bvid="BV1TEST",
        creator_uid="42",
        title="黄金后市",
        published_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        duration_sec=600,
        url="https://www.bilibili.com/video/BV1TEST",
    )
    db.upsert_video(video)
    db.upsert_video(video)

    assert len(db.list_creators()) == 1
    assert len(db.list_videos("42")) == 1


def test_deleting_creator_keeps_shared_prices(tmp_path):
    db = Database(tmp_path / "goldbook.db")
    db.initialize()
    db.upsert_creator(Creator("42", "测试UP", "https://space.bilibili.com/42"))
    db.replace_prices([("2026-08-01", 2400.0, 2420.0, 2390.0, 2410.0)])
    db.delete_creator("42")

    assert db.list_prices()[0][0] == "2026-08-01"


def test_replace_prices_accepts_price_bar_and_five_tuple(tmp_path):
    db = Database(tmp_path / "goldbook.db")
    db.initialize()

    db.replace_prices(
        [
            PriceBar("2026-08-01", 2400.0, 2420.0, 2390.0, 2410.0),
            ("2026-08-02", 2410.0, 2430.0, 2405.0, 2425.0),
        ]
    )

    assert db.list_prices() == [
        ("2026-08-01", 2400.0, 2420.0, 2390.0, 2410.0),
        ("2026-08-02", 2410.0, 2430.0, 2405.0, 2425.0),
    ]


def test_replacing_same_price_date_updates_instead_of_duplicating(tmp_path):
    db = Database(tmp_path / "goldbook.db")
    db.initialize()

    db.replace_prices([PriceBar(date(2026, 8, 1), 2400.0, 2420.0, 2390.0, 2410.0)])
    db.replace_prices([PriceBar(date(2026, 8, 1), 2401.0, 2421.0, 2391.0, 2411.0)])

    assert db.list_prices() == [("2026-08-01", 2401.0, 2421.0, 2391.0, 2411.0)]


def test_intraday_prices_upsert_and_query_by_aware_utc_range(tmp_path):
    db = Database(tmp_path / "goldbook.db")
    db.initialize()
    first = IntradayPriceBar(
        _utc("2026-08-12T10:00:00"), 60, 4400, 4410, 4390, 4405, "XAUS"
    )
    corrected = replace(first, close=4407)
    second = IntradayPriceBar(
        _utc("2026-08-12T11:00:00"), 60, 4407, 4420, 4400, 4418, "XAUS"
    )

    assert db.upsert_intraday_prices((first, second)) == 2
    assert db.upsert_intraday_prices((corrected,)) == 1
    assert db.list_intraday_price_bars(_utc("2026-08-12T10:30:00"), None) == [second]


def test_intraday_price_query_rejects_naive_bounds(tmp_path):
    db = Database(tmp_path / "goldbook.db")
    db.initialize()

    with pytest.raises(ValueError, match="timezone-aware"):
        db.list_intraday_price_bars(datetime(2026, 8, 12, 10), None)


def test_existing_database_migrates_claim_evaluation_timestamp_columns(tmp_path):
    path = tmp_path / "goldbook.db"
    _create_pre_intraday_claim_evaluation_schema(path)

    db = Database(path)
    db.initialize()
    with sqlite3.connect(path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(claim_evaluations)")
        }

    assert {"window_start_at", "window_end_at", "closest_at", "first_hit_at"} <= columns


def test_claim_evaluation_round_trip_preserves_optional_utc_timestamps(tmp_path):
    db = Database(tmp_path / "goldbook.db")
    db.initialize()
    db.upsert_creator(Creator("42", "test", "https://space.bilibili.com/42"))
    db.upsert_video(_video())
    db.replace_forecast_claims("BV1TEST", 0, (_claim(),))
    value = ClaimEvaluation(
        claim_id="claim-1",
        evaluated_at=_utc("2026-08-12T13:00:00"),
        window_start=None,
        window_end=None,
        entry_price=4400,
        observed_min=4390,
        observed_max=4451,
        final_close=4440,
        closest_price=4451,
        closest_date=None,
        distance_pct=0,
        first_hit_date=None,
        verdict=EvaluationVerdict.HIT,
        mature=True,
        reason="target_hit",
        window_start_at=_utc("2026-08-12T11:00:00"),
        window_end_at=_utc("2026-08-12T13:00:00"),
        closest_at=_utc("2026-08-12T12:00:00"),
        first_hit_at=_utc("2026-08-12T12:00:00"),
    )

    db.save_claim_evaluation(value)

    assert db.get_claim_evaluation("claim-1") == value


def test_deleting_creator_cascades_dependent_rows_but_not_prices(tmp_path):
    path = tmp_path / "goldbook.db"
    db = Database(path)
    db.initialize()
    db.upsert_creator(Creator("42", "测试UP", "https://space.bilibili.com/42"))
    db.upsert_video(_video())
    db.save_transcript("BV1TEST", [TranscriptSegment(0.0, 4.5, "黄金看涨")], model="small")
    db.save_analysis(
        _analysis(), bvid="BV1TEST", transcript_hash="transcript-hash"
    )
    db.save_outcome(
        Outcome(
            bvid="BV1TEST",
            direction=Direction.BULLISH,
            entry_date="2026-08-03",
            entry_price=2400.0,
            signal_id="BV1TEST:0",
            review_status=ReviewStatus.APPROVED,
            included=True,
            mature=True,
        )
    )
    db.create_job("42", video_bvid="BV1TEST")
    db.replace_prices([("2026-08-01", 2400.0, 2420.0, 2390.0, 2410.0)])

    db.delete_creator("42")

    with sqlite3.connect(path) as connection:
        for table in ("videos", "transcripts", "analyses", "outcomes", "jobs"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert db.list_prices() == [("2026-08-01", 2400.0, 2420.0, 2390.0, 2410.0)]


def test_repository_rejects_orphan_foreign_key_writes(tmp_path):
    db = Database(tmp_path / "goldbook.db")
    db.initialize()

    with pytest.raises(sqlite3.IntegrityError):
        db.upsert_video(_video())
    with pytest.raises(sqlite3.IntegrityError):
        db.save_transcript("BV1MISSING", [TranscriptSegment(0.0, 4.5, "黄金看涨")])
    with pytest.raises(sqlite3.IntegrityError):
        db.save_analysis(_analysis(), bvid="BV1MISSING", transcript_hash="hash")
    with pytest.raises(sqlite3.IntegrityError):
        db.save_outcome(
            Outcome(
                bvid="BV1MISSING",
                direction=Direction.BULLISH,
                entry_date="2026-08-03",
                entry_price=2400.0,
            )
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.create_job("42")


def test_rejects_naive_datetimes_at_model_boundary():
    with pytest.raises(ValueError, match="timezone-aware"):
        Video(
            bvid="BV1TEST",
            creator_uid="42",
            title="黄金后市",
            published_at=datetime(2026, 8, 1),
            duration_sec=600,
            url="https://www.bilibili.com/video/BV1TEST",
        )


def test_repository_persists_transcript_analysis_outcome_and_job(tmp_path):
    path = tmp_path / "goldbook.db"
    db = Database(path)
    db.initialize()
    db.upsert_creator(Creator("42", "测试UP", "https://space.bilibili.com/42"))
    db.upsert_video(_video())
    db.save_transcript(
        TranscriptSegment(
            bvid="BV1TEST",
            start_sec=0.0,
            end_sec=4.5,
            text="黄金看涨",
            model="small",
            text_hash="transcript-hash",
        )
    )
    db.save_analysis(
        SignalAnalysis(
            bvid="BV1TEST",
            transcript_hash="transcript-hash",
            direction=Direction.BULLISH,
            strength=1,
            confidence=0.9,
        )
    )
    db.save_outcome(
        Outcome(
            bvid="BV1TEST",
            direction=Direction.BULLISH,
            entry_date="2026-08-03",
            entry_price=2400.0,
            signal_id="BV1TEST",
            review_status=ReviewStatus.APPROVED,
            included=True,
            mature=True,
        )
    )
    job_id = db.create_job("42", video_bvid="BV1TEST")
    assert db.claim_pending_job(job_id)
    assert db.advance_job(job_id, "downloading", 0.0)
    assert db.advance_job(job_id, "transcribing", 0.25)
    assert db.advance_job(job_id, "analyzing", 0.5)
    assert db.advance_job(job_id, "pricing", 0.75)
    assert db.complete_job(job_id)

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM analyses").fetchone()[0] == 1
        assert connection.execute(
            "SELECT signal_id, review_status, included, mature FROM outcomes"
        ).fetchone() == ("BV1TEST", "approved", 1, 1)
        assert connection.execute(
            "SELECT stage, progress FROM jobs WHERE id = ?", (job_id,)
        ).fetchone() == ("complete", 1.0)


def test_repository_lists_one_deterministic_latest_metric_sample_per_video(tmp_path):
    db = Database(tmp_path / "goldbook.db")
    db.initialize()
    db.upsert_creator(Creator("42", "测试UP", "https://space.bilibili.com/42"))
    video = _video()
    db.upsert_video(video)
    db.save_analysis(
        SignalAnalysis(
            direction=Direction.BEARISH, strength=2, confidence=0.9,
            review_status=ReviewStatus.APPROVED, bvid=video.bvid, transcript_hash="hash",
        )
    )
    db.save_analysis(
        SignalAnalysis(
            direction=Direction.BULLISH, strength=4, confidence=0.65,
            review_status=ReviewStatus.APPROVED, bvid=video.bvid, transcript_hash="hash", revision=1,
        )
    )
    db.save_outcome(
        Outcome(
            bvid=video.bvid, direction=Direction.BULLISH, entry_date="2026-08-03",
            entry_price=100.0, signal_id=video.bvid, review_status=ReviewStatus.APPROVED,
            included=True, return_1d=0.1, return_5d=0.2, mature=True,
        )
    )

    samples = db.list_creator_metric_samples("42")

    assert samples == [
        CreatorMetricSample(
            bvid=video.bvid, signal_id=video.bvid, direction=Direction.BULLISH,
            review_status=ReviewStatus.APPROVED, included=True, mature=True,
            entry_price=100.0, return_1d=0.1, return_5d=0.2, return_20d=None,
            confidence=0.65, manual_revision=True, disposition="approved",
        )
    ]


def test_repository_aggregates_creator_metrics_from_latest_metric_samples(tmp_path):
    db = Database(tmp_path / "goldbook.db")
    db.initialize()
    db.upsert_creator(Creator("42", "测试UP", "https://space.bilibili.com/42"))
    video = _video()
    db.upsert_video(video)
    db.save_analysis(
        SignalAnalysis(
            direction=Direction.BULLISH, strength=4, confidence=0.9,
            review_status=ReviewStatus.APPROVED, bvid=video.bvid, transcript_hash="hash",
        )
    )
    db.save_outcome(
        Outcome(
            bvid=video.bvid, direction=Direction.BULLISH, entry_date="2026-08-03",
            entry_price=100.0, signal_id=video.bvid, review_status=ReviewStatus.APPROVED,
            included=True, return_1d=0.1, return_5d=0.2, mature=True,
        )
    )

    metrics = db.list_creator_metrics()

    assert len(metrics) == 1
    assert metrics[0].bullish_count == 1
    assert metrics[0].average_signed_return_5d == 0.2

def test_initialize_backfills_provable_legacy_outcome_qualification(tmp_path):
    path = tmp_path / "legacy.db"
    _insert_legacy_outcome(path, "BV1PROVEN")
    _insert_legacy_analysis(
        path,
        "BV1PROVEN",
        review_status="approved",
        signal_json='{"is_retrospective":false,"is_news_only":false}',
    )

    db = Database(path)
    db.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT signal_id, review_status, included FROM outcomes WHERE bvid = ?",
            ("BV1PROVEN",),
        ).fetchone() == ("BV1PROVEN", "approved", 1)
    assert db.list_outcome_recomputation_requirements() == []


def test_initialize_surfaces_unprovable_legacy_outcome_until_recomputed(tmp_path):
    path = tmp_path / "legacy.db"
    _insert_legacy_outcome(path, "BV1UNKNOWN")

    db = Database(path)
    db.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT signal_id, review_status, included FROM outcomes WHERE bvid = ?",
            ("BV1UNKNOWN",),
        ).fetchone() == ("BV1UNKNOWN", "needs_review", 0)
    assert db.list_outcome_recomputation_requirements() == [
        ("BV1UNKNOWN", "missing analysis provenance")
    ]

    db.upsert_creator(Creator("42", "测试UP", "https://space.bilibili.com/42"))
    db.upsert_video(
        Video(
            bvid="BV1UNKNOWN",
            creator_uid="42",
            title="黄金后市",
            published_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            duration_sec=600,
            url="https://www.bilibili.com/video/BV1UNKNOWN",
        )
    )
    db.save_outcome(
        Outcome(
            bvid="BV1UNKNOWN",
            direction=Direction.BULLISH,
            entry_date="2026-08-03",
            entry_price=2400.0,
            signal_id="BV1UNKNOWN",
            review_status=ReviewStatus.APPROVED,
            included=True,
            exit_5d=2420.0,
            return_5d=0.01,
            mature=True,
        )
    )

    assert db.list_outcome_recomputation_requirements() == []


def test_initialize_deterministically_excludes_proven_retrospective_legacy_outcome(tmp_path):
    path = tmp_path / "legacy.db"
    _insert_legacy_outcome(path, "BV1RETRO")
    _insert_legacy_analysis(
        path,
        "BV1RETRO",
        review_status="approved",
        signal_json='{"is_retrospective":true,"is_news_only":false}',
    )

    db = Database(path)
    db.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT signal_id, review_status, included FROM outcomes WHERE bvid = ?",
            ("BV1RETRO",),
        ).fetchone() == ("BV1RETRO", "approved", 0)
    assert db.list_outcome_recomputation_requirements() == []


@pytest.mark.parametrize("analysis_direction", ["neutral", "no_signal"])
def test_initialize_requires_recomputation_for_non_directional_legacy_analysis(
    tmp_path, analysis_direction
):
    path = tmp_path / "legacy.db"
    _insert_legacy_outcome(path, "BV1NONDIRECTIONAL")
    _insert_legacy_analysis(
        path,
        "BV1NONDIRECTIONAL",
        review_status="approved",
        direction=analysis_direction,
        signal_json='{"is_retrospective":false,"is_news_only":false}',
    )

    db = Database(path)
    db.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT review_status, included FROM outcomes WHERE bvid = ?",
            ("BV1NONDIRECTIONAL",),
        ).fetchone() == ("approved", 0)
    assert db.list_outcome_recomputation_requirements() == [
        ("BV1NONDIRECTIONAL", "analysis direction is not actionable")
    ]


def test_initialize_requires_recomputation_for_direction_mismatched_legacy_outcome(tmp_path):
    path = tmp_path / "legacy.db"
    _insert_legacy_outcome(path, "BV1MISMATCH")
    _insert_legacy_analysis(
        path,
        "BV1MISMATCH",
        review_status="approved",
        direction="bearish",
        signal_json='{"is_retrospective":false,"is_news_only":false}',
    )

    db = Database(path)
    db.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT review_status, included FROM outcomes WHERE bvid = ?",
            ("BV1MISMATCH",),
        ).fetchone() == ("approved", 0)
    assert db.list_outcome_recomputation_requirements() == [
        ("BV1MISMATCH", "analysis direction differs from outcome")
    ]


def _video():
    return Video(
        bvid="BV1TEST",
        creator_uid="42",
        title="黄金后市",
        published_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        duration_sec=600,
        url="https://www.bilibili.com/video/BV1TEST",
    )


def _analysis():
    return SignalAnalysis(
        direction=Direction.BULLISH,
        strength=1,
        confidence=0.9,
        horizon_text=None,
        target_price=None,
        stop_price=None,
        conditions=(),
        is_retrospective=False,
        is_news_only=False,
        evidence=(),
        summary="黄金看涨",
    )


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _claim() -> ForecastClaim:
    return ForecastClaim(
        claim_id="claim-1",
        bvid="BV1TEST",
        analysis_revision=0,
        claim_index=0,
        instrument=Instrument.XAU_USD_SPOT,
        claim_type=ClaimType.TARGET_TOUCH,
        direction=Direction.BULLISH,
        legs=(ClaimLeg(">=", 4450, 4450),),
        condition_text="",
        horizon_text="未来2小时",
        horizon_source=HorizonSource.EXPLICIT_RELATIVE,
        horizon_min_trading_days=0,
        horizon_max_trading_days=0,
        horizon_point_trading_days=0,
        deadline_at=None,
        time_confidence=0.9,
        confidence=0.9,
        evidence=(),
        supersedes_claim_id=None,
        status=ClaimStatus.AUTO_VALIDATED,
        model_name="MiniMax-M3",
        prompt_version="claims-v1",
        transcript_hash="hash",
    )


def _create_pre_intraday_claim_evaluation_schema(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE claim_evaluations (
                claim_id TEXT PRIMARY KEY,
                evaluated_at TEXT NOT NULL,
                window_start TEXT,
                window_end TEXT,
                entry_price REAL,
                observed_min REAL,
                observed_max REAL,
                final_close REAL,
                closest_price REAL,
                closest_date TEXT,
                distance_pct REAL,
                first_hit_date TEXT,
                verdict TEXT NOT NULL,
                mature INTEGER NOT NULL,
                reason TEXT NOT NULL
            )
            """
        )


def _insert_legacy_outcome(path, bvid: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE outcomes (
                bvid TEXT PRIMARY KEY,
                direction TEXT NOT NULL,
                entry_date TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_1d REAL,
                exit_5d REAL,
                exit_20d REAL,
                return_1d REAL,
                return_5d REAL,
                return_20d REAL,
                mature INTEGER NOT NULL
            );
            CREATE TABLE analyses (
                id INTEGER PRIMARY KEY,
                bvid TEXT NOT NULL,
                transcript_hash TEXT NOT NULL,
                raw_response_hash TEXT,
                signal_json TEXT NOT NULL,
                direction TEXT NOT NULL,
                strength INTEGER NOT NULL,
                confidence REAL NOT NULL,
                review_status TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE (bvid, revision)
            );
            """
        )
        connection.execute(
            """
            INSERT INTO outcomes (
                bvid, direction, entry_date, entry_price, exit_5d, return_5d, mature
            ) VALUES (?, 'bullish', '2026-08-03', 2400, 2420, 0.01, 1)
            """,
            (bvid,),
        )


def _insert_legacy_analysis(
    path, bvid: str, *, review_status: str, signal_json: str, direction: str = "bullish"
) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO analyses (
                bvid, transcript_hash, signal_json, direction, strength, confidence,
                review_status, revision, created_at
            ) VALUES (?, 'hash', ?, ?, 4, 0.9, ?, 0, '2026-08-01T00:00:00+00:00')
            """,
            (bvid, signal_json, direction, review_status),
        )
