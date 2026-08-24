import json
import html
import re
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from goldbook.config import Settings
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
    IntradayPriceBar,
    PriceBar,
    ReviewStatus,
    SignalAnalysis,
    TranscriptSegment,
    Video,
)
from goldbook.web import (
    _claim_chart,
    _claim_decision_steps,
    _claim_explanation,
    _format_claim_horizon,
    create_app,
)


@pytest.fixture
def claim_web(tmp_path):
    database = Database(tmp_path / "goldbook.db")
    database.initialize()
    creator = Creator("42", "点位UP", "https://space.bilibili.com/42")
    video = Video(
        "BV1WEBCLAIM",
        "42",
        "短期回踩后目标4700",
        datetime(2026, 8, 3, tzinfo=timezone.utc),
        60,
        "https://www.bilibili.com/video/BV1WEBCLAIM",
    )
    database.upsert_creator(creator)
    database.upsert_video(video)
    database.save_transcript(
        video.bvid,
        [TranscriptSegment(0.0, 4.0, "先回踩4650再看4700")],
        model="small",
        text_hash="hash",
    )
    claim = ForecastClaim(
        claim_id="BV1WEBCLAIM:1:0",
        bvid=video.bvid,
        analysis_revision=1,
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
        time_confidence=0.8,
        confidence=0.9,
        evidence=(
            {
                "start_sec": 0.0,
                "end_sec": 4.0,
                "quote": "先回踩4650再看4700",
            },
        ),
        supersedes_claim_id=None,
        status=ClaimStatus.AUTO_VALIDATED,
        model_name="MiniMax-M3",
        prompt_version="claims-v1",
        transcript_hash="hash",
    )
    database.replace_forecast_claims(video.bvid, 1, [claim])
    database.save_analysis(
        SignalAnalysis(
            bvid=video.bvid,
            transcript_hash="hash",
            direction=Direction.BULLISH,
            strength=4,
            confidence=0.9,
            horizon_text="短期",
            target_price=4700.0,
            evidence=claim.evidence,
            summary="先回踩后看4700",
            review_status=ReviewStatus.APPROVED,
            signal_json='{"summary":"先回踩后看4700","claims":[]}',
            revision=1,
            model_name="MiniMax-M3",
            prompt_version="claims-v1",
        )
    )
    database.save_claim_evaluation(
        ClaimEvaluation(
            claim_id=claim.claim_id,
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
    )
    database.replace_prices(
        [
            PriceBar("2026-08-04", 4660, 4690, 4648, 4680),
            PriceBar("2026-08-05", 4680, 4702, 4670, 4698),
            PriceBar("2026-08-06", 4698, 4710, 4685, 4705),
        ]
    )
    settings = Settings.from_env({"GOLDBOOK_DATA_DIR": str(tmp_path)})
    app = create_app(settings, database, None)
    app.config.update(TESTING=True)
    return app.test_client(), database, creator, video


def test_video_page_renders_each_claim_against_actual_result(claim_web):
    client, _database, creator, video = claim_web

    response = client.get(f"/creators/{creator.uid}/videos/{video.bvid}")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "作者原话" in body
    assert "先回踩4650再看4700" in body
    assert "M3 周期换算" in body
    assert "2 个交易日（1–3）" in body
    assert "程序行情验证" in body
    assert "实际最高" in body and "4702" in body
    assert "首次命中" in body and "2026-08-05" in body
    assert "命中" in body


def test_intraday_horizon_is_described_without_zero_trading_days(claim_web):
    _client, database, _creator, video = claim_web
    claim = replace(
        database.list_forecast_claims(video.bvid)[0],
        horizon_text="今天晚上",
        horizon_min_trading_days=0,
        horizon_point_trading_days=0,
        horizon_max_trading_days=1,
    )

    assert _format_claim_horizon(claim) == "日内/次一交易日（需要发布后的匹配行情）"


def test_awaiting_first_bar_explains_why_same_day_price_is_not_used(claim_web):
    _client, database, _creator, video = claim_web
    claim = database.list_forecast_claims(video.bvid)[0]
    prior = database.get_claim_evaluation(claim.claim_id)
    evaluation = replace(
        prior,
        window_start=None,
        window_end=None,
        observed_min=None,
        observed_max=None,
        verdict=EvaluationVerdict.UNRESOLVED,
        mature=False,
        reason="awaiting_first_complete_bar",
    )

    explanation = _claim_explanation(claim, evaluation)

    assert "并非没有黄金价格" in explanation
    assert "发布后的首根完整日线" in explanation


def test_creator_page_links_video_and_discloses_claim_coverage(claim_web):
    client, _database, creator, video = claim_web

    body = client.get(f"/creators/{creator.uid}").get_data(as_text=True)

    assert f"/creators/{creator.uid}/videos/{video.bvid}" in body
    assert "观点 1" in body
    assert "覆盖率 100.00%" in body
    assert "命中 1" in body
    assert f"/creators/{creator.uid}/claims?verdict=hit" in body


def test_creator_page_separates_ability_and_shows_signal_account(claim_web):
    client, _database, creator, _video = claim_web

    body = client.get(f"/creators/{creator.uid}").get_data(as_text=True)

    assert "方向能力" in body
    assert "点位能力" in body
    assert "100 美元信号账户" in body
    assert "最终余额" in body
    assert "最大回撤" in body
    assert "不计手续费、滑点" in body
    assert "当日收盘全部平仓" in body
    assert "无明确方向则全天现金" in body
    assert "无信号，空仓现金" in body
    assert "策略默认做空" not in body


def test_creator_chart_contains_equity_gold_and_position_backgrounds(claim_web):
    client, _database, creator, _video = claim_web

    body = client.get(f"/creators/{creator.uid}").get_data(as_text=True)
    match = re.search(r'id="backtest-chart" data-chart=\'([^\']+)\'', body)
    assert match is not None
    payload = json.loads(html.unescape(match.group(1)))

    assert payload["equity"]
    assert payload["gold"]
    assert {row["kind"] for row in payload["positions"]} == {"long", "cash"}
    assert payload["axes"] == {
        "balance": "账户余额（美元）",
        "gold": "XAU/USD（美元/盎司）",
    }


def test_signal_chart_uses_a_narrow_status_band_instead_of_full_height_hatching():
    script = (
        Path(__file__).resolve().parents[1] / "goldbook" / "static" / "app.js"
    ).read_text(encoding="utf-8")
    stylesheet = (
        Path(__file__).resolve().parents[1] / "goldbook" / "static" / "app.css"
    ).read_text(encoding="utf-8")

    assert "positionStatusBand" in script
    assert "const bandHeight = 9" in script
    assert "ctx.fillRect(left, chartArea.top, right - left, chartArea.height)" not in script
    assert "ctx.lineTo(x + chartArea.height, chartArea.top)" not in script
    assert ".legend-cash { --legend-color: #626c78; }" in stylesheet


def _install_primary_trend(database, video):
    original = database.list_forecast_claims(video.bvid)[0]
    original_evaluation = database.get_claim_evaluation(original.claim_id)
    trend = replace(
        original,
        claim_id=f"{video.bvid}:1:0",
        claim_index=0,
        claim_type=ClaimType.DIRECTIONAL_MOVE,
        legs=(),
        condition_text="短期回调后继续看涨",
    )
    point = replace(
        original,
        claim_id=f"{video.bvid}:1:1",
        claim_index=1,
    )
    database.replace_forecast_claims(video.bvid, 1, [trend, point])
    database.save_claim_evaluation(
        replace(
            original_evaluation,
            claim_id=trend.claim_id,
            entry_price=4660.0,
            final_close=4698.0,
            verdict=EvaluationVerdict.HIT,
            reason="direction matched",
        )
    )
    database.save_claim_evaluation(
        replace(original_evaluation, claim_id=point.claim_id)
    )
    return trend, point


def test_creator_discloses_primary_trend_coverage(claim_web):
    client, database, creator, video = claim_web
    _install_primary_trend(database, video)

    body = client.get(f"/creators/{creator.uid}").get_data(as_text=True)

    assert "趋势覆盖" in body
    assert "1 / 1 个视频" in body


def test_results_filter_trend_separately_from_price_levels(claim_web):
    client, database, creator, video = claim_web
    _install_primary_trend(database, video)

    trend = client.get(
        f"/creators/{creator.uid}/claims?kind=trend"
    ).get_data(as_text=True)
    levels = client.get(
        f"/creators/{creator.uid}/claims?kind=price_level"
    ).get_data(as_text=True)

    assert "<span>主要趋势</span>" in trend
    assert "先回踩4650再看4700" not in trend
    assert "<span>目标点位</span>" in levels
    assert "短期回调后继续看涨" not in levels


def test_trend_decision_chain_shows_literal_numeric_rule(claim_web):
    client, database, creator, video = claim_web
    _install_primary_trend(database, video)

    body = client.get(
        f"/creators/{creator.uid}/claims?kind=trend&verdict=hit"
    ).get_data(as_text=True)

    assert "首日开盘 4660.00" in body
    assert "截止收盘 4698.00" in body
    assert "实际变化 +0.82%" in body
    assert "要求看涨（变化 &gt; 0）" in body


def test_creator_claim_results_page_filters_and_explains_the_verdict(claim_web):
    client, _database, creator, video = claim_web

    response = client.get(f"/creators/{creator.uid}/claims?verdict=hit")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "命中观点" in body
    assert "先回踩4650再看4700" in body
    assert "2026-08-05 首次完成全部条件" in body
    assert "窗口内最低 4648.00，最高 4702.00" in body
    assert f"/creators/{creator.uid}/videos/{video.bvid}" in body
    assert "预测要求" in body
    assert "使用行情" in body
    assert "阈值比较" in body
    assert "最终结论" in body
    assert "实际最高 4702.00" in body


def test_creator_claim_results_rejects_an_unknown_verdict(claim_web):
    client, _database, creator, _video = claim_web

    response = client.get(f"/creators/{creator.uid}/claims?verdict=anything")

    assert response.status_code == 400


def test_claim_chart_defaults_to_prediction_window_and_names_price_axis(claim_web):
    _client, database, _creator, video = claim_web
    database.replace_prices(
        [
            PriceBar("2026-07-01", 4400, 4410, 4390, 4405),
            PriceBar("2026-08-04", 4660, 4690, 4648, 4680),
            PriceBar("2026-08-05", 4680, 4702, 4670, 4698),
            PriceBar("2026-08-06", 4698, 4710, 4685, 4705),
            PriceBar("2026-08-20", 4800, 4810, 4790, 4805),
        ]
    )
    claim = database.list_forecast_claims(video.bvid)[0]
    rows = [{"claim": claim, "evaluation": database.get_claim_evaluation(claim.claim_id)}]

    chart = _claim_chart(database, video, rows)

    assert chart["axis"] == {"x_title": "交易日", "y_title": "XAU/USD（美元/盎司）"}
    assert [row["date"] for row in chart["prices"]] == [
        "2026-08-04",
        "2026-08-05",
        "2026-08-06",
    ]
    assert len(chart["all_prices"]) == 5
    assert chart["focus"]["start"] <= "2026-08-04"
    assert chart["focus"]["end"] >= "2026-08-06"


def test_video_page_explains_intraday_window_and_chart_excludes_prepublication_hour(
    claim_web,
):
    client, database, creator, original_video = claim_web
    video = replace(
        original_video,
        published_at=datetime(2026, 8, 12, 10, 35, tzinfo=timezone.utc),
    )
    database.upsert_video(video)
    original_claim = database.list_forecast_claims(video.bvid)[0]
    claim = replace(
        original_claim,
        claim_type=ClaimType.TARGET_TOUCH,
        legs=(ClaimLeg(">=", 4450.0, None),),
        condition_text="今晚看4450",
        horizon_text="未来3小时",
        horizon_min_trading_days=0,
        horizon_point_trading_days=0,
        horizon_max_trading_days=0,
    )
    database.replace_forecast_claims(video.bvid, 1, (claim,))
    database.save_claim_evaluation(
        ClaimEvaluation(
            claim_id=claim.claim_id,
            evaluated_at=datetime(2026, 8, 12, 14, tzinfo=timezone.utc),
            window_start=None,
            window_end=None,
            entry_price=4400,
            observed_min=4390,
            observed_max=4451,
            final_close=4448,
            closest_price=4451,
            closest_date=None,
            distance_pct=0,
            first_hit_date=None,
            verdict=EvaluationVerdict.HIT,
            mature=True,
            reason="condition satisfied",
            window_start_at=datetime(2026, 8, 12, 11, tzinfo=timezone.utc),
            window_end_at=datetime(2026, 8, 12, 13, 35, tzinfo=timezone.utc),
            closest_at=datetime(2026, 8, 12, 12, tzinfo=timezone.utc),
            first_hit_at=datetime(2026, 8, 12, 12, tzinfo=timezone.utc),
        )
    )
    database.upsert_intraday_prices(
        (
            IntradayPriceBar(
                datetime(2026, 8, 12, 10, tzinfo=timezone.utc),
                60,
                4440,
                4500,
                4430,
                4490,
                "XAUS",
            ),
            IntradayPriceBar(
                datetime(2026, 8, 12, 11, tzinfo=timezone.utc),
                60,
                4400,
                4430,
                4390,
                4420,
                "XAUS",
            ),
            IntradayPriceBar(
                datetime(2026, 8, 12, 12, tzinfo=timezone.utc),
                60,
                4420,
                4451,
                4410,
                4448,
                "XAUS",
            ),
        )
    )

    body = client.get(
        f"/creators/{creator.uid}/videos/{video.bvid}"
    ).get_data(as_text=True)

    assert "视频发布：2026-08-12 18:35" in body
    assert "首根完整小时线：2026-08-12 19:00" in body
    assert "首次命中：2026-08-12 20:00" in body
    match = re.search(r'id="claim-price-chart" data-chart=\'([^\']+)\'', body)
    assert match is not None
    payload = json.loads(html.unescape(match.group(1)))
    assert payload["granularity"] == "1h"
    assert payload["prices"][0]["at"] == "2026-08-12T11:00:00+00:00"
    assert all(
        point["at"] != "2026-08-12T10:00:00+00:00"
        for point in payload["prices"]
    )
    assert {marker["kind"] for marker in payload["markers"]} >= {
        "publication",
        "entry",
        "hit",
        "deadline",
    }


def test_event_activated_decision_chain_names_release_time(claim_web):
    _client, database, _creator, original_video = claim_web
    video = replace(
        original_video,
        published_at=datetime(2026, 8, 11, 11, 24, 4, tzinfo=timezone.utc),
    )
    claim = replace(
        database.list_forecast_claims(video.bvid)[0],
        claim_type=ClaimType.CROSS_ABOVE,
        legs=(ClaimLeg(">=", 4450.0, None),),
        condition_text="CPI数据公布后黄金突破4450",
        horizon_text="明天的CPI数据",
        horizon_min_trading_days=1,
        horizon_point_trading_days=1,
        horizon_max_trading_days=1,
        deadline_at=datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc),
    )
    evaluation = replace(
        database.get_claim_evaluation(claim.claim_id),
        window_start=None,
        window_end=None,
        window_start_at=datetime(2026, 8, 12, 13, tzinfo=timezone.utc),
        window_end_at=datetime(2026, 8, 12, 16, tzinfo=timezone.utc),
        first_hit_date=None,
        first_hit_at=datetime(2026, 8, 12, 13, tzinfo=timezone.utc),
        observed_min=4468.0,
        observed_max=4502.7,
        final_close=4480.4,
    )

    steps = _claim_decision_steps(claim, evaluation, video)

    assert "事件生效：2026-08-12 20:30" in steps["market_data"]
    assert "跨越事件时刻的小时线已排除" in steps["market_data"]
    assert "首根完整小时线：2026-08-12 21:00" in steps["market_data"]


def test_leaderboard_uses_video_equal_claim_metrics_when_claims_exist(claim_web):
    client, database, creator, first_video = claim_web
    first_claim = database.list_forecast_claims(first_video.bvid)[0]
    first_evaluation = database.get_claim_evaluation(first_claim.claim_id)
    for index in (2, 3):
        bvid = f"BV1WEBCLAIM{index}"
        video = replace(
            first_video,
            bvid=bvid,
            title=f"黄金目标 {index}",
            published_at=first_video.published_at + timedelta(days=index),
            url=f"https://www.bilibili.com/video/{bvid}",
        )
        claim = replace(
            first_claim,
            claim_id=f"{bvid}:1:0",
            bvid=bvid,
            claim_type=ClaimType.TARGET_TOUCH,
            legs=(ClaimLeg(">=", 4700.0, None),),
        )
        database.upsert_video(video)
        database.replace_forecast_claims(bvid, 1, [claim])
        database.save_claim_evaluation(
            replace(first_evaluation, claim_id=claim.claim_id)
        )

    body = client.get("/leaderboard").get_data(as_text=True)

    assert "逐条观点能力排行" in body
    assert creator.name in body
    assert "视频等权得分" in body
    assert "覆盖率" in body


def test_optional_claim_correction_creates_audited_revision_and_recomputes(claim_web):
    client, database, creator, video = claim_web
    claim = database.list_forecast_claims(video.bvid)[0]
    evidence_json = json.dumps(claim.evidence, ensure_ascii=False)

    response = client.post(
        f"/creators/{creator.uid}/videos/{video.bvid}/claims/{claim.claim_id}/correct",
        data={
            "claim_type": "target_touch",
            "instrument": "xau_usd_spot",
            "direction": "bullish",
            "operator": ">=",
            "level_low": "4710",
            "level_high": "",
            "condition_text": "目标调整为4710",
            "horizon_text": "短期",
            "horizon_source": "context_inferred",
            "horizon_min": "1",
            "horizon_point": "2",
            "horizon_max": "3",
            "time_confidence": "0.8",
            "confidence": "0.9",
            "evidence_json": evidence_json,
        },
    )

    assert response.status_code == 303
    latest = database.list_forecast_claims(video.bvid)[0]
    assert latest.analysis_revision == 2
    assert latest.status is ClaimStatus.HUMAN_CORRECTED
    assert latest.legs[0].level_low == 4710.0
    assert latest.supersedes_claim_id == claim.claim_id
    assert len(database.list_forecast_claims(video.bvid, latest_only=False)) == 2
    assert database.get_claim_evaluation(latest.claim_id) is not None


def test_sequence_correction_accepts_explicit_ordered_legs_json(claim_web):
    client, database, creator, video = claim_web
    claim = database.list_forecast_claims(video.bvid)[0]

    response = client.post(
        f"/creators/{creator.uid}/videos/{video.bvid}/claims/{claim.claim_id}/correct",
        data={
            "claim_type": "sequence",
            "instrument": "xau_usd_spot",
            "direction": "bullish",
            "condition_text": "先回踩4640再看4720",
            "horizon_text": "中期",
            "horizon_source": "context_inferred",
            "horizon_min": "3",
            "horizon_point": "5",
            "horizon_max": "8",
            "time_confidence": "0.75",
            "confidence": "0.88",
            "legs_json": json.dumps(
                [
                    {"operator": "<=", "level_low": 4640, "level_high": None},
                    {"operator": ">=", "level_low": 4720, "level_high": None},
                ]
            ),
            "evidence_json": json.dumps(claim.evidence, ensure_ascii=False),
        },
    )

    assert response.status_code == 303
    latest = database.list_forecast_claims(video.bvid)[0]
    assert [leg.level_low for leg in latest.legs] == [4640.0, 4720.0]
