"""MiniMax signal extraction with transcript-grounded evidence."""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from goldbook.config import Settings
from goldbook.models import (
    ClaimLeg,
    ClaimStatus,
    ClaimType,
    Direction,
    ForecastClaim,
    HorizonSource,
    Instrument,
    ReviewStatus,
    SignalAnalysis,
    TranscriptSegment,
    Video,
)


_HARD_MAX_CONCURRENCY = 20
_RETRY_ATTEMPTS = 3
_PARSE_ATTEMPTS = 2
_REQUEST_TIMEOUT_SEC = 120.0
_CLAIM_REQUEST_TIMEOUT_SEC = 600.0
_FACT_CHECK_REQUEST_TIMEOUT_SEC = 180.0
CLAIM_PROMPT_VERSION = "claims-v3-grounded-context"
_SCHEMA_INSTRUCTION = """Return exactly one JSON object with this schema:
{
  \"direction\": \"bullish|bearish|neutral|no_signal\",
  \"strength\": 1,
  \"confidence\": 0.0,
  \"horizon_text\": null,
  \"target_price\": null,
  \"stop_price\": null,
  \"conditions\": [],
  \"is_retrospective\": false,
  \"is_news_only\": false,
  \"evidence\": [{\"start_sec\": 0.0, \"end_sec\": 0.0, \"quote\": \"\"}],
  \"summary\": \"\"
}
Evidence quotes must be verbatim and locatable in the numbered transcript."""
_CLAIM_SCHEMA_INSTRUCTION = """你只做发布时点的预测观点抽取，不判断事后输赢。无需展示推理，直接返回 JSON。
返回且只返回一个 JSON 对象，顶层键必须恰好是 summary、primary_trend 和 claims。
primary_trend 是每个视频唯一的主要趋势，必须包含：
direction: bullish|bearish|neutral|no_signal
condition_text: string
horizon_text: string|null
horizon_source: explicit_exact|explicit_relative|context_inferred|unknown
horizon_min_trading_days: integer|null
horizon_max_trading_days: integer|null
horizon_point_trading_days: integer|null
deadline_at: timezone-aware ISO datetime|null
time_confidence: 0..1
confidence: 0..1
evidence: [{start_sec: number, end_sec: number, quote: string}]
status: auto_validated|unresolved|excluded

claims 只放与点位、区间、波动或先后顺序有关的附加观点，每项必须包含：
instrument: xau_usd_spot|comex_gc|shfe_au|unknown
claim_type: target_touch|cross_above|cross_below|hold_above|hold_below|range|volatility|sequence|breakout_either_side
direction: bullish|bearish|neutral|null
legs: [{operator: >=|<=|between, level_low: number|null, level_high: number|null}]
condition_text: string
horizon_text: string|null
horizon_source: explicit_exact|explicit_relative|context_inferred|unknown
horizon_min_trading_days: integer|null
horizon_max_trading_days: integer|null
horizon_point_trading_days: integer|null
deadline_at: timezone-aware ISO datetime|null
time_confidence: 0..1
confidence: 0..1
evidence: [{start_sec: number, end_sec: number, quote: string}]
status: auto_validated|unresolved|excluded

主要趋势必须从完整字幕理解：逢低买入、回调做多、继续走强属于 bullish；逢高卖出、反弹做空、继续走弱属于 bearish；明确横盘或方向未定属于 neutral；纯资讯且没有未来趋势判断才是 no_signal。neutral/no_signal 在策略回测中对应空仓现金，不得与 bearish 混同。bullish、bearish、neutral 必须给出可定位的原话证据；no_signal 必须 status=unresolved、evidence=[]、horizon_source=unknown 且期限字段全为 null。

一条视频可返回多个点位观点，短期、中期、长期必须分别抽取。若字幕上下文足够，必须把相对周期换算为具体交易日点估计和 min/max 区间；上下文不足则 horizon_source=unknown 且三个交易日字段均为 null，绝不能默认 20 天。区分目标触达、突破、跌破、连续两日站稳、区间、方向和先后顺序；例如“先回踩4650再看4700”使用 sequence 两个 legs；“4200或3940哪边先突破就走哪边”使用 breakout_either_side，legs 分别为 >=4200 与 <=3940，不得简化成主要趋势。每条证据 quote 必须直接复制字幕原文，禁止改写、纠错或概括；若 ASR 有错字、乱码或不通顺，也必须原样复制，不能自行修正；无法逐字引用就不要输出该观点。start_sec/end_sec 必须复制覆盖该原文的字幕时间边界。若标题和相邻字幕已明确正在讨论黄金/金价，且点位为美元金价，则 instrument=xau_usd_spot；原油、股票等其它资产必须保持 unknown，不能混入黄金评分。不要输出当前视频之后的任何信息。"""
_PRIMARY_TREND_KEYS = {
    "direction",
    "condition_text",
    "horizon_text",
    "horizon_source",
    "horizon_min_trading_days",
    "horizon_max_trading_days",
    "horizon_point_trading_days",
    "deadline_at",
    "time_confidence",
    "confidence",
    "evidence",
    "status",
}


@dataclass(frozen=True)
class ClaimExtraction:
    summary: str
    claims: tuple[ForecastClaim, ...]
    rejected_count: int = 0


@dataclass(frozen=True)
class ClaimExtractionFailure:
    bvid: str
    reason: str


class ClaimExtractionError(ValueError):
    """The provider returned a response that is unsafe to evaluate."""


class MiniMaxProviderError(RuntimeError):
    """A retryable provider failure whose detail is safe to persist."""

    def __init__(self) -> None:
        super().__init__("MiniMax provider unavailable")


class MiniMaxClient:
    """A bounded MiniMax client that turns failed analysis into manual review."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = settings.minimax_base_url.rstrip("/")
        self._model = settings.minimax_model
        self._api_key = settings.minimax_api_key
        self._http_client = http_client or httpx.Client()
        self._test_request: Callable[[Sequence[TranscriptSegment]], str] | None = None
        self._sleep = sleep
        self._set_concurrency_limit(settings.minimax_max_concurrency)

    @classmethod
    def for_test(
        cls,
        request: Callable[[Sequence[TranscriptSegment]], str],
        *,
        max_concurrency: int = _HARD_MAX_CONCURRENCY,
        sleep: Callable[[float], None] = lambda _delay: None,
    ) -> "MiniMaxClient":
        """Build a no-network client for tests using an explicit fake transport."""
        client = cls.__new__(cls)
        client._base_url = ""
        client._model = ""
        client._api_key = None
        client._http_client = None
        client._test_request = request
        client._sleep = sleep
        client._set_concurrency_limit(max_concurrency)
        return client

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    def analyze(self, segments: Sequence[TranscriptSegment]) -> SignalAnalysis:
        for attempt in range(_PARSE_ATTEMPTS):
            try:
                return parse_analysis(self._request_with_retries(segments), segments)
            except MiniMaxProviderError:
                raise
            except Exception:
                if attempt == _PARSE_ATTEMPTS - 1:
                    return _manual_review_result()
        raise RuntimeError("MiniMax parse retry loop exhausted")

    def analyze_claims(
        self,
        video: Video,
        segments: Sequence[TranscriptSegment],
        *,
        revision: int,
        transcript_hash: str,
    ) -> ClaimExtraction:
        for attempt in range(_PARSE_ATTEMPTS):
            try:
                payload = self._request_claims_with_retries(video, segments)
                return parse_claim_extraction(
                    payload,
                    video,
                    segments,
                    revision=revision,
                    transcript_hash=transcript_hash,
                    model_name=self._model or "MiniMax-M3",
                )
            except MiniMaxProviderError:
                raise
            except ClaimExtractionError as error:
                if attempt == _PARSE_ATTEMPTS - 1:
                    raise error from None
        raise RuntimeError("MiniMax claim parse retry loop exhausted")

    def complete_fact_check(
        self, messages: Sequence[Mapping[str, str]]
    ) -> Mapping[str, object]:
        """Return one JSON object for a bounded fact-check agent turn."""
        for attempt in range(_PARSE_ATTEMPTS):
            content = self._request_fact_check_with_retries(messages)
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                if attempt + 1 == _PARSE_ATTEMPTS:
                    raise ValueError("MiniMax fact-check response is not JSON") from None
                continue
            if isinstance(parsed, dict):
                return parsed
            if attempt + 1 == _PARSE_ATTEMPTS:
                raise ValueError("MiniMax fact-check response must be an object")
        raise RuntimeError("MiniMax fact-check parse retry loop exhausted")

    def _set_concurrency_limit(self, requested: int) -> None:
        if isinstance(requested, bool) or not isinstance(requested, int):
            raise ValueError("max_concurrency must be an integer")
        self._max_concurrency = min(_HARD_MAX_CONCURRENCY, max(1, requested))
        self._request_semaphore = threading.BoundedSemaphore(self._max_concurrency)

    def _completion_url(self) -> str:
        """Use the native text endpoint required by M3 on the China API host."""
        path = (
            "text/chatcompletion_v2"
            if self._model == "MiniMax-M3"
            else "chat/completions"
        )
        return f"{self._base_url}/{path}"

    def _request_with_retries(self, segments: Sequence[TranscriptSegment]) -> str:
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                response = self._send_request(segments)
            except httpx.TransportError:
                if attempt == _RETRY_ATTEMPTS - 1:
                    raise MiniMaxProviderError() from None
                self._sleep(float(attempt + 1))
                continue
            if isinstance(response, str):
                return response
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == _RETRY_ATTEMPTS - 1:
                    raise MiniMaxProviderError()
                self._sleep(float(attempt + 1))
                continue
            response.raise_for_status()
            return _content_from_response(response)
        raise RuntimeError("MiniMax retry loop exhausted")

    def _send_request(self, segments: Sequence[TranscriptSegment]) -> str | httpx.Response:
        with self._request_semaphore:
            if self._test_request is not None:
                return self._test_request(segments)
            if not self._api_key:
                raise ValueError("MINIMAX_API_KEY is required")
            if self._http_client is None:
                raise RuntimeError("MiniMax HTTP client is unavailable")
            return self._http_client.post(
                self._completion_url(),
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=_REQUEST_TIMEOUT_SEC,
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": _SCHEMA_INSTRUCTION},
                        {"role": "user", "content": _format_segments(segments)},
                    ],
                },
            )

    def _request_claims_with_retries(
        self, video: Video, segments: Sequence[TranscriptSegment]
    ) -> str:
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                response = self._send_claim_request(video, segments)
            except httpx.TransportError:
                if attempt == _RETRY_ATTEMPTS - 1:
                    raise MiniMaxProviderError() from None
                self._sleep(float(attempt + 1))
                continue
            if isinstance(response, str):
                return response
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == _RETRY_ATTEMPTS - 1:
                    raise MiniMaxProviderError()
                self._sleep(float(attempt + 1))
                continue
            response.raise_for_status()
            return _content_from_response(response)
        raise RuntimeError("MiniMax claim retry loop exhausted")

    def _send_claim_request(
        self, video: Video, segments: Sequence[TranscriptSegment]
    ) -> str | httpx.Response:
        with self._request_semaphore:
            if self._test_request is not None:
                return self._test_request(segments)
            if not self._api_key:
                raise ValueError("MINIMAX_API_KEY is required")
            if self._http_client is None:
                raise RuntimeError("MiniMax HTTP client is unavailable")
            return self._http_client.post(
                self._completion_url(),
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=_CLAIM_REQUEST_TIMEOUT_SEC,
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": _CLAIM_SCHEMA_INSTRUCTION},
                        {
                            "role": "user",
                            "content": _format_claim_context(video, segments),
                        },
                    ],
                },
            )

    def _request_fact_check_with_retries(
        self, messages: Sequence[Mapping[str, str]]
    ) -> str:
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                response = self._send_fact_check_request(messages)
            except httpx.TransportError:
                if attempt + 1 == _RETRY_ATTEMPTS:
                    raise MiniMaxProviderError() from None
                self._sleep(float(attempt + 1))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 == _RETRY_ATTEMPTS:
                    raise MiniMaxProviderError()
                self._sleep(float(attempt + 1))
                continue
            response.raise_for_status()
            return _content_from_response(response)
        raise RuntimeError("MiniMax fact-check retry loop exhausted")

    def _send_fact_check_request(
        self, messages: Sequence[Mapping[str, str]]
    ) -> httpx.Response:
        with self._request_semaphore:
            if not self._api_key:
                raise ValueError("MINIMAX_API_KEY is required")
            if self._http_client is None:
                raise RuntimeError("MiniMax HTTP client is unavailable")
            return self._http_client.post(
                self._completion_url(),
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=_FACT_CHECK_REQUEST_TIMEOUT_SEC,
                json={"model": self._model, "messages": list(messages)},
            )


def run_analysis_batch(
    client: MiniMaxClient,
    items: Sequence[tuple[str, Sequence[TranscriptSegment]]],
) -> dict[str, SignalAnalysis]:
    """Analyze a batch without ever scheduling more than the client ceiling."""
    def analyze_item(item: tuple[str, Sequence[TranscriptSegment]]) -> tuple[str, SignalAnalysis]:
        identifier, segments = item
        return identifier, client.analyze(segments)

    with ThreadPoolExecutor(max_workers=client.max_concurrency) as executor:
        return dict(executor.map(analyze_item, items))


def run_claim_extraction_batch(
    client: MiniMaxClient,
    items: Sequence[tuple[Video, Sequence[TranscriptSegment], int, str]],
) -> dict[str, ClaimExtraction | ClaimExtractionFailure]:
    """Extract independently so one malformed video cannot abort the batch."""

    def analyze_item(
        item: tuple[Video, Sequence[TranscriptSegment], int, str]
    ) -> tuple[str, ClaimExtraction | ClaimExtractionFailure]:
        video, segments, revision, transcript_hash = item
        try:
            result = client.analyze_claims(
                video,
                segments,
                revision=revision,
                transcript_hash=transcript_hash,
            )
        except MiniMaxProviderError:
            result = ClaimExtractionFailure(video.bvid, "provider unavailable")
        except ClaimExtractionError as error:
            result = ClaimExtractionFailure(
                video.bvid, f"invalid structured response: {error}"
            )
        except (TypeError, ValueError):
            result = ClaimExtractionFailure(video.bvid, "malformed provider response")
        return video.bvid, result

    with ThreadPoolExecutor(max_workers=client.max_concurrency) as executor:
        return dict(executor.map(analyze_item, items))


def _content_from_response(response: httpx.Response) -> str:
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("MiniMax response must be an object")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("MiniMax response has no choices")
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise ValueError("MiniMax choice must be an object")
    message = first_choice.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ValueError("MiniMax response has no text content")
    return message["content"]


def _format_segments(segments: Sequence[TranscriptSegment]) -> str:
    return "\n".join(
        f"[{index}] {segment.start_sec:.3f}-{segment.end_sec:.3f}: {segment.text}"
        for index, segment in enumerate(segments, start=1)
    )


def _format_claim_context(
    video: Video, segments: Sequence[TranscriptSegment]
) -> str:
    return (
        f"title: {video.title}\n"
        f"published_at: {video.published_at.isoformat()}\n"
        "transcript:\n"
        f"{_format_segments(segments)}"
    )


def _manual_review_result() -> SignalAnalysis:
    return SignalAnalysis(
        direction=Direction.NO_SIGNAL,
        strength=1,
        confidence=0.0,
        evidence=(),
        summary="MiniMax analysis unavailable",
        review_status=ReviewStatus.NEEDS_REVIEW,
    )


def parse_analysis(
    payload_text: str, segments: Sequence[TranscriptSegment]
) -> SignalAnalysis:
    """Parse a model response and retain only transcript-locatable evidence."""
    payload = _extract_json_object(payload_text)
    direction = _parse_direction(payload.get("direction"))
    strength = _parse_strength(payload.get("strength"))
    confidence = _parse_confidence(payload.get("confidence"))
    horizon_text = _parse_optional_text(payload.get("horizon_text"), "horizon_text")
    target_price = _parse_optional_number(payload.get("target_price"), "target_price")
    stop_price = _parse_optional_number(payload.get("stop_price"), "stop_price")
    conditions = _parse_string_list(payload.get("conditions"), "conditions")
    is_retrospective = _parse_bool(payload.get("is_retrospective"), "is_retrospective")
    is_news_only = _parse_bool(payload.get("is_news_only"), "is_news_only")
    summary = _parse_text(payload.get("summary"), "summary")
    evidence, invalid_evidence = _parse_evidence(payload.get("evidence"), segments)

    approved = (
        direction in {Direction.BULLISH, Direction.BEARISH}
        and confidence >= 0.70
        and bool(evidence)
        and not invalid_evidence
        and not is_retrospective
        and not is_news_only
    )
    return SignalAnalysis(
        direction=direction,
        strength=strength,
        confidence=confidence,
        horizon_text=horizon_text,
        target_price=target_price,
        stop_price=stop_price,
        conditions=conditions,
        is_retrospective=is_retrospective,
        is_news_only=is_news_only,
        evidence=evidence,
        summary=summary,
        review_status=(ReviewStatus.APPROVED if approved else ReviewStatus.NEEDS_REVIEW),
        signal_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


def parse_claim_extraction(
    payload_text: str,
    video: Video,
    segments: Sequence[TranscriptSegment],
    *,
    revision: int,
    transcript_hash: str,
    model_name: str,
) -> ClaimExtraction:
    """Parse multiple transcript-grounded claims without seeing future data."""
    try:
        payload = _extract_json_object(payload_text)
        if set(payload) == _PRIMARY_TREND_KEYS:
            payload = {
                "summary": payload["condition_text"],
                "primary_trend": payload,
                "claims": [],
            }
        _require_exact_keys(
            payload, {"summary", "primary_trend", "claims"}, "claim extraction"
        )
        summary = _parse_text(payload.get("summary"), "summary")
        primary_trend = _parse_primary_trend(
            payload.get("primary_trend"),
            video,
            segments,
            revision=revision,
            transcript_hash=transcript_hash,
            model_name=model_name,
        )
        raw_claims = payload.get("claims")
        if not isinstance(raw_claims, list):
            raise ClaimExtractionError("claims must be a list")
        claims: list[ForecastClaim] = [primary_trend]
        rejected_count = 0
        for index, raw_claim in enumerate(raw_claims, start=1):
            try:
                if _is_noncommittal_claim(raw_claim):
                    raise ClaimExtractionError("noncommittal wording is not a forecast")
                if isinstance(raw_claim, dict) and raw_claim.get("claim_type") == "directional_move":
                    raise ClaimExtractionError(
                        "point claim cannot duplicate primary directional trend"
                    )
                claims.append(
                    _parse_forecast_claim(
                        raw_claim,
                        video,
                        segments,
                        revision=revision,
                        claim_index=index,
                        transcript_hash=transcript_hash,
                        model_name=model_name,
                    )
                )
            except ClaimExtractionError:
                rejected_count += 1
        return ClaimExtraction(
            summary=summary,
            claims=tuple(claims),
            rejected_count=rejected_count,
        )
    except ClaimExtractionError:
        raise
    except (TypeError, ValueError, KeyError) as error:
        raise ClaimExtractionError(str(error)) from error


def _is_noncommittal_claim(value: object) -> bool:
    """Exclude wishes and watch-levels that are not falsifiable forecasts."""
    if not isinstance(value, dict):
        return False
    text = str(value.get("condition_text") or "")
    return any(marker in text for marker in ("希望", "关注", "留意", "可能", "或许"))


def _parse_primary_trend(
    value: object,
    video: Video,
    segments: Sequence[TranscriptSegment],
    *,
    revision: int,
    transcript_hash: str,
    model_name: str,
) -> ForecastClaim:
    if not isinstance(value, dict):
        raise ClaimExtractionError("primary_trend must be an object")
    raw = dict(value)
    raw.setdefault("deadline_at", None)
    _require_exact_keys(
        raw,
        _PRIMARY_TREND_KEYS,
        "primary trend",
    )
    direction = _parse_enum(Direction, raw.get("direction"), "direction")
    status = _parse_enum(ClaimStatus, raw.get("status"), "status")
    deadline_value = raw.get("deadline_at")
    if isinstance(deadline_value, str):
        try:
            parsed_deadline = datetime.fromisoformat(deadline_value)
        except ValueError:
            parsed_deadline = None
        if (
            parsed_deadline is not None
            and parsed_deadline.tzinfo is not None
            and parsed_deadline.utcoffset() is not None
            and parsed_deadline <= video.published_at
        ):
            raw["deadline_at"] = None
    if (
        direction is not Direction.NO_SIGNAL
        and raw.get("horizon_source") != HorizonSource.UNKNOWN.value
        and raw.get("horizon_point_trading_days") is None
        and raw.get("deadline_at") is None
    ):
        raw["horizon_source"] = HorizonSource.UNKNOWN.value
        raw["horizon_min_trading_days"] = None
        raw["horizon_max_trading_days"] = None
    if direction is not Direction.NO_SIGNAL:
        raw["evidence"] = _repair_primary_evidence(raw.get("evidence"), segments)
    if direction is Direction.NO_SIGNAL:
        if status is not ClaimStatus.UNRESOLVED:
            raise ClaimExtractionError("no_signal primary trend must be unresolved")
        if raw.get("evidence") != []:
            raise ClaimExtractionError("no_signal primary trend evidence must be empty")
        if raw.get("horizon_source") != HorizonSource.UNKNOWN.value or any(
            raw.get(field) is not None
            for field in (
                "horizon_min_trading_days",
                "horizon_max_trading_days",
                "horizon_point_trading_days",
            )
        ):
            raise ClaimExtractionError("no_signal primary trend horizon must be unknown")
    elif not raw.get("evidence"):
        raise ClaimExtractionError("primary trend evidence is not locatable")

    parsed = _parse_forecast_claim(
        {
            "instrument": Instrument.XAU_USD_SPOT.value,
            "claim_type": ClaimType.DIRECTIONAL_MOVE.value,
            "direction": direction.value,
            "legs": [],
            **raw,
        },
        video,
        segments,
        revision=revision,
        claim_index=0,
        transcript_hash=transcript_hash,
        model_name=model_name,
        allow_empty_evidence=direction is Direction.NO_SIGNAL,
    )
    if direction is not Direction.NO_SIGNAL and not parsed.evidence:
        raise ClaimExtractionError("primary trend evidence is not locatable")
    return parsed


def _parse_forecast_claim(
    value: object,
    video: Video,
    segments: Sequence[TranscriptSegment],
    *,
    revision: int,
    claim_index: int,
    transcript_hash: str,
    model_name: str,
    allow_empty_evidence: bool = False,
) -> ForecastClaim:
    if not isinstance(value, dict):
        raise ClaimExtractionError("each claim must be an object")
    value = dict(value)
    value.setdefault("deadline_at", None)
    _require_exact_keys(
        value,
        {
            "instrument",
            "claim_type",
            "direction",
            "legs",
            "condition_text",
            "horizon_text",
            "horizon_source",
            "horizon_min_trading_days",
            "horizon_max_trading_days",
            "horizon_point_trading_days",
            "deadline_at",
            "time_confidence",
            "confidence",
            "evidence",
            "status",
        },
        "claim",
    )
    instrument = _parse_enum(Instrument, value.get("instrument"), "instrument")
    claim_type = _parse_enum(ClaimType, value.get("claim_type"), "claim_type")
    direction_value = value.get("direction")
    direction = (
        None
        if direction_value is None
        else _parse_enum(Direction, direction_value, "direction")
    )
    legs_value = value.get("legs")
    if not isinstance(legs_value, list):
        raise ClaimExtractionError("legs must be a list")
    legs = (
        ()
        if claim_type in {ClaimType.DIRECTIONAL_MOVE, ClaimType.VOLATILITY}
        else tuple(_parse_claim_leg(item) for item in legs_value)
    )
    if claim_type is ClaimType.SEQUENCE and len(legs) < 2:
        raise ClaimExtractionError("sequence claim requires at least two legs")
    if claim_type is ClaimType.BREAKOUT_EITHER_SIDE and (
        len(legs) != 2 or {leg.operator for leg in legs} != {">=", "<="}
    ):
        raise ClaimExtractionError("either-side breakout requires upper and lower legs")
    if claim_type not in {ClaimType.DIRECTIONAL_MOVE, ClaimType.VOLATILITY} and not legs:
        raise ClaimExtractionError("executable claim requires a leg")

    horizon_source = _parse_enum(
        HorizonSource, value.get("horizon_source"), "horizon_source"
    )
    minimum = _parse_optional_positive_int(
        value.get("horizon_min_trading_days"), "horizon_min_trading_days"
    )
    maximum = _parse_optional_positive_int(
        value.get("horizon_max_trading_days"), "horizon_max_trading_days"
    )
    point = _parse_optional_positive_int(
        value.get("horizon_point_trading_days"), "horizon_point_trading_days"
    )
    if horizon_source is HorizonSource.UNKNOWN:
        if any(item is not None for item in (minimum, point, maximum)):
            raise ClaimExtractionError("unknown horizon cannot contain trading days")
    elif point is None and value.get("deadline_at") is None:
        raise ClaimExtractionError("horizon requires a point estimate")
    elif point is not None:
        minimum = point if minimum is None else minimum
        maximum = point if maximum is None else maximum
        minimum = min(minimum, point, maximum)
        maximum = max(minimum, point, maximum)

    deadline_at = _parse_optional_aware_datetime(value.get("deadline_at"))
    if deadline_at is not None and deadline_at <= video.published_at:
        raise ClaimExtractionError("deadline must be after publication")
    evidence, invalid_evidence = _parse_evidence(value.get("evidence"), segments)
    if invalid_evidence or (not evidence and not allow_empty_evidence):
        raise ClaimExtractionError("claim evidence is not locatable")
    status = _parse_enum(ClaimStatus, value.get("status"), "status")
    if status is ClaimStatus.HUMAN_CORRECTED:
        raise ClaimExtractionError("model cannot create human-corrected claims")

    return ForecastClaim(
        claim_id=f"{video.bvid}:{revision}:{claim_index}",
        bvid=video.bvid,
        analysis_revision=revision,
        claim_index=claim_index,
        instrument=instrument,
        claim_type=claim_type,
        direction=direction,
        legs=legs,
        condition_text=_parse_text(value.get("condition_text"), "condition_text"),
        horizon_text=_parse_optional_text(value.get("horizon_text"), "horizon_text"),
        horizon_source=horizon_source,
        horizon_min_trading_days=minimum,
        horizon_max_trading_days=maximum,
        horizon_point_trading_days=point,
        deadline_at=deadline_at,
        time_confidence=_parse_number_in_range(
            value.get("time_confidence"), "time_confidence", 0.0, 1.0
        ),
        confidence=_parse_confidence(value.get("confidence")),
        evidence=evidence,
        supersedes_claim_id=None,
        status=status,
        model_name=model_name,
        prompt_version=CLAIM_PROMPT_VERSION,
        transcript_hash=transcript_hash,
    )


def _parse_claim_leg(value: object) -> ClaimLeg:
    if not isinstance(value, dict):
        raise ClaimExtractionError("each claim leg must be an object")
    _require_exact_keys(value, {"operator", "level_low", "level_high"}, "claim leg")
    operator = _parse_text(value.get("operator"), "operator")
    if operator not in {">=", "<=", "between"}:
        raise ClaimExtractionError("unsupported claim operator")
    low = _parse_optional_number(value.get("level_low"), "level_low")
    high = _parse_optional_number(value.get("level_high"), "level_high")
    if operator in {">=", "<="} and low is None:
        raise ClaimExtractionError("point operator requires level_low")
    if operator == "between" and (low is None or high is None or low > high):
        raise ClaimExtractionError("between operator requires ordered levels")
    return ClaimLeg(operator=operator, level_low=low, level_high=high)


def _require_exact_keys(
    value: dict[str, object], expected: set[str], field_name: str
) -> None:
    actual = set(value)
    if actual != expected:
        raise ClaimExtractionError(
            f"{field_name} keys differ: missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _parse_enum(enum_type: type[Any], value: object, field_name: str) -> Any:
    if not isinstance(value, str):
        raise ClaimExtractionError(f"{field_name} must be a string enum value")
    try:
        return enum_type(value)
    except ValueError as error:
        raise ClaimExtractionError(f"invalid {field_name}") from error


def _parse_optional_positive_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ClaimExtractionError(f"{field_name} must be a non-negative integer")
    return value


def _parse_optional_aware_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ClaimExtractionError("deadline_at must be an ISO datetime or null")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ClaimExtractionError("deadline_at must be an ISO datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ClaimExtractionError("deadline_at must be timezone-aware")
    return parsed


def _extract_json_object(payload_text: str) -> dict[str, Any]:
    if not isinstance(payload_text, str):
        raise ValueError("MiniMax payload must be text")

    decoder = json.JSONDecoder()
    start = payload_text.find("{")
    while start >= 0:
        try:
            value, _ = decoder.raw_decode(payload_text[start:])
        except json.JSONDecodeError:
            start = payload_text.find("{", start + 1)
            continue
        if not isinstance(value, dict):
            raise ValueError("MiniMax payload must contain a JSON object")
        return value
    raise ValueError("MiniMax payload does not contain a JSON object")


def _parse_direction(value: object) -> Direction:
    if not isinstance(value, str):
        raise ValueError("direction must be a string enum value")
    try:
        return Direction(value)
    except ValueError as error:
        raise ValueError("invalid direction") from error


def _parse_strength(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
        raise ValueError("strength must be an integer from 1 to 5")
    return value


def _parse_confidence(value: object) -> float:
    return _parse_number_in_range(value, "confidence", 0.0, 1.0)


def _parse_optional_number(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    return _parse_number_in_range(value, field_name, -math.inf, math.inf)


def _parse_number_in_range(
    value: object, field_name: str, minimum: float, maximum: float
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{field_name} is outside its allowed range")
    return number


def _parse_optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _parse_text(value, field_name)


def _parse_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _parse_string_list(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return tuple(value)


def _parse_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _parse_evidence(
    value: object, segments: Sequence[TranscriptSegment]
) -> tuple[tuple[dict[str, object], ...], bool]:
    if not isinstance(value, list):
        raise ValueError("evidence must be a list")

    valid_evidence: list[dict[str, object]] = []
    invalid_evidence = False
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("each evidence item must be an object")
        start_sec = _parse_number_in_range(item.get("start_sec"), "evidence.start_sec", 0, math.inf)
        end_sec = _parse_number_in_range(item.get("end_sec"), "evidence.end_sec", 0, math.inf)
        quote = _parse_text(item.get("quote"), "evidence.quote")
        if not quote.strip() or end_sec < start_sec or not evidence_is_locatable(
            quote, start_sec, end_sec, segments
        ):
            invalid_evidence = True
            continue
        valid_evidence.append(
            {"start_sec": start_sec, "end_sec": end_sec, "quote": quote}
        )
    return tuple(valid_evidence), invalid_evidence


def _repair_primary_evidence(
    value: object, segments: Sequence[TranscriptSegment]
) -> object:
    """Repair only model timestamps when its quote is still verbatim in the transcript."""
    if not isinstance(value, list):
        return value
    repaired: list[object] = []
    for item in value:
        if not isinstance(item, dict):
            repaired.append(item)
            continue
        quote = item.get("quote")
        start = item.get("start_sec")
        end = item.get("end_sec")
        if (
            isinstance(quote, str)
            and isinstance(start, (int, float))
            and not isinstance(start, bool)
            and isinstance(end, (int, float))
            and not isinstance(end, bool)
            and evidence_is_locatable(quote, float(start), float(end), segments)
        ):
            repaired.append(item)
            continue
        located = _locate_verbatim_quote(quote, segments) if isinstance(quote, str) else None
        if located is None:
            repaired.append(item)
            continue
        repaired.append(
            {"start_sec": located[0], "end_sec": located[1], "quote": quote}
        )
    return repaired


def _locate_verbatim_quote(
    quote: str, segments: Sequence[TranscriptSegment]
) -> tuple[float, float] | None:
    needle = _normalize_whitespace(quote)
    if not needle:
        return None
    ordered = sorted(segments, key=lambda segment: segment.start_sec)
    for start_index, first in enumerate(ordered):
        combined = ""
        for segment in ordered[start_index:]:
            combined += segment.text
            normalized = _normalize_whitespace(combined)
            if needle in normalized:
                return first.start_sec, segment.end_sec
            if len(normalized) > len(needle) * 3 + 500:
                break
    return None


def evidence_is_locatable(
    quote: str,
    start_sec: float,
    end_sec: float,
    segments: Sequence[TranscriptSegment],
) -> bool:
    normalized_quote = _normalize_whitespace(quote)
    in_range_segments = sorted(
        (
            segment
            for segment in segments
            if start_sec <= segment.start_sec and segment.end_sec <= end_sec
        ),
        key=lambda segment: segment.start_sec,
    )
    if not in_range_segments:
        return False

    covered_until = start_sec
    transcript_text: list[str] = []
    for segment in in_range_segments:
        if segment.start_sec > covered_until:
            return False
        transcript_text.append(segment.text)
        covered_until = max(covered_until, segment.end_sec)

    return (
        covered_until >= end_sec
        and normalized_quote in _normalize_whitespace("".join(transcript_text))
    )


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", "", value)
