from datetime import date, datetime, timezone

from goldbook.db import Database
from goldbook.models import (
    ClaimEvaluation,
    ClaimLeg,
    ClaimStatus,
    ClaimType,
    Creator,
    Direction,
    EvaluationVerdict,
    ForecastClaim,
    HorizonSource,
    Instrument,
    ReviewStatus,
    SignalAnalysis,
    Video,
)


def _claim(*, revision: int = 1) -> ForecastClaim:
    return ForecastClaim(
        claim_id=f"BV1CLAIM:{revision}:0",
        bvid="BV1CLAIM",
        analysis_revision=revision,
        claim_index=0,
        instrument=Instrument.XAU_USD_SPOT,
        claim_type=ClaimType.SEQUENCE,
        direction=Direction.BULLISH,
        legs=(ClaimLeg("<=", 4650.0, None), ClaimLeg(">=", 4700.0, None)),
        condition_text="先回踩4650再看4700",
        horizon_text="短期",
        horizon_source=HorizonSource.CONTEXT_INFERRED,
        horizon_min_trading_days=1,
        horizon_max_trading_days=3,
        horizon_point_trading_days=2,
        deadline_at=None,
        time_confidence=0.81,
        confidence=0.92,
        evidence=(
            {
                "start_sec": 3.0,
                "end_sec": 8.0,
                "quote": "先回踩4650再看4700",
            },
        ),
        supersedes_claim_id=None,
        status=ClaimStatus.AUTO_VALIDATED,
        model_name="MiniMax-M3",
        prompt_version="claims-v1",
        transcript_hash="hash-1",
    )


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "goldbook.db")
    database.initialize()
    database.upsert_creator(Creator("42", "测试UP", "https://space.bilibili.com/42"))
    database.upsert_video(
        Video(
            "BV1CLAIM",
            "42",
            "黄金点位",
            datetime(2026, 8, 3, 4, 0, tzinfo=timezone.utc),
            60,
            "https://www.bilibili.com/video/BV1CLAIM",
        )
    )
    return database


def test_claims_and_evaluations_round_trip_without_losing_typed_fields(tmp_path):
    database = _database(tmp_path)
    expected_claim = _claim()

    database.replace_forecast_claims("BV1CLAIM", 1, [expected_claim])

    assert database.list_forecast_claims("BV1CLAIM") == [expected_claim]

    expected_evaluation = ClaimEvaluation(
        claim_id=expected_claim.claim_id,
        evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        window_start=date(2026, 8, 4),
        window_end=date(2026, 8, 6),
        entry_price=4660.0,
        observed_min=4648.0,
        observed_max=4702.0,
        final_close=4698.0,
        closest_price=4702.0,
        closest_date=date(2026, 8, 5),
        distance_pct=0.0,
        first_hit_date=date(2026, 8, 5),
        verdict=EvaluationVerdict.HIT,
        mature=True,
        reason="sequence satisfied",
    )
    database.save_claim_evaluation(expected_evaluation)

    assert database.get_claim_evaluation(expected_claim.claim_id) == expected_evaluation
    assert database.has_claim_extraction(
        "BV1CLAIM", "hash-1", "MiniMax-M3", "claims-v1"
    )


def test_replacing_same_revision_is_idempotent_and_creator_delete_cascades(tmp_path):
    database = _database(tmp_path)
    expected_claim = _claim()
    database.replace_forecast_claims("BV1CLAIM", 1, [expected_claim])
    database.replace_forecast_claims("BV1CLAIM", 1, [expected_claim])
    database.save_claim_evaluation(
        ClaimEvaluation(
            claim_id=expected_claim.claim_id,
            evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
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
            verdict=EvaluationVerdict.UNRESOLVED,
            mature=False,
            reason="horizon_not_mature",
        )
    )

    assert database.list_forecast_claims("BV1CLAIM") == [expected_claim]

    database.delete_creator("42")

    assert database.list_forecast_claims("BV1CLAIM", latest_only=False) == []
    assert database.get_claim_evaluation(expected_claim.claim_id) is None


def test_latest_no_claim_analysis_hides_claims_from_an_older_revision(tmp_path):
    database = _database(tmp_path)
    database.replace_forecast_claims("BV1CLAIM", 1, [_claim(revision=1)])
    database.save_analysis(
        SignalAnalysis(
            bvid="BV1CLAIM",
            transcript_hash="hash-2",
            direction=Direction.NO_SIGNAL,
            strength=1,
            confidence=0.95,
            summary="没有可执行预测",
            review_status=ReviewStatus.APPROVED,
            signal_json='{"summary":"没有可执行预测","claims":[]}',
            revision=2,
            model_name="MiniMax-M3",
            prompt_version="claims-v1",
        )
    )

    assert database.has_claim_extraction(
        "BV1CLAIM", "hash-2", "MiniMax-M3", "claims-v1"
    )
    assert database.list_forecast_claims("BV1CLAIM") == []
    assert database.list_creator_forecast_claims("42") == []
