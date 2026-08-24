"""Local-only Flask views for reviewing cached research data."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import math
import secrets
from typing import Any

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for

from goldbook.backtest import backtest_creator
from goldbook.bilibili import parse_public_source
from goldbook.claim_evaluation import recompute_claim_evaluations
from goldbook.claim_metrics import aggregate_creator_claims, aggregate_video_claims
from goldbook.claim_time import is_event_activated_claim, is_primary_trend
from goldbook.config import Settings
from goldbook.db import Database
from goldbook.fact_check import detect_fact_check_need
from goldbook.minimax import evidence_is_locatable
from goldbook.models import (
    ClaimLeg,
    ClaimStatus,
    ClaimType,
    Direction,
    ForecastClaim,
    HorizonSource,
    Instrument,
    PriceBar,
    ReviewStatus,
)
from goldbook.scoring import aggregate_creator, score_signal


_SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


def create_app(settings: Settings, db: Database, pipeline: Any) -> Flask:
    """Build the local dashboard without exposing configuration secrets."""
    app = Flask(__name__)
    app.secret_key = secrets.token_urlsafe(32)
    app.config["GOLDBOOK_SETTINGS"] = settings
    app.config["GOLDBOOK_DB"] = db
    app.config["GOLDBOOK_PIPELINE"] = pipeline

    @app.before_request
    def _csrf_protection() -> Any:
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(24)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not app.config.get("TESTING"):
            supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
            if not supplied or not secrets.compare_digest(supplied, session["csrf_token"]):
                if request.path.startswith("/api/"):
                    return _error("csrf_failed", "请求校验失败。", 400)
                abort(400)
        return None

    @app.context_processor
    def _template_context() -> dict[str, object]:
        return {
            "csrf_token": session.get("csrf_token", ""),
            "format_percent": _format_percent,
            "format_claim_horizon": _format_claim_horizon,
            "format_shanghai": _format_shanghai,
            "verdict_label": _verdict_label,
        }

    @app.after_request
    def _baseline_security_headers(response: Any) -> Any:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; form-action 'self'; "
            "frame-ancestors 'self'; object-src 'none'",
        )
        return response

    @app.get("/")
    def index() -> str:
        return render_template("index.html", creators=_creator_rows(db), jobs=db.list_jobs())

    @app.get("/api/status")
    def api_status() -> Any:
        return _ok(
            {
                "version": "local-research",
                "database_ready": db.path.exists(),
                "model": settings.minimax_model,
                "key_configured": bool(settings.minimax_api_key),
            }
        )

    @app.get("/api/jobs")
    def api_jobs() -> Any:
        return _ok(
            [
                {
                    "id": job.id,
                    "kind": job.kind,
                    "creator_uid": job.creator_uid,
                    "video_bvid": job.video_bvid,
                    "status": job.status,
                    "stage": job.stage,
                    "progress": job.progress,
                    "retries": job.retries,
                    "error": job.error,
                }
                for job in db.list_jobs()
            ]
        )

    @app.post("/api/creators")
    def create_creator() -> Any:
        payload = request.get_json(silent=True) or {}
        source = payload.get("source")
        try:
            parsed, job = _enqueue_creator_sync(pipeline, source)
        except ValueError:
            return _error("invalid_source", "只支持公开的 B 站 UP 主页地址。", 400)
        except RuntimeError:
            return _error("queue_unavailable", "后台任务尚未就绪。", 503)
        return _ok({"creator_uid": parsed.value, "job_id": job.id}, 202)

    @app.post("/creators")
    def create_creator_form() -> Any:
        try:
            _enqueue_creator_sync(pipeline, request.form.get("source"))
        except (ValueError, RuntimeError):
            abort(400)
        return redirect(url_for("index"), code=303)

    @app.post("/api/creators/<uid>/sync")
    def sync_creator(uid: str) -> Any:
        creator = db.get_creator(uid)
        if creator is None:
            return _error("not_found", "未找到该 UP 主。", 404)
        if pipeline is None or not hasattr(pipeline, "enqueue_creator_sync"):
            return _error("queue_unavailable", "后台任务尚未就绪。", 503)
        job = pipeline.enqueue_creator_sync(creator.space_url, datetime.now(timezone.utc))
        return _ok({"creator_uid": uid, "job_id": job.id}, 202)

    @app.post("/creators/<uid>/sync")
    def sync_creator_form(uid: str) -> Any:
        creator = db.get_creator(uid)
        if creator is None or pipeline is None or not hasattr(pipeline, "enqueue_creator_sync"):
            abort(404)
        pipeline.enqueue_creator_sync(creator.space_url, datetime.now(timezone.utc))
        return redirect(url_for("index"), code=303)

    @app.post("/jobs/<int:job_id>/<action>")
    def control_job_form(job_id: int, action: str) -> Any:
        handlers = {
            "retry": lambda: _retry_job(pipeline, db, job_id),
            "pause": lambda: db.pause_job(job_id),
            "cancel": lambda: db.cancel_job(job_id),
        }
        handler = handlers.get(action)
        if handler is None or not handler():
            abort(409)
        return redirect(url_for("index"), code=303)

    @app.post("/creators/<uid>/enabled")
    def set_creator_enabled_form(uid: str) -> Any:
        if not db.set_creator_enabled(uid, request.form.get("enabled") == "1"):
            abort(404)
        return redirect(url_for("index"), code=303)

    @app.post("/creators/<uid>/delete")
    def delete_creator_form(uid: str) -> Any:
        if request.form.get("confirm_uid") != uid:
            abort(400)
        if db.get_creator(uid) is None:
            abort(404)
        db.delete_creator(uid)
        return redirect(url_for("index"), code=303)

    @app.delete("/api/creators/<uid>")
    def delete_creator(uid: str) -> Any:
        if db.get_creator(uid) is None:
            return _error("not_found", "未找到该 UP 主。", 404)
        db.delete_creator(uid)
        return _ok({"creator_uid": uid})

    @app.get("/leaderboard")
    def leaderboard() -> str:
        rows = _creator_rows(db)
        claim_mode = any(row["claim_metrics"].total_claim_count for row in rows)
        if claim_mode:
            eligible = sorted(
                (row for row in rows if row["claim_metrics"].eligible_for_rank),
                key=lambda row: row["claim_metrics"].score or float("-inf"),
                reverse=True,
            )
            insufficient = [
                row for row in rows if not row["claim_metrics"].eligible_for_rank
            ]
        else:
            eligible = sorted(
                (row for row in rows if row["metrics"].eligible_for_rank),
                key=lambda row: row["metrics"].average_signed_return_5d
                or float("-inf"),
                reverse=True,
            )
            insufficient = [row for row in rows if not row["metrics"].eligible_for_rank]
        return render_template(
            "leaderboard.html",
            eligible=eligible,
            insufficient=insufficient,
            claim_mode=claim_mode,
        )

    @app.get("/creators/<uid>")
    def creator_detail(uid: str) -> str:
        creator = db.get_creator(uid)
        if creator is None:
            abort(404)
        videos = db.list_videos(uid)
        rows = [_video_row(db, video) for video in videos]
        counts = _creator_metrics(db, uid).disposition_counts
        rows = _filter_creator_rows(rows, request.args)
        analyses = {
            video.bvid: analysis
            for video in videos
            if (analysis := db.get_latest_analysis(video.bvid)) is not None
        }
        backtest = backtest_creator(
            videos, analyses, db.list_price_bars(), initial_balance=100.0
        )
        trend_coverage = {
            "covered": sum(
                any(is_primary_trend(claim) for claim in row["claims"])
                for row in rows
            ),
            "total": len(videos),
        }
        return render_template(
            "creator.html", creator=creator, rows=rows, counts=counts,
            metrics=_creator_metrics(db, uid), filters=request.args,
            claim_metrics=_creator_claim_metrics(db, uid),
            price_chart=_price_chart(db, videos),
            backtest=backtest,
            trend_coverage=trend_coverage,
            backtest_chart={
                "equity": [
                    {"date": point.trade_date.isoformat(), "balance": point.balance}
                    for point in backtest.equity_curve
                ],
                "gold": [
                    {"date": bar.trade_date.isoformat(), "close": bar.close}
                    for bar in db.list_price_bars()
                ],
                "positions": [
                    {
                        "start_date": segment.start_date.isoformat(),
                        "end_date": segment.end_date.isoformat(),
                        "kind": segment.kind,
                        "bvid": segment.bvid,
                        "title": segment.title,
                        "direction_label": segment.direction_label,
                        "stage_return": segment.stage_return,
                    }
                    for segment in backtest.position_segments
                ],
                "axes": {
                    "balance": "账户余额（美元）",
                    "gold": "XAU/USD（美元/盎司）",
                },
            },
        )

    @app.get("/creators/<uid>/claims")
    def creator_claim_results(uid: str) -> str:
        creator = db.get_creator(uid)
        if creator is None:
            abort(404)
        verdict = request.args.get("verdict", "all")
        if verdict not in {"all", "hit", "partial_near", "miss", "unresolved"}:
            abort(400)
        kind = request.args.get("kind", "all")
        if kind not in {"all", "trend", "price_level"}:
            abort(400)
        videos = {video.bvid: video for video in db.list_videos(uid)}
        rows = []
        for claim in db.list_creator_forecast_claims(uid):
            evaluation = db.get_claim_evaluation(claim.claim_id)
            video = videos.get(claim.bvid)
            if evaluation is None or video is None:
                continue
            primary_trend = is_primary_trend(claim)
            if kind == "trend" and not primary_trend:
                continue
            if kind == "price_level" and primary_trend:
                continue
            if verdict != "all" and evaluation.verdict.value != verdict:
                continue
            rows.append({
                "claim": claim,
                "evaluation": evaluation,
                "video": video,
                    "kind_label": "主要趋势" if primary_trend else "目标点位",
                    "explanation": _claim_explanation(claim, evaluation),
                    "decision_steps": _claim_decision_steps(claim, evaluation, video),
                    "levels": _claim_levels_text(claim),
            })
        rows.sort(
            key=lambda row: (row["video"].published_at, row["claim"].claim_index),
            reverse=True,
        )
        return render_template(
            "claim_results.html",
            creator=creator,
            rows=rows,
            selected_verdict=verdict,
            selected_kind=kind,
            metrics=_creator_claim_metrics(db, uid),
        )

    @app.route("/videos/<bvid>", defaults={"uid": None}, methods=["GET", "POST"])
    @app.route("/creators/<uid>/videos/<bvid>", methods=["GET", "POST"])
    def video_detail(bvid: str, uid: str | None) -> Any:
        video = db.get_video(bvid)
        if video is None:
            abort(404)
        if uid is not None and video.creator_uid != uid:
            abort(404)
        if request.method == "POST":
            _apply_manual_review(db, video, request.form)
            return redirect(
                url_for("video_detail", uid=uid, bvid=bvid) if uid else url_for("video_detail", bvid=bvid),
                code=303,
            )
        analysis = db.get_latest_analysis(bvid)
        claims = db.list_forecast_claims(bvid)
        fact_check = db.get_current_fact_check(bvid)
        fact_check_job = db.get_active_fact_check_job(bvid)
        fact_check_need = detect_fact_check_need(
            video, claims, db.list_transcript_segments(bvid)
        )
        claim_rows = [
            {
                "claim": claim,
                "evaluation": (evaluation := db.get_claim_evaluation(claim.claim_id)),
                "explanation": _claim_explanation(claim, evaluation)
                if evaluation
                else "尚未生成行情评价。",
                "decision_steps": _claim_decision_steps(claim, evaluation, video)
                if evaluation
                else None,
                "levels": _claim_levels_text(claim),
                "legs_json": json.dumps(
                    [
                        {
                            "operator": leg.operator,
                            "level_low": leg.level_low,
                            "level_high": leg.level_high,
                        }
                        for leg in claim.legs
                    ],
                    ensure_ascii=False,
                ),
            }
            for claim in claims
        ]
        return render_template(
            "video.html", video=video, creator=db.get_creator(video.creator_uid),
            analysis=analysis, outcome=db.get_outcome(bvid),
            segments=db.list_transcript_segments(bvid), revisions=db.list_analysis_revisions(bvid),
            price_chart=_price_chart(db, [video]), claim_rows=claim_rows,
            claim_chart=_claim_chart(db, video, claim_rows),
            fact_check=fact_check, fact_check_job=fact_check_job,
            fact_check_need=fact_check_need,
        )

    @app.post("/creators/<uid>/videos/<bvid>/fact-check")
    def enqueue_fact_check(uid: str, bvid: str) -> Any:
        video = db.get_video(bvid)
        if video is None or video.creator_uid != uid:
            abort(404)
        if pipeline is None:
            abort(503)
        try:
            pipeline.enqueue_fact_check(bvid)
        except ValueError:
            abort(400)
        return redirect(url_for("video_detail", uid=uid, bvid=bvid), code=303)

    @app.post(
        "/creators/<uid>/videos/<bvid>/claims/<path:claim_id>/correct"
    )
    def correct_claim(uid: str, bvid: str, claim_id: str) -> Any:
        video = db.get_video(bvid)
        if video is None or video.creator_uid != uid:
            abort(404)
        _apply_claim_correction(db, video, claim_id, request.form)
        return redirect(url_for("video_detail", uid=uid, bvid=bvid), code=303)

    @app.errorhandler(400)
    def bad_request(_error_value: Any) -> Any:
        if request.path.startswith("/api/"):
            return _error("bad_request", "请求无效。", 400)
        return "请求无效。", 400

    @app.errorhandler(404)
    def not_found(_error_value: Any) -> Any:
        if request.path.startswith("/api/"):
            return _error("not_found", "未找到请求的 API。", 404)
        return "未找到页面。", 404

    @app.errorhandler(405)
    def method_not_allowed(_error_value: Any) -> Any:
        if request.path.startswith("/api/"):
            return _error("method_not_allowed", "该 API 不支持此请求方法。", 405)
        return "不支持此请求方法。", 405

    return app


def _apply_manual_review(db: Database, video: Any, values: Any) -> None:
    previous = db.get_latest_analysis(video.bvid)
    if previous is None:
        abort(400)
    try:
        direction = Direction(values.get("direction", ""))
        strength = int(values.get("strength", ""))
        confidence = float(values.get("confidence", ""))
    except (TypeError, ValueError):
        abort(400)
    if not 1 <= strength <= 5 or not 0.0 <= confidence <= 1.0:
        abort(400)
    summary = str(values.get("summary", "")).strip()
    if len(summary) > 1000:
        abort(400)
    excluded = values.get("excluded") == "on"
    exclusion_reason = str(values.get("exclusion_reason", "")).strip()
    if excluded and not exclusion_reason:
        abort(400)
    try:
        target_price = _optional_positive_float(values.get("target_price"))
        stop_price = _optional_positive_float(values.get("stop_price"))
        conditions = _conditions(values.get("conditions", ""))
        evidence = _evidence(values, previous.evidence, db.list_transcript_segments(video.bvid))
    except ValueError:
        abort(400)
    horizon_text = str(values.get("horizon_text", "")).strip() or None
    if horizon_text is not None and len(horizon_text) > 250:
        abort(400)
    amended_summary = summary or previous.summary
    if excluded:
        amended_summary = f"{amended_summary}\n排除原因：{exclusion_reason}".strip()
    amended = replace(
        previous, direction=direction, strength=strength, confidence=confidence,
        horizon_text=horizon_text, target_price=target_price, stop_price=stop_price,
        conditions=conditions, is_retrospective=values.get("is_retrospective") == "on",
        is_news_only=values.get("is_news_only") == "on", evidence=evidence, summary=amended_summary,
        review_status=ReviewStatus.EXCLUDED if excluded else ReviewStatus.APPROVED,
        signal_json=None, revision=previous.revision + 1, created_at=datetime.now(timezone.utc),
    )
    db.save_analysis(amended)
    prices = [PriceBar(*row) for row in db.list_prices()]
    outcome = score_signal(amended, video.published_at, prices)
    if outcome.entry_date is None or outcome.entry_price is None:
        db.delete_outcome(video.bvid)
    else:
        db.save_outcome(outcome)


def _apply_claim_correction(
    db: Database, video: Any, claim_id: str, values: Any
) -> None:
    previous_claims = db.list_forecast_claims(video.bvid)
    target = next(
        (claim for claim in previous_claims if claim.claim_id == claim_id), None
    )
    previous_analysis = db.get_latest_analysis(video.bvid)
    if target is None or previous_analysis is None:
        abort(404)
    try:
        claim_type = ClaimType(str(values.get("claim_type", "")))
        instrument = Instrument(str(values.get("instrument", "")))
        direction_text = str(values.get("direction", "")).strip()
        direction = Direction(direction_text) if direction_text else None
        horizon_source = HorizonSource(str(values.get("horizon_source", "")))
        minimum = _optional_positive_int(values.get("horizon_min"))
        point = _optional_positive_int(values.get("horizon_point"))
        maximum = _optional_positive_int(values.get("horizon_max"))
        time_confidence = _unit_float(values.get("time_confidence"))
        confidence = _unit_float(values.get("confidence"))
        evidence = _evidence(
            values, target.evidence, db.list_transcript_segments(video.bvid)
        )
        legs = _corrected_legs(claim_type, values)
    except (TypeError, ValueError):
        abort(400)
    if horizon_source is HorizonSource.UNKNOWN:
        if any(value is not None for value in (minimum, point, maximum)):
            abort(400)
    elif not (
        minimum is not None
        and point is not None
        and maximum is not None
        and minimum <= point <= maximum
    ):
        abort(400)
    condition_text = str(values.get("condition_text", "")).strip()
    horizon_text = str(values.get("horizon_text", "")).strip() or None
    if not condition_text or len(condition_text) > 500 or (
        horizon_text is not None and len(horizon_text) > 250
    ):
        abort(400)

    revision = max(previous_analysis.revision, target.analysis_revision) + 1
    revised_claims = []
    for index, prior in enumerate(previous_claims):
        common = {
            "claim_id": f"{video.bvid}:{revision}:{index}",
            "analysis_revision": revision,
            "claim_index": index,
        }
        if prior.claim_id != claim_id:
            revised_claims.append(replace(prior, **common))
            continue
        revised_claims.append(
            replace(
                prior,
                **common,
                instrument=instrument,
                claim_type=claim_type,
                direction=direction,
                legs=legs,
                condition_text=condition_text,
                horizon_text=horizon_text,
                horizon_source=horizon_source,
                horizon_min_trading_days=minimum,
                horizon_max_trading_days=maximum,
                horizon_point_trading_days=point,
                time_confidence=time_confidence,
                confidence=confidence,
                evidence=evidence,
                supersedes_claim_id=prior.claim_id,
                status=ClaimStatus.HUMAN_CORRECTED,
            )
        )
    amended = replace(
        previous_analysis,
        direction=direction or previous_analysis.direction,
        confidence=confidence,
        horizon_text=horizon_text,
        target_price=legs[0].level_low if legs else None,
        evidence=evidence,
        review_status=ReviewStatus.APPROVED,
        signal_json=None,
        revision=revision,
        created_at=datetime.now(timezone.utc),
    )
    db.save_claim_extraction(amended, revised_claims)
    recompute_claim_evaluations(db, evaluated_at=datetime.now(timezone.utc))


def _corrected_legs(claim_type: ClaimType, values: Any) -> tuple[ClaimLeg, ...]:
    if claim_type is ClaimType.DIRECTIONAL_MOVE:
        return ()
    if "legs_json" in values:
        try:
            payload = json.loads(str(values.get("legs_json", "[]")))
        except json.JSONDecodeError as error:
            raise ValueError("legs must be JSON") from error
        if not isinstance(payload, list) or not 1 <= len(payload) <= 8:
            raise ValueError("legs list invalid")
        legs = []
        for item in payload:
            if not isinstance(item, dict) or set(item) != {
                "operator",
                "level_low",
                "level_high",
            }:
                raise ValueError("leg object invalid")
            operator = str(item["operator"])
            if operator not in {">=", "<=", "between"}:
                raise ValueError("invalid operator")
            low = _optional_positive_float(item["level_low"])
            high = _optional_positive_float(item["level_high"])
            if low is None or (
                operator == "between" and (high is None or low > high)
            ):
                raise ValueError("invalid levels")
            legs.append(ClaimLeg(operator, low, high))
        if claim_type is ClaimType.SEQUENCE and len(legs) < 2:
            raise ValueError("sequence needs two legs")
        if claim_type is ClaimType.BREAKOUT_EITHER_SIDE and (
            len(legs) != 2 or {leg.operator for leg in legs} != {">=", "<="}
        ):
            raise ValueError("either-side breakout needs upper and lower legs")
        if claim_type not in {ClaimType.SEQUENCE, ClaimType.BREAKOUT_EITHER_SIDE} and len(legs) != 1:
            raise ValueError("claim type accepts one leg")
        return tuple(legs)
    operator = str(values.get("operator", "")).strip()
    if operator not in {">=", "<=", "between"}:
        raise ValueError("invalid operator")
    low = _optional_positive_float(values.get("level_low"))
    high = _optional_positive_float(values.get("level_high"))
    if low is None or (operator == "between" and (high is None or low > high)):
        raise ValueError("invalid levels")
    if claim_type is ClaimType.SEQUENCE:
        raise ValueError("sequence correction requires legs_json")
    return (ClaimLeg(operator, low, high),)


def _enqueue_creator_sync(pipeline: Any, source: object) -> tuple[Any, Any]:
    if not isinstance(source, str):
        raise ValueError("creator source is required")
    parsed = parse_public_source(source)
    if parsed.kind not in {"space", "video"}:
        raise ValueError("creator source must be public Bilibili input")
    if pipeline is None or not hasattr(pipeline, "enqueue_creator_sync"):
        raise RuntimeError("background queue unavailable")
    return parsed, pipeline.enqueue_creator_sync(source, datetime.now(timezone.utc))


def _creator_rows(db: Database) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for creator in db.list_creators():
        metric_samples = db.list_creator_metric_samples(creator.uid)
        rows.append(
            {
                "creator": creator,
                "metrics": _creator_metrics(db, creator.uid),
                "claim_metrics": _creator_claim_metrics(db, creator.uid),
                "video_count": len(metric_samples),
                "trend_count": sum(
                    sample.direction in (Direction.BULLISH, Direction.BEARISH)
                    for sample in metric_samples
                ),
            }
        )
    return rows


def _creator_metrics(db: Database, uid: str) -> Any:
    samples = db.list_creator_metric_samples(uid)
    # Legacy cached outcomes predate structured analyses. Keep them visible and rankable
    # while current records use the richer metric-sample interface.
    return aggregate_creator(samples if any(sample.review_status is not None for sample in samples) else db.list_creator_outcomes(uid))


def _video_row(db: Database, video: Any) -> dict[str, object]:
    claims = db.list_forecast_claims(video.bvid)
    evaluations = [
        evaluation
        for claim in claims
        if (evaluation := db.get_claim_evaluation(claim.claim_id)) is not None
    ]
    return {
        "video": video,
        "analysis": db.get_latest_analysis(video.bvid),
        "outcome": db.get_outcome(video.bvid),
        "claims": claims,
        "claim_metrics": aggregate_video_claims(video.bvid, claims, evaluations),
    }


def _creator_claim_metrics(db: Database, uid: str) -> Any:
    videos = db.list_videos(uid)
    return aggregate_creator_claims(
        [_video_row(db, video)["claim_metrics"] for video in videos]
    )


def _claim_chart(db: Database, video: Any, claim_rows: list[dict[str, object]]) -> dict[str, object]:
    intraday_rows = [
        row
        for row in claim_rows
        if row["evaluation"]
        and (
            row["evaluation"].window_start_at is not None
            or row["evaluation"].window_end_at is not None
        )
    ]
    if intraday_rows:
        ends = [
            row["evaluation"].window_end_at
            for row in intraday_rows
            if row["evaluation"].window_end_at is not None
        ]
        focus_end = max(ends, default=video.published_at + timedelta(hours=24))
        hourly = db.list_intraday_price_bars(video.published_at, focus_end)
        prices = [
            {
                "at": bar.started_at.isoformat(),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
            }
            for bar in hourly
        ]
        markers = [{"kind": "publication", "at": video.published_at.isoformat()}]
        for row in intraday_rows:
            evaluation = row["evaluation"]
            for kind, moment in (
                ("entry", evaluation.window_start_at),
                ("hit", evaluation.first_hit_at),
                ("deadline", evaluation.window_end_at),
            ):
                if moment is not None:
                    marker = {
                        "kind": kind,
                        "at": moment.isoformat(),
                        "claim_id": row["claim"].claim_id,
                    }
                    if marker not in markers:
                        markers.append(marker)
        return {
            "granularity": "1h",
            "prices": prices,
            "all_prices": prices,
            "markers": markers,
            "axis": {
                "x_title": "上海时间（小时）",
                "y_title": "XAU/USD（美元/盎司）",
            },
            "focus": {
                "start": video.published_at.isoformat(),
                "end": focus_end.isoformat(),
            },
            "published_at": video.published_at.isoformat(),
            "claims": [
                {
                    "claim_id": row["claim"].claim_id,
                    "levels": [
                        leg.level_low
                        for leg in row["claim"].legs
                        if leg.level_low is not None
                    ],
                    "window_start": _iso_date(row["evaluation"].window_start_at),
                    "window_end": _iso_date(row["evaluation"].window_end_at),
                    "first_hit": _iso_date(row["evaluation"].first_hit_at),
                }
                for row in intraday_rows
            ],
        }

    all_prices = [
        {"date": trade_date, "open": open_, "high": high, "low": low, "close": close}
        for trade_date, open_, high, low, close in db.list_prices()
    ]
    starts = [
        row["evaluation"].window_start
        for row in claim_rows
        if row["evaluation"] and row["evaluation"].window_start
    ]
    ends = [
        row["evaluation"].window_end
        for row in claim_rows
        if row["evaluation"] and row["evaluation"].window_end
    ]
    focus_start = min(starts, default=video.published_at.date()) - timedelta(days=7)
    focus_end = max(
        ends, default=video.published_at.date() + timedelta(days=20)
    ) + timedelta(days=7)
    prices = [
        row
        for row in all_prices
        if focus_start.isoformat() <= row["date"] <= focus_end.isoformat()
    ] or all_prices
    return {
        "granularity": "1d",
        "prices": prices,
        "all_prices": all_prices,
        "axis": {"x_title": "交易日", "y_title": "XAU/USD（美元/盎司）"},
        "focus": {"start": focus_start.isoformat(), "end": focus_end.isoformat()},
        "published_at": video.published_at.isoformat(),
        "claims": [
            {
                "claim_id": row["claim"].claim_id,
                "levels": [
                    leg.level_low
                    for leg in row["claim"].legs
                    if leg.level_low is not None
                ],
                "window_start": _iso_date(row["evaluation"].window_start)
                if row["evaluation"]
                else None,
                "window_end": _iso_date(row["evaluation"].window_end)
                if row["evaluation"]
                else None,
                "first_hit": _iso_date(row["evaluation"].first_hit_date)
                if row["evaluation"]
                else None,
            }
            for row in claim_rows
        ],
    }


def _claim_levels_text(claim: ForecastClaim) -> str:
    if not claim.legs:
        return "无明确点位"
    values = []
    for leg in claim.legs:
        if leg.operator == "between":
            values.append(f"{leg.level_low:.2f}–{leg.level_high:.2f}")
        else:
            values.append(f"{leg.operator} {leg.level_low:.2f}")
    return "；".join(values)


def _claim_explanation(claim: ForecastClaim, evaluation: Any) -> str:
    low = f"{evaluation.observed_min:.2f}" if evaluation.observed_min is not None else "—"
    high = f"{evaluation.observed_max:.2f}" if evaluation.observed_max is not None else "—"
    observed = f"窗口内最低 {low}，最高 {high}"
    if evaluation.verdict.value == "hit":
        hit_moment = evaluation.first_hit_at or evaluation.first_hit_date
        hit_text = (
            _format_shanghai(evaluation.first_hit_at)
            if evaluation.first_hit_at is not None
            else str(evaluation.first_hit_date)
            if evaluation.first_hit_date is not None
            else None
        )
        if claim.claim_type is ClaimType.BREAKOUT_EITHER_SIDE and hit_moment:
            upper = evaluation.reason.startswith("upper")
            operator = ">=" if upper else "<="
            level = next(leg.level_low for leg in claim.legs if leg.operator == operator)
            side = "上方" if upper else "下方"
            return f"{evaluation.first_hit_date} 先突破{side} {level:.2f}；{observed}。"
        when = (
            f"{hit_text} 首次完成全部条件"
            if hit_moment
            else "观察窗口结束时条件成立"
        )
        return f"{when}；{observed}。"
    if evaluation.verdict.value == "partial_near":
        distance = f"{evaluation.distance_pct * 100:.2f}%" if evaluation.distance_pct is not None else "—"
        closest = f"{evaluation.closest_price:.2f}" if evaluation.closest_price is not None else "—"
        return f"未完全满足条件；最近价 {closest}，距目标 {distance}，按 0.5% 接近阈值计为接近。{observed}。"
    if evaluation.verdict.value == "miss":
        if claim.claim_type is ClaimType.DIRECTIONAL_MOVE and evaluation.entry_price is not None and evaluation.final_close is not None:
            expected = "上涨" if claim.direction is Direction.BULLISH else "下跌" if claim.direction is Direction.BEARISH else "明确方向"
            change = (evaluation.final_close - evaluation.entry_price) / evaluation.entry_price * 100
            return f"预测要求{expected}，但从首个完整交易日开盘 {evaluation.entry_price:.2f} 到窗口末收盘 {evaluation.final_close:.2f} 实际变动 {change:+.2f}%；{observed}。"
        distance = f"，最近仅差 {evaluation.distance_pct * 100:.2f}%" if evaluation.distance_pct is not None else ""
        return f"观察窗口结束仍未满足「{_claim_levels_text(claim)}」{distance}；{observed}。"
    labels = {
        "horizon_not_mature": "预测期限尚未走完",
        "awaiting_first_complete_bar": (
            "并非没有黄金价格；为避免使用视频发布前或发布当天已经发生的行情，"
            "程序只接受发布后的首根完整日线，目前仍在等待该数据"
        ),
        "unresolved_instrument": "品种不是可直接比较的 XAU/USD 现货",
        "unresolved_intraday_data": "尚未形成发布后的完整小时线，不使用跨越发布时刻的行情",
        "intraday_horizon_not_mature": "日内预测仍在观察期，尚未命中时不提前判为未命中",
        "horizon_unknown_awaiting_next_prediction": "原话未给期限，正在等待下一条同品种预测作为截止点",
        "invalid_claim_structure": "结构化条件不完整，程序无法可靠执行",
        "condition_not_triggered": "联网事实核查确认该条件未成立，因此不进入价格命中率",
        "fact_conflicting": "外部事件来源存在冲突，条件分支暂不判定",
        "fact_insufficient": "外部事件证据不足，条件分支暂不判定",
    }
    return labels.get(evaluation.reason, "当前数据不足，暂不能可靠评价") + "。"


def _claim_decision_steps(
    claim: ForecastClaim, evaluation: Any, video: Any | None = None
) -> dict[str, str]:
    if claim.claim_type is ClaimType.DIRECTIONAL_MOVE:
        direction = (
            "要求看涨（变化 > 0）"
            if claim.direction is Direction.BULLISH
            else "要求看跌（变化 < 0）"
            if claim.direction is Direction.BEARISH
            else "不进入方向评分"
        )
        requirement = f"主要趋势：{direction}。"
    elif claim.claim_type is ClaimType.SEQUENCE:
        requirement = "必须按顺序完成：" + " → ".join(
            _leg_requirement(leg) for leg in claim.legs
        ) + "。"
    elif claim.claim_type is ClaimType.BREAKOUT_EITHER_SIDE:
        requirement = "任一边先突破即成立：" + " 或 ".join(
            _leg_requirement(leg) for leg in claim.legs
        ) + "。"
    else:
        requirement = "需要满足：" + _claim_levels_text(claim) + "。"

    if evaluation.verdict.value == "not_triggered":
        market_data = "外部条件未成立；不会为该分支选取价格窗口。"
    elif evaluation.window_start_at is not None and evaluation.window_end_at is not None:
        publication = (
            _format_shanghai(video.published_at) if video is not None else "—"
        )
        if is_event_activated_claim(claim):
            timing_prefix = (
                f"视频发布：{publication}；"
                f"事件生效：{_format_shanghai(claim.deadline_at)}；"
                "跨越事件时刻的小时线已排除。"
            )
        else:
            timing_prefix = (
                f"视频发布：{publication}；"
                "跨越发布时刻的小时线已排除。"
            )
        market_data = timing_prefix + (
            f"首根完整小时线：{_format_shanghai(evaluation.window_start_at)}，"
            f"观察截止：{_format_shanghai(evaluation.window_end_at)}；"
            f"实际最低 {_number(evaluation.observed_min)}，"
            f"实际最高 {_number(evaluation.observed_max)}，"
            f"末根完整小时线收盘 {_number(evaluation.final_close)}。"
        )
        if evaluation.first_hit_at is not None:
            market_data += f"首次命中：{_format_shanghai(evaluation.first_hit_at)}。"
    elif evaluation.window_start is None or evaluation.window_end is None:
        market_data = "尚未形成可用观察窗口；同一发布日的行情不会被用于事后验证。"
    else:
        market_data = (
            f"{evaluation.window_start} 至 {evaluation.window_end}；"
            f"实际最低 {_number(evaluation.observed_min)}，"
            f"实际最高 {_number(evaluation.observed_max)}，"
            f"窗口末收盘 {_number(evaluation.final_close)}。"
        )

    if evaluation.verdict.value in {"unresolved", "not_triggered", "superseded", "excluded"}:
        comparison = _claim_explanation(claim, evaluation)
    elif claim.claim_type is ClaimType.DIRECTIONAL_MOVE:
        change = (
            (evaluation.final_close - evaluation.entry_price)
            / evaluation.entry_price
            * 100
            if evaluation.entry_price and evaluation.final_close is not None
            else None
        )
        entry_label = "首小时开盘" if evaluation.window_start_at else "首日开盘"
        comparison = (
            f"{entry_label} {_number(evaluation.entry_price)} → 截止收盘 "
            f"{_number(evaluation.final_close)}；实际变化 "
            f"{change:+.2f}%；{direction}。"
            if change is not None
            else "缺少首尾价格，无法比较。"
        )
    elif claim.claim_type is ClaimType.SEQUENCE:
        comparisons = [_leg_observation(leg, evaluation) for leg in claim.legs]
        suffix = (
            f"；{_format_hit_moment(evaluation)} 完成最后一个条件。"
            if evaluation.first_hit_at or evaluation.first_hit_date
            else "；观察窗口结束时仍未按顺序全部完成。"
        )
        comparison = "；".join(comparisons) + suffix
    elif claim.claim_type is ClaimType.BREAKOUT_EITHER_SIDE:
        comparison = "；".join(
            _leg_observation(leg, evaluation) for leg in claim.legs
        )
        if evaluation.first_hit_at or evaluation.first_hit_date:
            comparison += f"；{_format_hit_moment(evaluation)} 首次触发其中一侧。"
    elif claim.legs:
        comparison = _leg_observation(claim.legs[0], evaluation)
    else:
        comparison = _claim_explanation(claim, evaluation)
    if evaluation.distance_pct is not None and evaluation.verdict.value != "hit":
        comparison += f" 最近距离为 {evaluation.distance_pct * 100:.2f}%。"

    return {
        "requirement": requirement,
        "market_data": market_data,
        "comparison": comparison,
        "conclusion": f"{_verdict_label(evaluation.verdict)}：{_claim_explanation(claim, evaluation)}",
    }


def _leg_requirement(leg: ClaimLeg) -> str:
    if leg.operator == "between":
        return f"价格保持在 {leg.level_low:.2f}–{leg.level_high:.2f}"
    return f"价格 {leg.operator} {leg.level_low:.2f}"


def _leg_observation(leg: ClaimLeg, evaluation: Any) -> str:
    if leg.operator == ">=":
        actual = evaluation.observed_max
        symbol = ">=" if actual is not None and actual >= leg.level_low else "<"
        return f"实际最高 {_number(actual)} {symbol} 目标 {leg.level_low:.2f}"
    if leg.operator == "<=":
        actual = evaluation.observed_min
        symbol = "<=" if actual is not None and actual <= leg.level_low else ">"
        return f"实际最低 {_number(actual)} {symbol} 目标 {leg.level_low:.2f}"
    return (
        f"实际区间 {_number(evaluation.observed_min)}–{_number(evaluation.observed_max)}，"
        f"要求 {leg.level_low:.2f}–{leg.level_high:.2f}"
    )


def _number(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _format_shanghai(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(_SHANGHAI).strftime("%Y-%m-%d %H:%M")


def _format_hit_moment(evaluation: Any) -> str:
    if evaluation.first_hit_at is not None:
        return _format_shanghai(evaluation.first_hit_at)
    return str(evaluation.first_hit_date) if evaluation.first_hit_date is not None else "—"


def _price_chart(db: Database, videos: list[Any]) -> dict[str, object]:
    prices = [{"date": trade_date, "close": close} for trade_date, _open, _high, _low, close in db.list_prices()]
    dates = [row["date"] for row in prices]
    markers = []
    for video in videos:
        outcome = db.get_outcome(video.bvid)
        marker = {"bvid": video.bvid, "publication": video.published_at.date().isoformat()}
        if outcome and outcome.entry_date:
            entry = outcome.entry_date.isoformat()
            marker["entry"] = entry
            entry_index = dates.index(entry) if entry in dates else -1
            for horizon in (1, 5, 20):
                index = entry_index + horizon - 1
                if entry_index >= 0 and index < len(dates):
                    marker[f"exit_{horizon}d"] = dates[index]
        markers.append(marker)
    return {"prices": prices, "markers": markers}


def _retry_job(pipeline: Any, db: Database, job_id: int) -> bool:
    if pipeline is not None and hasattr(pipeline, "retry_job"):
        return bool(pipeline.retry_job(job_id))
    return db.retry_job(job_id)


def _optional_positive_float(value: object) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    parsed = float(str(value))
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError("price must be finite and positive")
    return parsed


def _optional_positive_int(value: object) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    parsed = int(str(value))
    if parsed < 1:
        raise ValueError("trading days must be positive")
    return parsed


def _unit_float(value: object) -> float:
    parsed = float(str(value))
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        raise ValueError("confidence must be between zero and one")
    return parsed


def _conditions(value: object) -> tuple[str, ...]:
    rows = tuple(row.strip() for row in str(value).splitlines() if row.strip())
    if len(rows) > 20 or any(len(row) > 200 for row in rows):
        raise ValueError("conditions invalid")
    return rows


def _evidence(values: Any, prior: tuple[dict[str, object], ...], segments: list[Any]) -> tuple[dict[str, object], ...]:
    if "evidence_json" not in values:
        return prior
    try:
        payload = json.loads(str(values.get("evidence_json", "[]")))
    except json.JSONDecodeError as error:
        raise ValueError("evidence must be JSON") from error
    if not isinstance(payload, list) or len(payload) > 20:
        raise ValueError("evidence list invalid")
    normalised = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("evidence item invalid")
        try:
            start, end = float(item["start_sec"]), float(item["end_sec"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("evidence time invalid") from error
        quote = item.get("quote")
        if not math.isfinite(start) or not math.isfinite(end) or end < start or not isinstance(quote, str) or not quote.strip():
            raise ValueError("evidence value invalid")
        if not evidence_is_locatable(quote, start, end, segments):
            raise ValueError("evidence is not locatable in cached transcript")
        normalised.append({"start_sec": start, "end_sec": end, "quote": quote.strip()})
    return tuple(normalised)


def _filter_creator_rows(rows: list[dict[str, object]], filters: Any) -> list[dict[str, object]]:
    disposition, direction = filters.get("disposition", "all"), filters.get("direction", "all")
    review, maturity = filters.get("review", "all"), filters.get("maturity", "all")
    filtered = []
    for row in rows:
        analysis, outcome = row["analysis"], row["outcome"]
        actual_disposition = analysis.review_status.value if analysis else "unanalysed"
        if disposition != "all" and actual_disposition != disposition:
            continue
        if review != "all" and (analysis is None or analysis.review_status.value != review):
            continue
        if direction != "all" and (analysis is None or analysis.direction.value != direction):
            continue
        if maturity == "mature" and not (outcome and outcome.mature):
            continue
        if maturity == "immature" and outcome and outcome.mature:
            continue
        filtered.append(row)
    return filtered


def _format_percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:+.2f}%"


def _format_claim_horizon(claim: Any) -> str:
    point = claim.horizon_point_trading_days
    minimum = claim.horizon_min_trading_days
    maximum = claim.horizon_max_trading_days
    if point is None or minimum is None or maximum is None:
        return "周期不明确"
    if point == 0 or minimum == 0:
        return "日内/次一交易日（需要发布后的匹配行情）"
    return f"{point} 个交易日（{minimum}–{maximum}）"


def _verdict_label(value: Any) -> str:
    labels = {
        "hit": "命中",
        "partial_near": "接近",
        "miss": "未命中",
        "unresolved": "不可评价",
        "not_triggered": "条件未触发",
        "superseded": "已被后续观点替代",
        "excluded": "已排除",
    }
    key = value.value if hasattr(value, "value") else str(value)
    return labels.get(key, key)


def _iso_date(value: Any) -> str | None:
    return None if value is None else value.isoformat()


def _ok(data: Any, status: int = 200) -> Any:
    return jsonify({"ok": True, "data": data}), status


def _error(code: str, message: str, status: int) -> Any:
    return jsonify({"ok": False, "error": {"code": code, "message": message}}), status
