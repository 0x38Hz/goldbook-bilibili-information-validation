import json
from datetime import datetime, timezone

import pytest

from goldbook.claim_pipeline import (
    ClaimBackfillSummary,
    _matching_direction_evidence,
    reanalyse_cached_claims,
)
import goldbook.claim_pipeline as claim_pipeline
from goldbook.db import Database
from goldbook.minimax import MiniMaxClient
from goldbook.models import (
    Creator,
    Direction,
    ReviewStatus,
    SignalAnalysis,
    TranscriptSegment,
    Video,
    VideoStatus,
)


def _payload() -> str:
    return json.dumps(
        {
            "summary": "短期目标4700",
            "primary_trend": {
                "direction": "bullish",
                "condition_text": "短期目标4700",
                "horizon_text": "短期",
                "horizon_source": "context_inferred",
                "horizon_min_trading_days": 1,
                "horizon_max_trading_days": 3,
                "horizon_point_trading_days": 2,
                "deadline_at": None,
                "time_confidence": 0.8,
                "confidence": 0.9,
                "evidence": [
                    {
                        "start_sec": 0.0,
                        "end_sec": 4.0,
                        "quote": "短期目标4700",
                    }
                ],
                "status": "auto_validated",
            },
            "claims": [
                {
                    "instrument": "xau_usd_spot",
                    "claim_type": "target_touch",
                    "direction": "bullish",
                    "legs": [
                        {"operator": ">=", "level_low": 4700, "level_high": None}
                    ],
                    "condition_text": "短期目标4700",
                    "horizon_text": "短期",
                    "horizon_source": "context_inferred",
                    "horizon_min_trading_days": 1,
                    "horizon_max_trading_days": 3,
                    "horizon_point_trading_days": 2,
                    "deadline_at": None,
                    "time_confidence": 0.8,
                    "confidence": 0.9,
                    "evidence": [
                        {
                            "start_sec": 0.0,
                            "end_sec": 4.0,
                            "quote": "短期目标4700",
                        }
                    ],
                    "status": "auto_validated",
                }
            ],
        },
        ensure_ascii=False,
    )


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "goldbook.db")
    database.initialize()
    database.upsert_creator(Creator("42", "测试UP", "https://space.bilibili.com/42"))
    database.upsert_video(
        Video(
            "BV1PIPE",
            "42",
            "黄金目标",
            datetime(2026, 8, 3, tzinfo=timezone.utc),
            60,
            "https://www.bilibili.com/video/BV1PIPE",
        )
    )
    database.save_transcript(
        "BV1PIPE",
        [TranscriptSegment(0.0, 4.0, "短期目标4700")],
        model="small",
        text_hash="transcript-hash",
    )
    return database


def test_backfill_uses_cached_transcript_and_skips_exact_completed_identity(tmp_path):
    database = _database(tmp_path)
    calls = 0

    def request(_segments):
        nonlocal calls
        calls += 1
        return _payload()

    client = MiniMaxClient.for_test(request)
    progress = []

    first = reanalyse_cached_claims(
        database,
        client,
        evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        on_progress=lambda done, total, bvid, status: progress.append(
            (done, total, bvid, status)
        ),
    )
    second = reanalyse_cached_claims(
        database,
        client,
        evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )

    assert first == ClaimBackfillSummary(
        total=1,
        completed=1,
        skipped=0,
        failed=0,
        primary_trends=1,
        bullish=1,
    )
    assert second == ClaimBackfillSummary(total=1, completed=0, skipped=1, failed=0)
    assert calls == 1
    assert progress == [(1, 1, "BV1PIPE", "completed")]
    claim = database.list_forecast_claims("BV1PIPE")[1]
    assert claim.legs[0].level_low == 4700
    assert database.get_latest_analysis("BV1PIPE").model_name == "MiniMax-M3"


def test_v2_backfill_reports_one_primary_trend_per_completed_video(tmp_path):
    database = _database(tmp_path)

    summary = reanalyse_cached_claims(
        database,
        MiniMaxClient.for_test(lambda _segments: _payload()),
        evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )

    assert summary.primary_trends == summary.completed == 1
    assert summary.bullish == 1
    assert summary.bullish + summary.bearish + summary.neutral + summary.no_signal == 1


def test_invalid_v2_primary_uses_only_locatable_prior_m3_trend_evidence(tmp_path):
    database = _database(tmp_path)
    database.save_analysis(
        SignalAnalysis(
            bvid="BV1PIPE",
            transcript_hash="transcript-hash",
            direction=Direction.BULLISH,
            strength=4,
            confidence=0.86,
            horizon_text="短期",
            evidence=(
                {"start_sec": 0.0, "end_sec": 4.0, "quote": "短期目标4700"},
            ),
            summary="短期仍然偏多",
            review_status=ReviewStatus.APPROVED,
            revision=1,
            model_name="MiniMax-M3",
            prompt_version="claims-v1",
        )
    )
    invalid = json.loads(_payload())
    invalid["primary_trend"]["evidence"][0]["quote"] = "字幕中不存在"

    summary = reanalyse_cached_claims(
        database,
        MiniMaxClient.for_test(
            lambda _segments: json.dumps(invalid, ensure_ascii=False)
        ),
        evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )

    claim = database.list_forecast_claims("BV1PIPE")[0]
    assert summary.completed == 1
    assert summary.failed == 0
    assert claim.direction is Direction.BULLISH
    assert claim.condition_text == "短期目标4700"
    assert claim.evidence[0]["quote"] == "短期目标4700"


def test_legacy_direction_can_anchor_to_an_explicit_matching_transcript_segment(tmp_path):
    database = _database(tmp_path)
    database.save_transcript(
        "BV1PIPE",
        [TranscriptSegment(0.0, 4.0, "黄金后面继续上涨")],
        model="small",
        text_hash="direction-hash",
    )
    database.save_analysis(
        SignalAnalysis(
            bvid="BV1PIPE",
            transcript_hash="direction-hash",
            direction=Direction.BULLISH,
            strength=4,
            confidence=0.82,
            evidence=(
                {"start_sec": 0.0, "end_sec": 4.0, "quote": "不存在的改写"},
            ),
            summary="继续偏多",
            review_status=ReviewStatus.APPROVED,
            revision=1,
            model_name="MiniMax-M3",
            prompt_version="claims-v1",
        )
    )
    invalid = json.loads(_payload())
    invalid["primary_trend"]["evidence"][0]["quote"] = "字幕中不存在"

    summary = reanalyse_cached_claims(
        database,
        MiniMaxClient.for_test(
            lambda _segments: json.dumps(invalid, ensure_ascii=False)
        ),
        evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )

    claim = database.list_forecast_claims("BV1PIPE")[0]
    assert summary.completed == 1
    assert claim.direction is Direction.BULLISH
    assert claim.condition_text == "黄金后面继续上涨"
    assert claim.evidence == (
        {"start_sec": 0.0, "end_sec": 4.0, "quote": "黄金后面继续上涨"},
    )


@pytest.mark.parametrize(
    ("direction", "text"),
    [
        (Direction.NEUTRAL, "这个位置可以选择观望，看它涨上去还是跌下来"),
        (Direction.BULLISH, "明天原油可能低开，而黄金会受到支撑"),
    ],
)
def test_grounded_fallback_recognizes_explicit_neutral_and_support_language(
    direction, text
):
    evidence = _matching_direction_evidence(
        direction,
        [TranscriptSegment(10.0, 20.0, text)],
    )

    assert evidence == (
        {"start_sec": 10.0, "end_sec": 20.0, "quote": text},
    )


def test_complete_video_without_transcript_gets_explicit_unresolved_no_signal(tmp_path):
    database = Database(tmp_path / "goldbook.db")
    database.initialize()
    database.upsert_creator(Creator("42", "测试UP", "https://space.bilibili.com/42"))
    video = Video(
        "BVNOTEXT",
        "42",
        "没有可用字幕",
        datetime(2026, 8, 3, tzinfo=timezone.utc),
        60,
        "https://www.bilibili.com/video/BVNOTEXT",
        status=VideoStatus.COMPLETE,
    )
    database.upsert_video(video)
    database.save_analysis(
        SignalAnalysis(
            bvid=video.bvid,
            transcript_hash="legacy-empty",
            direction=Direction.NO_SIGNAL,
            strength=1,
            confidence=0.0,
            summary="无字幕",
            review_status=ReviewStatus.APPROVED,
            revision=1,
        )
    )
    calls = 0

    def request(_segments):
        nonlocal calls
        calls += 1
        return _payload()

    summary = reanalyse_cached_claims(
        database,
        MiniMaxClient.for_test(request),
        evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )

    claim = database.list_forecast_claims(video.bvid)[0]
    assert summary.total == summary.completed == summary.no_signal == 1
    assert calls == 0
    assert claim.direction is Direction.NO_SIGNAL
    assert claim.status.value == "unresolved"
    assert claim.evidence == ()
    assert claim.condition_text == "本地无可用字幕，无法提取趋势"


def test_failed_structured_response_leaves_no_cache_identity_and_retries_next_run(tmp_path):
    database = _database(tmp_path)
    progress = []
    failed = reanalyse_cached_claims(
        database,
        MiniMaxClient.for_test(
            lambda _segments: json.dumps(
                {**json.loads(_payload()), "claims": "bad"}, ensure_ascii=False
            )
        ),
        evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        on_progress=lambda done, total, bvid, status: progress.append(
            (done, total, bvid, status)
        ),
    )

    assert failed.failed == 1
    assert progress == [
        (
            1,
            1,
            "BV1PIPE",
            "failed (invalid structured response: claims must be a list)",
        )
    ]
    assert database.list_forecast_claims("BV1PIPE") == []
    assert not database.has_claim_extraction(
        "BV1PIPE", "transcript-hash", "MiniMax-M3", "claims-v3-grounded-context"
    )

    retried = reanalyse_cached_claims(
        database,
        MiniMaxClient.for_test(lambda _segments: _payload()),
        evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )

    assert retried.completed == 1
    assert database.list_forecast_claims("BV1PIPE")[1].condition_text == "短期目标4700"


def test_backfill_persists_in_configured_concurrency_checkpoints(tmp_path, monkeypatch):
    database = _database(tmp_path)
    for index in range(2, 8):
        bvid = f"BV1PIPE{index}"
        database.upsert_video(
            Video(
                bvid,
                "42",
                "黄金目标",
                datetime(2026, 8, 3 + index, tzinfo=timezone.utc),
                60,
                f"https://www.bilibili.com/video/{bvid}",
            )
        )
        database.save_transcript(
            bvid,
            [TranscriptSegment(0.0, 4.0, "短期目标4700")],
            model="small",
            text_hash=f"transcript-hash-{index}",
        )
    batch_sizes = []
    real_batch = claim_pipeline.run_claim_extraction_batch

    def recording_batch(client, items):
        batch_sizes.append(len(items))
        return real_batch(client, items)

    monkeypatch.setattr(claim_pipeline, "run_claim_extraction_batch", recording_batch)

    summary = reanalyse_cached_claims(
        database,
        MiniMaxClient.for_test(lambda _segments: _payload(), max_concurrency=5),
        evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )

    assert summary.completed == 7
    assert batch_sizes == [5, 2]
    assert sum(
        bool(database.list_forecast_claims(video.bvid))
        for video in database.list_videos("42")
    ) == 7
