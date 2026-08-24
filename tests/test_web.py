from datetime import datetime, timezone
import json
import re
from pathlib import Path

import pytest

from goldbook.config import Settings
from goldbook.db import Database
from goldbook.models import (
    Creator,
    Direction,
    Outcome,
    PriceBar,
    ReviewStatus,
    SignalAnalysis,
    TranscriptSegment,
    Video,
)
from goldbook.scoring import score_signal


@pytest.fixture
def app_settings(tmp_path):
    return Settings(
        data_dir=tmp_path,
        web_host="127.0.0.1",
        web_port=8765,
        lookback_days=183,
        minimax_api_key="test-secret-key",
        minimax_base_url="https://api.minimaxi.com/v1",
        minimax_model="test-model",
        minimax_max_concurrency=1,
        whisper_model="small",
        whisper_device="cpu",
    )


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "goldbook.db")
    db.initialize()
    return db


@pytest.fixture
def app(app_settings, database):
    from goldbook.web import create_app

    application = create_app(app_settings, database, pipeline=_QueueOnlyPipeline(database))
    application.config.update(TESTING=True)
    return application


class _QueueOnlyPipeline:
    """Offline queue boundary; it never lists public videos in a route test."""

    def __init__(self, db):
        self.db = db

    def enqueue_creator_sync(self, source, _now):
        uid = "42" if source.startswith("BV") or "/video/BV" in source else source.rstrip("/").rsplit("/", 1)[-1]
        self.db.upsert_creator(Creator(uid, uid, source))
        return self.db.create_or_get_active_creator_sync_job(uid)

    def retry_job(self, job_id):
        return self.db.retry_job(job_id)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def seeded_video(database):
    video = Video(
        "BVREVIEW",
        "42",
        "黄金观点",
        datetime(2026, 8, 1, tzinfo=timezone.utc),
        60,
        "https://www.bilibili.com/video/BVREVIEW",
    )
    database.upsert_creator(Creator("42", "测试UP", "https://space.bilibili.com/42"))
    database.upsert_video(video)
    database.save_transcript(
        video.bvid,
        (TranscriptSegment(0.0, 3.0, "黄金可能上涨"),),
        model="test-whisper",
        text_hash="transcript-hash",
    )
    analysis = SignalAnalysis(
        Direction.BULLISH,
        3,
        0.8,
        bvid=video.bvid,
        transcript_hash="transcript-hash",
        summary="原始看多",
        evidence=({"start_sec": 0.0, "end_sec": 3.0, "quote": "黄金可能上涨"},),
        review_status=ReviewStatus.NEEDS_REVIEW,
    )
    database.save_analysis(analysis)
    bars = [
        PriceBar(f"2026-08-{day:02d}", 2400 + day, 2405 + day, 2395 + day, 2402 + day)
        for day in range(2, 24)
    ]
    database.replace_prices(bars)
    outcome = score_signal(analysis, video.published_at, bars)
    if outcome.entry_date is not None:
        database.save_outcome(outcome)
    return video


def test_index_and_leaderboard_render(client):
    assert client.get("/").status_code == 200
    response = client.get("/leaderboard")
    assert response.status_code == 200
    assert "历史信号统计" in response.get_data(as_text=True)


def test_index_labels_video_trend_and_evaluable_counts_separately(client, seeded_video):
    body = client.get("/").get_data(as_text=True)

    assert "视频数" in body
    assert "趋势已提取" in body
    assert "已可核验" in body
    assert "严格收益样本" in body
    assert "78" not in body


def test_api_key_is_not_exposed_anywhere(client, app_settings):
    secret = app_settings.minimax_api_key
    for path in ["/", "/leaderboard", "/api/status"]:
        response = client.get(path)
        assert secret not in response.get_data(as_text=True)


def test_local_responses_set_baseline_browser_security_headers(client):
    response = client.get("/api/status")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]


def test_create_creator_accepts_only_bilibili_source(client):
    bad = client.post("/api/creators", json={"source": "https://example.com/u/1"})
    assert bad.status_code == 400
    good = client.post("/api/creators", json={"source": "https://space.bilibili.com/42"})
    assert good.status_code == 202
    assert good.get_json()["ok"] is True
    assert good.get_json()["data"] == {"creator_uid": "42", "job_id": 1}


def test_dashboard_script_links_a_queued_uid_to_its_creator_progress_page():
    script = (Path(__file__).resolve().parents[1] / "goldbook" / "static" / "app.js").read_text(encoding="utf-8")

    assert '查看处理进度' in script
    assert 'payload.data.creator_uid' in script


def test_job_status_api_returns_safe_local_summary(client, app_settings):
    client.post("/api/creators", json={"source": "https://space.bilibili.com/42"})
    response = client.get("/api/jobs")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["data"][0]["kind"] == "sync_creator"
    assert app_settings.minimax_api_key not in response.get_data(as_text=True)


def test_leaderboard_uses_five_day_average_signed_return_for_ordering(client, database):
    _seed_ranked_outcomes(database, "100", "平均甲", [0.5, 0.5, -0.9])
    _seed_ranked_outcomes(database, "200", "平均乙", [0.0, 0.0, 0.0])

    body = client.get("/leaderboard").get_data(as_text=True)

    assert "5 日平均" in body
    assert body.index("平均甲") < body.index("平均乙")
    assert "+3.33%" in body


def test_creator_forms_work_without_javascript(client, database):
    index = client.get("/").get_data(as_text=True)
    assert 'method="post" action="/creators"' in index
    created = client.post(
        "/creators", data={"source": "https://space.bilibili.com/42"}, follow_redirects=False
    )
    assert created.status_code == 303
    assert database.get_creator("42") is not None
    detail = client.get("/").get_data(as_text=True)
    assert 'method="post" action="/creators/42/sync"' in detail
    synced = client.post("/creators/42/sync", follow_redirects=False)
    assert synced.status_code == 303


@pytest.mark.parametrize("method,path,status", [("get", "/api/not-a-route", 404), ("get", "/api/creators", 405)])
def test_unknown_or_disallowed_api_routes_use_json_error_envelope(client, method, path, status):
    response = getattr(client, method)(path)
    assert response.status_code == status
    assert response.is_json
    assert response.get_json()["ok"] is False
    assert response.get_json()["error"]["code"] in {"not_found", "method_not_allowed"}


def test_manual_review_recomputes_outcome(client, seeded_video):
    response = client.post(
        f"/videos/{seeded_video.bvid}",
        data={
            "direction": "bearish",
            "strength": "4",
            "confidence": "1.0",
            "summary": "人工确认看空",
            "excluded": "",
            "exclusion_reason": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    detail = client.get(f"/videos/{seeded_video.bvid}").get_data(as_text=True)
    assert "人工确认看空" in detail
    assert "已人工复核" in detail


def test_creator_history_links_each_video_to_its_creator_scoped_comparison_page(
    client, seeded_video
):
    detail = client.get(f"/creators/{seeded_video.creator_uid}").get_data(as_text=True)

    assert (
        f'href="/creators/{seeded_video.creator_uid}/videos/{seeded_video.bvid}"'
        in detail
    )


def test_creator_scoped_video_page_compares_prediction_with_actual_prices(
    client, seeded_video
):
    assert client.post(
        f"/videos/{seeded_video.bvid}",
        data={
            "direction": "bullish",
            "strength": "4",
            "confidence": "0.9",
            "summary": "确认看涨",
            "evidence_json": json.dumps(
                [{"start_sec": 0.0, "end_sec": 3.0, "quote": "黄金可能上涨"}]
            ),
        },
    ).status_code == 303
    response = client.get(
        f"/creators/{seeded_video.creator_uid}/videos/{seeded_video.bvid}"
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "测试UP 的历史合订本" in body
    assert "当时的预测" in body
    assert "实际黄金结果" in body
    assert "看涨" in body
    assert "2402.00" in body
    assert "2404.00" in body
    assert "2408.00" in body
    assert "2423.00" in body


def test_creator_scoped_video_page_rejects_a_video_owned_by_another_creator(
    client, seeded_video
):
    response = client.get(f"/creators/not-the-owner/videos/{seeded_video.bvid}")

    assert response.status_code == 404


@pytest.mark.parametrize(
    "source",
    ["42", "https://space.bilibili.com/42", "BV1TEST", "https://www.bilibili.com/video/BV1TEST"],
)
def test_creator_form_accepts_public_uid_space_and_video_identifiers(client, source):
    response = client.post("/creators", data={"source": source}, follow_redirects=False)
    assert response.status_code == 303


def test_html_controls_change_only_valid_creator_and_job_states(client, database):
    database.upsert_creator(Creator("42", "测试UP", "https://space.bilibili.com/42"))
    job_id = database.create_job("sync_creator", "42")
    assert database.claim_pending_job(job_id)
    assert database.fail_job(job_id, "discovering", "RuntimeError: creator discovery failed")

    assert client.post(f"/jobs/{job_id}/retry", follow_redirects=False).status_code == 303
    assert database.get_job(job_id).status == "pending"
    assert client.post("/creators/42/enabled", data={"enabled": "0"}, follow_redirects=False).status_code == 303
    assert database.get_creator("42").enabled is False
    assert client.post("/creators/42/delete", data={"confirm_uid": "wrong"}).status_code == 400
    assert client.post("/creators/42/delete", data={"confirm_uid": "42"}, follow_redirects=False).status_code == 303
    assert database.get_creator("42") is None


def test_production_post_controls_require_session_csrf(app):
    app.config.update(TESTING=False)
    production_client = app.test_client()
    page = production_client.get("/").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    assert production_client.post("/creators", data={"source": "42"}).status_code == 400
    assert production_client.post(
        "/creators", data={"source": "42", "csrf_token": token}, follow_redirects=False
    ).status_code == 303


def test_manual_review_updates_all_structured_fields_and_audits_revision(client, seeded_video):
    response = client.post(
        f"/videos/{seeded_video.bvid}",
        data={
            "direction": "bearish", "strength": "5", "confidence": "0.95",
            "horizon_text": "两周", "target_price": "2600", "stop_price": "2400",
            "conditions": "突破\n回踩", "summary": "完整人工复核",
            "is_retrospective": "on", "is_news_only": "on",
            "evidence_json": json.dumps([{"start_sec": 0, "end_sec": 3, "quote": "黄金可能上涨"}]),
            "excluded": "", "exclusion_reason": "",
        }, follow_redirects=False,
    )
    assert response.status_code == 303
    db = client.application.config["GOLDBOOK_DB"]
    analysis = db.get_latest_analysis(seeded_video.bvid)
    assert (analysis.direction, analysis.strength, analysis.confidence) == (Direction.BEARISH, 5, 0.95)
    assert analysis.horizon_text == "两周" and analysis.target_price == 2600.0 and analysis.stop_price == 2400.0
    assert analysis.conditions == ("突破", "回踩")
    assert analysis.is_retrospective and analysis.is_news_only
    assert analysis.evidence[0]["quote"] == "黄金可能上涨"
    assert analysis.review_status is ReviewStatus.APPROVED
    assert len(db.list_analysis_revisions(seeded_video.bvid)) == 2


def test_manual_review_rejects_evidence_not_locatable_in_local_transcript(client, seeded_video):
    response = client.post(
        f"/videos/{seeded_video.bvid}",
        data={
            "direction": "bullish", "strength": "4", "confidence": "0.9", "summary": "x",
            "evidence_json": '[{"start_sec": 0, "end_sec": 3, "quote": "不存在"}]',
        },
    )
    assert response.status_code == 400


def test_manual_review_accepts_existing_evidence_spanning_adjacent_segments(client, seeded_video):
    db = client.application.config["GOLDBOOK_DB"]
    db.save_transcript(
        seeded_video.bvid,
        (TranscriptSegment(3.0, 5.0, "仍将走高"),),
        model="test-whisper",
        text_hash="transcript-hash",
    )
    response = client.post(
        f"/videos/{seeded_video.bvid}",
        data={
            "direction": "bullish", "strength": "4", "confidence": "0.9",
            "summary": "保留跨片段证据",
            "evidence_json": json.dumps([
                {"start_sec": 0.0, "end_sec": 5.0, "quote": "黄金可能上涨仍将走高"}
            ]),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert db.get_latest_analysis(seeded_video.bvid).evidence == (
        {"start_sec": 0.0, "end_sec": 5.0, "quote": "黄金可能上涨仍将走高"},
    )


def test_manual_review_rejects_cross_segment_evidence_with_a_time_gap(client, seeded_video):
    db = client.application.config["GOLDBOOK_DB"]
    db.save_transcript(
        seeded_video.bvid,
        (TranscriptSegment(4.0, 5.0, "仍将走高"),),
        model="test-whisper",
        text_hash="transcript-hash",
    )
    response = client.post(
        f"/videos/{seeded_video.bvid}",
        data={
            "direction": "bullish", "strength": "4", "confidence": "0.9", "summary": "x",
            "evidence_json": json.dumps([
                {"start_sec": 0.0, "end_sec": 5.0, "quote": "黄金可能上涨仍将走高"}
            ]),
        },
    )
    assert response.status_code == 400


def _seed_ranked_outcomes(database, uid, name, returns):
    database.upsert_creator(Creator(uid, name, f"https://space.bilibili.com/{uid}"))
    for index, value in enumerate(returns, start=1):
        bvid = f"BV{uid}{index}"
        database.upsert_video(
            Video(
                bvid, uid, f"{name} {index}", datetime(2026, 8, index, tzinfo=timezone.utc),
                60, f"https://www.bilibili.com/video/{bvid}",
            )
        )
        database.save_outcome(
            Outcome(
                Direction.BULLISH, entry_date="2026-08-02", entry_price=100.0, bvid=bvid,
                signal_id=bvid, review_status=ReviewStatus.APPROVED, included=True,
                return_5d=value, mature=True,
            )
        )
