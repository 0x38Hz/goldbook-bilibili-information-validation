"""Cached-transcript claim backfill without media or transcription work."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from goldbook.claim_evaluation import recompute_claim_evaluations
from goldbook.db import Database
from goldbook.minimax import (
    CLAIM_PROMPT_VERSION,
    ClaimExtraction,
    ClaimExtractionFailure,
    MiniMaxClient,
    evidence_is_locatable,
    run_claim_extraction_batch,
)
from goldbook.models import (
    ClaimStatus,
    ClaimType,
    Direction,
    ForecastClaim,
    HorizonSource,
    Instrument,
    ReviewStatus,
    SignalAnalysis,
    VideoStatus,
)


_DIRECTION_KEYWORDS = {
    Direction.BULLISH: (
        "看涨", "上涨", "做多", "买入", "低多", "偏多", "走高", "上攻", "强势",
        "黄金会受到支撑", "金价会受到支撑",
    ),
    Direction.BEARISH: (
        "看跌", "下跌", "做空", "卖出", "高空", "偏空", "走低", "下探", "回落", "弱势",
    ),
    Direction.NEUTRAL: (
        "横盘", "震荡", "盘整", "方向不明", "等待突破", "选择观望",
    ),
}


@dataclass(frozen=True)
class ClaimBackfillSummary:
    total: int
    completed: int
    skipped: int
    failed: int
    primary_trends: int = 0
    bullish: int = 0
    bearish: int = 0
    neutral: int = 0
    no_signal: int = 0


def reanalyse_cached_claims(
    database: Database,
    client: MiniMaxClient,
    *,
    evaluated_at: datetime,
    on_progress: Callable[[int, int, str, str], None] | None = None,
) -> ClaimBackfillSummary:
    """Reanalyse cached transcripts and explicitly mark completed videos without text."""
    candidates = []
    no_transcript = []
    legacy_by_bvid: dict[str, SignalAnalysis | None] = {}
    skipped = 0
    total = 0
    for video, legacy_analysis in database.list_videos_with_latest_analysis():
        segments = tuple(database.list_transcript_segments(video.bvid))
        identity = database.get_transcript_identity(video.bvid)
        if not segments or identity is None:
            if video.status is not VideoStatus.COMPLETE:
                continue
            total += 1
            existing = database.list_forecast_claims(video.bvid)
            if any(
                claim.claim_index == 0
                and claim.claim_type is ClaimType.DIRECTIONAL_MOVE
                for claim in existing
            ):
                skipped += 1
                continue
            no_transcript.append(
                (
                    video,
                    database.next_analysis_revision(video.bvid),
                    "no-transcript",
                )
            )
            continue
        total += 1
        transcript_hash = identity[1]
        if database.has_claim_extraction(
            video.bvid,
            transcript_hash,
            "MiniMax-M3",
            CLAIM_PROMPT_VERSION,
        ):
            skipped += 1
            continue
        candidates.append(
            (
                video,
                segments,
                database.next_analysis_revision(video.bvid),
                transcript_hash,
            )
        )
        legacy_by_bvid[video.bvid] = legacy_analysis

    completed = 0
    failed = 0
    primary_trends = 0
    direction_counts = {direction: 0 for direction in Direction}
    processed = 0
    work_total = len(no_transcript) + len(candidates)
    for video, revision, transcript_hash in no_transcript:
        extraction = _no_transcript_extraction(video.bvid, revision, transcript_hash)
        analysis = _summary_analysis(
            video.bvid,
            revision,
            transcript_hash,
            extraction,
            evaluated_at,
        )
        database.save_claim_extraction(analysis, extraction.claims)
        completed += 1
        primary_trends += 1
        direction_counts[Direction.NO_SIGNAL] += 1
        processed += 1
        if on_progress is not None:
            on_progress(
                processed,
                work_total,
                video.bvid,
                "completed (no local transcript; unresolved no_signal)",
            )
    batch_size = client.max_concurrency
    for batch_start in range(0, len(candidates), batch_size):
        batch = candidates[batch_start : batch_start + batch_size]
        results = run_claim_extraction_batch(client, batch)
        for video, segments, revision, transcript_hash in batch:
            result = results[video.bvid]
            used_fallback = False
            if isinstance(result, ClaimExtractionFailure):
                fallback = (
                    _legacy_primary_fallback(
                        video.bvid,
                        revision,
                        transcript_hash,
                        legacy_by_bvid.get(video.bvid),
                        segments,
                    )
                    if result.reason.startswith("invalid structured response:")
                    else None
                )
                if fallback is None:
                    failed += 1
                    status = f"failed ({result.reason})"
                else:
                    result = fallback
                    used_fallback = True
            if isinstance(result, ClaimExtraction):
                analysis = _summary_analysis(
                    video.bvid,
                    revision,
                    transcript_hash,
                    result,
                    evaluated_at,
                )
                database.save_claim_extraction(analysis, result.claims)
                completed += 1
                if result.claims:
                    primary_trends += 1
                    primary_direction = result.claims[0].direction or Direction.NO_SIGNAL
                    direction_counts[primary_direction] += 1
                status = (
                    "completed (grounded legacy trend fallback)"
                    if used_fallback
                    else "completed"
                    if result.rejected_count == 0
                    else f"completed ({len(result.claims)} accepted, "
                    f"{result.rejected_count} rejected)"
                )
            processed += 1
            if on_progress is not None:
                on_progress(processed, work_total, video.bvid, status)

    recompute_claim_evaluations(database, evaluated_at=evaluated_at)
    return ClaimBackfillSummary(
        total=total,
        completed=completed,
        skipped=skipped,
        failed=failed,
        primary_trends=primary_trends,
        bullish=direction_counts[Direction.BULLISH],
        bearish=direction_counts[Direction.BEARISH],
        neutral=direction_counts[Direction.NEUTRAL],
        no_signal=direction_counts[Direction.NO_SIGNAL],
    )


def _no_transcript_extraction(
    bvid: str, revision: int, transcript_hash: str
) -> ClaimExtraction:
    condition_text = "本地无可用字幕，无法提取趋势"
    claim = ForecastClaim(
        claim_id=f"{bvid}:{revision}:0",
        bvid=bvid,
        analysis_revision=revision,
        claim_index=0,
        instrument=Instrument.XAU_USD_SPOT,
        claim_type=ClaimType.DIRECTIONAL_MOVE,
        direction=Direction.NO_SIGNAL,
        legs=(),
        condition_text=condition_text,
        horizon_text=None,
        horizon_source=HorizonSource.UNKNOWN,
        horizon_min_trading_days=None,
        horizon_max_trading_days=None,
        horizon_point_trading_days=None,
        deadline_at=None,
        time_confidence=0.0,
        confidence=0.0,
        evidence=(),
        supersedes_claim_id=None,
        status=ClaimStatus.UNRESOLVED,
        model_name="Goldbook-local",
        prompt_version=CLAIM_PROMPT_VERSION,
        transcript_hash=transcript_hash,
    )
    return ClaimExtraction(summary=condition_text, claims=(claim,))


def _legacy_primary_fallback(
    bvid: str,
    revision: int,
    transcript_hash: str,
    legacy: SignalAnalysis | None,
    segments,
) -> ClaimExtraction | None:
    if legacy is None or legacy.review_status is ReviewStatus.EXCLUDED:
        return None
    locatable = tuple(
        evidence
        for evidence in legacy.evidence
        if evidence_is_locatable(
            str(evidence.get("quote", "")),
            float(evidence.get("start_sec", -1)),
            float(evidence.get("end_sec", -1)),
            segments,
        )
    )
    if not locatable:
        locatable = _matching_direction_evidence(legacy.direction, segments)
    if legacy.direction is not Direction.NO_SIGNAL and not locatable:
        return None
    condition_text = (
        "；".join(str(evidence["quote"]) for evidence in locatable)
        if locatable
        else legacy.summary
    )
    claim = ForecastClaim(
        claim_id=f"{bvid}:{revision}:0",
        bvid=bvid,
        analysis_revision=revision,
        claim_index=0,
        instrument=Instrument.XAU_USD_SPOT,
        claim_type=ClaimType.DIRECTIONAL_MOVE,
        direction=legacy.direction,
        legs=(),
        condition_text=condition_text,
        horizon_text=legacy.horizon_text,
        horizon_source=HorizonSource.UNKNOWN,
        horizon_min_trading_days=None,
        horizon_max_trading_days=None,
        horizon_point_trading_days=None,
        deadline_at=None,
        time_confidence=0.5 if legacy.horizon_text else 0.0,
        confidence=legacy.confidence,
        evidence=locatable,
        supersedes_claim_id=None,
        status=(
            ClaimStatus.UNRESOLVED
            if legacy.direction is Direction.NO_SIGNAL
            else ClaimStatus.AUTO_VALIDATED
        ),
        model_name=legacy.model_name or "MiniMax-M3",
        prompt_version=CLAIM_PROMPT_VERSION,
        transcript_hash=transcript_hash,
    )
    return ClaimExtraction(summary=legacy.summary, claims=(claim,))


def _matching_direction_evidence(direction: Direction, segments) -> tuple[dict[str, object], ...]:
    keywords = _DIRECTION_KEYWORDS.get(direction, ())
    for segment in segments:
        if any(keyword in segment.text for keyword in keywords):
            return (
                {
                    "start_sec": segment.start_sec,
                    "end_sec": segment.end_sec,
                    "quote": segment.text,
                },
            )
    return ()


def _summary_analysis(
    bvid: str,
    revision: int,
    transcript_hash: str,
    extraction: ClaimExtraction,
    created_at: datetime,
) -> SignalAnalysis:
    direction = (
        extraction.claims[0].direction
        if extraction.claims and extraction.claims[0].direction is not None
        else Direction.NO_SIGNAL
    )
    confidence = (
        sum(claim.confidence for claim in extraction.claims) / len(extraction.claims)
        if extraction.claims
        else 1.0
    )
    target_prices = [
        leg.level_low
        for claim in extraction.claims
        for leg in claim.legs
        if leg.level_low is not None
    ]
    horizon_texts = tuple(
        dict.fromkeys(
            claim.horizon_text
            for claim in extraction.claims
            if claim.horizon_text is not None
        )
    )
    return SignalAnalysis(
        bvid=bvid,
        transcript_hash=transcript_hash,
        direction=direction,
        strength=max(1, min(5, round(confidence * 5))),
        confidence=confidence,
        horizon_text=" / ".join(horizon_texts) or None,
        target_price=target_prices[0] if len(target_prices) == 1 else None,
        conditions=tuple(claim.condition_text for claim in extraction.claims),
        evidence=tuple(
            evidence
            for claim in extraction.claims
            for evidence in claim.evidence
        ),
        summary=extraction.summary,
        review_status=ReviewStatus.APPROVED,
        signal_json=_extraction_json(extraction),
        revision=revision,
        created_at=created_at,
        model_name=(
            extraction.claims[0].model_name
            if extraction.claims
            else "MiniMax-M3"
        ),
        prompt_version=CLAIM_PROMPT_VERSION,
    )


def _extraction_json(extraction: ClaimExtraction) -> str:
    return json.dumps(
        {
            "summary": extraction.summary,
            "claims": [_claim_json(claim) for claim in extraction.claims],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _claim_json(claim: ForecastClaim) -> dict[str, object]:
    return {
        "claim_id": claim.claim_id,
        "instrument": claim.instrument.value,
        "claim_type": claim.claim_type.value,
        "direction": None if claim.direction is None else claim.direction.value,
        "legs": [
            {
                "operator": leg.operator,
                "level_low": leg.level_low,
                "level_high": leg.level_high,
            }
            for leg in claim.legs
        ],
        "condition_text": claim.condition_text,
        "horizon_text": claim.horizon_text,
        "horizon_source": claim.horizon_source.value,
        "horizon_min_trading_days": claim.horizon_min_trading_days,
        "horizon_max_trading_days": claim.horizon_max_trading_days,
        "horizon_point_trading_days": claim.horizon_point_trading_days,
        "deadline_at": (
            None if claim.deadline_at is None else claim.deadline_at.isoformat()
        ),
        "time_confidence": claim.time_confidence,
        "confidence": claim.confidence,
        "evidence": claim.evidence,
        "status": claim.status.value,
    }
