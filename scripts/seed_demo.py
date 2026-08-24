"""Deterministic, offline-only sample data for the local dashboard."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from goldbook.claim_evaluation import recompute_claim_evaluations
from goldbook.db import Database
from goldbook.models import (
    ClaimLeg,
    ClaimStatus,
    ClaimType,
    Creator,
    Direction,
    ForecastClaim,
    HorizonSource,
    Instrument,
    PriceBar,
    ReviewStatus,
    SignalAnalysis,
    TranscriptSegment,
    Video,
)
from goldbook.scoring import score_signal


_CREATORS = (
    Creator("demo-aurora", "曙光金研社（演示）", "https://space.bilibili.com/demo-aurora"),
    Creator("demo-compass", "罗盘观察室（演示）", "https://space.bilibili.com/demo-compass"),
)


def seed_demo(db: Database) -> None:
    """Seed fictional records without network, media downloads, or model calls."""
    bars = _demo_bars()
    db.replace_prices(bars)
    for creator in _CREATORS:
        db.upsert_creator(creator)
    for video, analysis in _demo_records():
        db.upsert_video(video)
        transcript_hash = f"demo-transcript-{video.bvid.lower()}"
        db.save_transcript(
            video.bvid,
            (TranscriptSegment(0.0, 4.0, f"这是 {video.title} 的虚构离线演示内容。"),),
            model="demo-offline",
            text_hash=transcript_hash,
        )
        db.save_analysis(analysis, bvid=video.bvid, transcript_hash=transcript_hash)
        outcome = score_signal(analysis, video.published_at, bars)
        if outcome.entry_date is not None and outcome.entry_price is not None:
            db.save_outcome(outcome)
    for claim in _demo_claims():
        db.replace_forecast_claims(claim.bvid, claim.analysis_revision, [claim])
    recompute_claim_evaluations(
        db, evaluated_at=datetime(2026, 2, 5, tzinfo=timezone.utc)
    )


def _demo_bars() -> tuple[PriceBar, ...]:
    start = date(2026, 1, 2)
    return tuple(
        PriceBar(
            start + timedelta(days=index),
            2000.0 + index * 4.0,
            2005.0 + index * 4.0,
            1995.0 + index * 4.0,
            2003.0 + index * 4.0,
        )
        for index in range(30)
    )


def _demo_records() -> tuple[tuple[Video, SignalAnalysis], ...]:
    return (
        _record("BVDEMOA1", "demo-aurora", "演示：趋势向上", date(2025, 12, 31), Direction.BULLISH, ReviewStatus.APPROVED),
        _record("BVDEMOA2", "demo-aurora", "演示：逆势看空", date(2026, 1, 2), Direction.BEARISH, ReviewStatus.APPROVED),
        _record("BVDEMOA3", "demo-aurora", "演示：回调后看多", date(2026, 1, 4), Direction.BULLISH, ReviewStatus.APPROVED),
        _record("BVDEMOB1", "demo-compass", "演示：谨慎看多", date(2026, 1, 6), Direction.BULLISH, ReviewStatus.APPROVED),
        _record("BVDEMOB2", "demo-compass", "演示：无明确方向", date(2026, 1, 7), Direction.NO_SIGNAL, ReviewStatus.APPROVED),
        _record("BVDEMOB3", "demo-compass", "演示：人工排除样本", date(2026, 1, 8), Direction.BEARISH, ReviewStatus.EXCLUDED),
    )


def _record(
    bvid: str,
    creator_uid: str,
    title: str,
    published_date: date,
    direction: Direction,
    review_status: ReviewStatus,
) -> tuple[Video, SignalAnalysis]:
    published_at = datetime.combine(published_date, datetime.min.time(), tzinfo=timezone.utc)
    video = Video(
        bvid=bvid,
        creator_uid=creator_uid,
        title=title,
        published_at=published_at,
        duration_sec=240,
        url=f"https://www.bilibili.com/video/{bvid}",
    )
    analysis = SignalAnalysis(
        direction=direction,
        strength=3,
        confidence=0.88,
        evidence=(
            {"start_sec": 0.0, "end_sec": 4.0, "quote": f"{title}：虚构研究结论"},
        ),
        summary=f"虚构离线演示：{title}。仅用于展示本地研究工作流。",
        review_status=review_status,
        bvid=bvid,
        created_at=published_at,
    )
    return video, analysis


def _demo_claims() -> tuple[ForecastClaim, ...]:
    return (
        _demo_claim("BVDEMOA1", 2008.0, Instrument.XAU_USD_SPOT),
        _demo_claim("BVDEMOA2", 2020.0, Instrument.XAU_USD_SPOT),
        _demo_claim("BVDEMOA3", 2100.0, Instrument.XAU_USD_SPOT),
        _demo_claim("BVDEMOB1", 2050.0, Instrument.COMEX_GC),
    )


def _demo_claim(
    bvid: str, target: float, instrument: Instrument
) -> ForecastClaim:
    return ForecastClaim(
        claim_id=f"{bvid}:0:0",
        bvid=bvid,
        analysis_revision=0,
        claim_index=0,
        instrument=instrument,
        claim_type=ClaimType.TARGET_TOUCH,
        direction=Direction.BULLISH,
        legs=(ClaimLeg(">=", target, None),),
        condition_text=f"虚构演示目标 {target:.0f}",
        horizon_text="短期",
        horizon_source=HorizonSource.CONTEXT_INFERRED,
        horizon_min_trading_days=1,
        horizon_max_trading_days=2,
        horizon_point_trading_days=2,
        deadline_at=None,
        time_confidence=0.9,
        confidence=0.9,
        evidence=(
            {
                "start_sec": 0.0,
                "end_sec": 4.0,
                "quote": f"虚构演示目标 {target:.0f}",
            },
        ),
        supersedes_claim_id=None,
        status=ClaimStatus.AUTO_VALIDATED,
        model_name="demo-offline",
        prompt_version="claims-v1",
        transcript_hash=f"demo-transcript-{bvid.lower()}",
    )
