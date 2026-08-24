from datetime import datetime, timezone

from goldbook.db import Database
from goldbook.fact_check import (
    BranchDecision,
    BranchPredicate,
    BranchStatus,
    FactCheckImpact,
    FactCheckResult,
    FactValue,
    SearchEvidence,
)
from goldbook.models import Creator, Direction, SignalAnalysis, Video


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def _database(tmp_path):
    database = Database(tmp_path / "goldbook.db")
    database.initialize()
    database.upsert_creator(Creator("42", "测试UP", "https://space.bilibili.com/42"))
    database.upsert_video(
        Video(
            "BV1CPI",
            "42",
            "CPI预测",
            datetime(2026, 8, 12, 0, 51, tzinfo=timezone.utc),
            120,
            "https://www.bilibili.com/video/BV1CPI",
        )
    )
    database.save_analysis(
        SignalAnalysis(
            Direction.NEUTRAL,
            2,
            0.8,
            bvid="BV1CPI",
            transcript_hash="hash-1",
            revision=1,
            model_name="MiniMax-M3",
        )
    )
    return database


def _evidence(identifier, domain):
    return SearchEvidence(
        identifier,
        "US CPI actual forecast",
        f"Evidence {identifier}",
        f"https://{domain}/cpi",
        domain,
        NOW,
        "Actual and consensus were both 0.1 percent.",
        NOW,
    )


def _result():
    return FactCheckResult(
        "Was CPI supportive for gold?",
        "US CPI July 2026",
        datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc),
        (FactValue("headline_mom", "0.1", "0.1", "-0.4", "%"),),
        FactCheckImpact.NEUTRAL,
        "Actual matched consensus.",
        ("e1", "e2"),
        (
            BranchDecision(
                "BV1CPI:1:1",
                BranchPredicate.SUPPORTIVE,
                BranchStatus.NOT_TRIGGERED,
                "Neutral did not trigger the supportive branch.",
            ),
        ),
        0.88,
    )


def test_fact_check_round_trip_is_bound_to_latest_analysis_revision(tmp_path):
    database = _database(tmp_path)
    run = database.create_fact_check_run(
        "BV1CPI",
        analysis_revision=1,
        event_description="今晚CPI数据",
        model_name="MiniMax-M3",
        created_at=NOW,
    )
    repeated = database.create_fact_check_run(
        "BV1CPI",
        analysis_revision=1,
        event_description="今晚CPI数据",
        model_name="MiniMax-M3",
        created_at=NOW,
    )
    assert repeated.run_id == run.run_id

    database.save_fact_check_evidence(run.run_id, (_evidence("e1", "one.example"), _evidence("e2", "two.example")))
    database.save_fact_check_result(run.run_id, _result(), search_count=2, completed_at=NOW)

    stored = database.get_current_fact_check("BV1CPI")
    assert stored is not None
    assert stored.run.status.value == "completed"
    assert stored.run.search_count == 2
    assert stored.evidence == (_evidence("e1", "one.example"), _evidence("e2", "two.example"))
    assert stored.result == _result()

    database.save_analysis(
        SignalAnalysis(
            Direction.BULLISH,
            3,
            0.9,
            bvid="BV1CPI",
            transcript_hash="hash-2",
            revision=2,
            model_name="MiniMax-M3",
        )
    )
    assert database.get_current_fact_check("BV1CPI") is None


def test_fact_check_rows_cascade_when_video_is_deleted(tmp_path):
    database = _database(tmp_path)
    run = database.create_fact_check_run(
        "BV1CPI", 1, "今晚CPI数据", "MiniMax-M3", created_at=NOW
    )
    database.save_fact_check_evidence(run.run_id, (_evidence("e1", "one.example"),))

    with database._connect() as connection:
        connection.execute("DELETE FROM videos WHERE bvid = 'BV1CPI'")
        assert connection.execute("SELECT COUNT(*) FROM fact_check_runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM fact_check_evidence").fetchone()[0] == 0


def test_fact_check_job_is_idempotent_and_distinct_from_a_video_job(tmp_path):
    database = _database(tmp_path)
    video_job = database.create_or_get_active_video_job("42", "BV1CPI")
    first = database.create_or_get_active_fact_check_job("42", "BV1CPI")
    second = database.create_or_get_active_fact_check_job("42", "BV1CPI")

    assert first.id == second.id
    assert first.kind == "fact_check"
    assert first.id != video_job.id
    assert database.get_active_video_job("BV1CPI").id == video_job.id
    assert database.get_active_fact_check_job("BV1CPI").id == first.id
