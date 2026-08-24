"""M3-orchestrated, source-cited web fact checking."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from hashlib import sha256
import json
from typing import Any, Protocol
from urllib.parse import urlsplit

from goldbook.fact_check import (
    BranchDecision,
    BranchPredicate,
    BranchStatus,
    FactCheckImpact,
    FactCheckNeed,
    FactCheckResult,
    FactCheckValidationError,
    FactValue,
    SearchEvidence,
    validate_fact_check_result,
    validate_search_evidence,
)
from goldbook.minimax_search import SearchProviderError, SearchResult, WebSearchProvider
from goldbook.models import ForecastClaim, TranscriptSegment, Video


MAX_SEARCHES = 6
MAX_SEARCH_CONCURRENCY = 3
MAX_AGENT_ROUNDS = 4
_MAX_RESULTS_PER_QUERY = 5


class FactCheckAgentError(RuntimeError):
    """A safe workflow failure that can be persisted and retried."""


class FactCheckModel(Protocol):
    def complete_fact_check(
        self, messages: Sequence[Mapping[str, str]]
    ) -> Mapping[str, object]:
        raise NotImplementedError


@dataclass(frozen=True)
class FactCheckBundle:
    evidence: tuple[SearchEvidence, ...]
    result: FactCheckResult
    search_count: int


class M3FactCheckAgent:
    def __init__(
        self,
        model: FactCheckModel,
        search_provider: WebSearchProvider,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._model = model
        self._search = search_provider
        self._clock = clock

    def run(
        self,
        video: Video,
        need: FactCheckNeed,
        claims: Sequence[ForecastClaim],
        segments: Sequence[TranscriptSegment],
    ) -> FactCheckBundle:
        if not need.required:
            raise ValueError("fact-check agent requires an external condition")
        current_claim_ids = {claim.claim_id for claim in claims}
        if not set(need.claim_ids).issubset(current_claim_ids):
            raise ValueError("fact-check need references an unknown claim")

        messages: list[dict[str, str]] = [
            {"role": "system", "content": _SYSTEM_INSTRUCTION},
            {
                "role": "user",
                "content": json.dumps(
                    _initial_context(video, need, claims, segments),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]
        evidence_by_id: dict[str, SearchEvidence] = {}
        seen_queries: set[str] = set()
        search_count = 0

        for _round_index in range(MAX_AGENT_ROUNDS):
            reply = self._model.complete_fact_check(tuple(messages))
            status = reply.get("status") if isinstance(reply, Mapping) else None
            if status == "complete":
                try:
                    result = _parse_result(reply.get("result"))
                    checked = validate_fact_check_result(
                        result,
                        evidence_by_id.values(),
                        current_claim_ids=current_claim_ids,
                    )
                    _validate_expected_predicates(checked, claims, need.claim_ids)
                except (FactCheckAgentError, FactCheckValidationError) as error:
                    if isinstance(error, FactCheckValidationError) and not str(
                        error
                    ).startswith("branch "):
                        raise
                    messages.append(
                        {
                            "role": "assistant",
                            "content": json.dumps(
                                reply, ensure_ascii=False, separators=(",", ":")
                            ),
                        }
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "error": "schema_validation_failed",
                                    "instruction": "Return status=complete again using this exact schema and only saved evidence IDs.",
                                    "result_schema": _RESULT_SCHEMA,
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        }
                    )
                    continue
                return FactCheckBundle(
                    tuple(evidence_by_id.values()), checked, search_count
                )
            if status != "search":
                raise FactCheckAgentError("invalid M3 fact-check response")

            queries = _parse_queries(reply.get("queries"))
            unique = []
            for query in queries:
                normalized = " ".join(query.split())
                if normalized not in seen_queries:
                    unique.append(normalized)
                    seen_queries.add(normalized)
            if not unique:
                raise FactCheckAgentError("M3 repeated every fact-check search")
            if search_count + len(unique) > MAX_SEARCHES:
                raise FactCheckAgentError("fact-check search limit exceeded")

            try:
                batches = _parallel_search(self._search, unique)
            except SearchProviderError:
                raise FactCheckAgentError("search provider unavailable") from None
            search_count += len(unique)
            for query, results in zip(unique, batches, strict=True):
                for result in results[:_MAX_RESULTS_PER_QUERY]:
                    evidence = _normalize_evidence(query, result, self._clock())
                    try:
                        validate_search_evidence(evidence)
                    except FactCheckValidationError:
                        continue
                    evidence_by_id.setdefault(evidence.evidence_id, evidence)

            messages.append(
                {
                    "role": "assistant",
                    "content": json.dumps(reply, ensure_ascii=False, separators=(",", ":")),
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "tool": "web_search",
                            "evidence": [
                                _evidence_payload(item)
                                for item in evidence_by_id.values()
                            ],
                            "searches_used": search_count,
                            "searches_remaining": MAX_SEARCHES - search_count,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                }
            )
        raise FactCheckAgentError("M3 fact-check round limit exceeded")


def _parallel_search(
    provider: WebSearchProvider, queries: Sequence[str]
) -> tuple[tuple[SearchResult, ...], ...]:
    with ThreadPoolExecutor(max_workers=MAX_SEARCH_CONCURRENCY) as executor:
        return tuple(executor.map(provider.search, queries))


def _parse_queries(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise FactCheckAgentError("invalid M3 search request")
    parsed: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise FactCheckAgentError("invalid M3 search request")
        normalized = " ".join(item.split())
        if not normalized or len(normalized) > 300:
            raise FactCheckAgentError("invalid M3 search request")
        parsed.append(normalized)
    return tuple(parsed)


def _parse_result(value: object) -> FactCheckResult:
    if not isinstance(value, Mapping):
        raise FactCheckAgentError("invalid M3 fact-check result")
    try:
        facts = tuple(
            FactValue(
                name=_required_string(item, "name"),
                actual=_optional_string(item.get("actual")),
                forecast=_optional_string(item.get("forecast")),
                previous=_optional_string(item.get("previous")),
                unit=_optional_string(item.get("unit")),
            )
            for item in _mapping_list(value.get("facts"), "facts")
        )
        decisions = tuple(
            BranchDecision(
                claim_id=_required_string(item, "claim_id"),
                predicate=BranchPredicate(_required_string(item, "predicate")),
                status=BranchStatus(_required_string(item, "status")),
                reason=_required_string(item, "reason"),
            )
            for item in _mapping_list(value.get("branch_decisions"), "branch_decisions")
        )
        evidence_ids_raw = value.get("evidence_ids")
        if not isinstance(evidence_ids_raw, list) or not all(
            isinstance(item, str) and item.strip() for item in evidence_ids_raw
        ):
            raise ValueError("invalid evidence IDs")
        return FactCheckResult(
            question=_required_string(value, "question"),
            event_name=_required_string(value, "event_name"),
            event_time_utc=_optional_datetime(value.get("event_time_utc")),
            facts=facts,
            impact=FactCheckImpact(_required_string(value, "impact")),
            reasoning_summary=_required_string(value, "reasoning_summary"),
            evidence_ids=tuple(item.strip() for item in evidence_ids_raw),
            branch_decisions=decisions,
            confidence=float(value["confidence"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise FactCheckAgentError("invalid M3 fact-check result") from error


def _normalize_evidence(
    query: str, result: SearchResult, fetched_at: datetime
) -> SearchEvidence:
    parsed = urlsplit(result.url)
    identity = sha256(f"{query}\n{result.url}".encode("utf-8")).hexdigest()[:20]
    return SearchEvidence(
        evidence_id=f"ev-{identity}",
        query=query,
        title=result.title,
        url=result.url,
        domain=(parsed.hostname or "").lower().rstrip("."),
        published_at=_parse_published_text(result.published_text),
        snippet=result.snippet,
        fetched_at=fetched_at,
    )


def _parse_published_text(value: str | None) -> datetime | None:
    if value is None or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed_date = date.fromisoformat(text)
        except ValueError:
            return None
        return datetime.combine(parsed_date, time(0, 0), timezone.utc)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mapping_list(value: object, field: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    if not all(isinstance(item, Mapping) for item in value):
        raise ValueError(f"{field} must contain objects")
    return tuple(value)


def _required_string(value: Mapping[str, object], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip() or len(item) > 2_000:
        raise ValueError(f"invalid {field}")
    return item.strip()


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 200:
        raise ValueError("invalid optional fact value")
    return value.strip() or None


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid event time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("event time must include a timezone")
    return parsed.astimezone(timezone.utc)


def _initial_context(
    video: Video,
    need: FactCheckNeed,
    claims: Sequence[ForecastClaim],
    segments: Sequence[TranscriptSegment],
) -> dict[str, object]:
    relevant_ids = set(need.claim_ids)
    return {
        "task": "fact_check_external_condition",
        "video": {
            "bvid": video.bvid,
            "title": video.title,
            "published_at": video.published_at.isoformat(),
        },
        "event_description": need.event_description,
        "expected_start": need.expected_start.isoformat() if need.expected_start else None,
        "expected_end": need.expected_end.isoformat() if need.expected_end else None,
        "claims": [
            {
                "claim_id": claim.claim_id,
                "condition_text": claim.condition_text,
                "claim_type": claim.claim_type.value,
                "instrument": claim.instrument.value,
                "expected_predicate": _expected_predicate(claim),
                "legs": [
                    {
                        "operator": leg.operator,
                        "level_low": leg.level_low,
                        "level_high": leg.level_high,
                    }
                    for leg in claim.legs
                ],
            }
            for claim in claims
            if claim.claim_id in relevant_ids
        ],
        "transcript_evidence": [
            {
                "start_sec": segment.start_sec,
                "end_sec": segment.end_sec,
                "text": segment.text,
            }
            for segment in segments
        ],
    }


def _evidence_payload(value: SearchEvidence) -> dict[str, object]:
    return {
        "evidence_id": value.evidence_id,
        "query": value.query,
        "title": value.title,
        "url": value.url,
        "domain": value.domain,
        "published_at": value.published_at.isoformat() if value.published_at else None,
        "snippet": value.snippet,
    }


def _expected_predicate(claim: ForecastClaim) -> str | None:
    text = claim.condition_text.replace(" ", "")
    has_not_supportive = any(marker in text for marker in ("不利好", "未利好"))
    has_supportive = "利好" in text and not has_not_supportive
    has_adverse = any(marker in text for marker in ("利空", "不利")) and not has_not_supportive
    if has_not_supportive:
        return BranchPredicate.NOT_SUPPORTIVE.value
    if has_supportive:
        return BranchPredicate.SUPPORTIVE.value
    if has_adverse:
        return BranchPredicate.ADVERSE.value
    return None


def _validate_expected_predicates(
    result: FactCheckResult,
    claims: Sequence[ForecastClaim],
    relevant_ids: Sequence[str],
) -> None:
    if result.impact in {FactCheckImpact.CONFLICTING, FactCheckImpact.INSUFFICIENT}:
        return
    relevant = set(relevant_ids)
    expected = {
        claim.claim_id: predicate
        for claim in claims
        if claim.claim_id in relevant
        and (predicate := _expected_predicate(claim)) is not None
    }
    actual = {decision.claim_id: decision.predicate.value for decision in result.branch_decisions}
    for claim_id, predicate in expected.items():
        if actual.get(claim_id) != predicate:
            raise FactCheckValidationError(
                "branch predicate contradicts claim condition"
            )


_RESULT_SCHEMA = {
    "question": "string",
    "event_name": "string",
    "event_time_utc": "ISO-8601 string with timezone, or null",
    "facts": [
        {
            "name": "string",
            "actual": "string or null",
            "forecast": "string or null",
            "previous": "string or null",
            "unit": "string or null",
        }
    ],
    "impact": "supportive|adverse|neutral|conflicting|insufficient",
    "reasoning_summary": "string",
    "evidence_ids": ["saved evidence_id"],
    "branch_decisions": [
        {
            "claim_id": "current claim_id",
            "predicate": "supportive|adverse|not_supportive",
            "status": "triggered|not_triggered",
            "reason": "string",
        }
    ],
    "confidence": "number from 0 to 1",
}


_SYSTEM_INSTRUCTION = """你是联网事实核查代理，只能依据 web_search 返回的证据判断视频发布后的外部事件。
若需要搜索，返回 {"status":"search","queries":["..."]}。每轮只给互相独立且必要的查询，总计不得超过 6 个。
获得证据后可以继续搜索，或返回 {"status":"complete","result":{...}}。
只搜索并判断外部事件本身的 actual/forecast/previous；不搜索或判断事件后的黄金价格，不得因不同时点的金价不同而标记 conflicting。
每条 claim 的 expected_predicate 由原话确定，branch_decisions 必须原样使用；null 表示复合叙述，不要为它生成分支决定。
完成结果必须严格符合以下 JSON schema：""" + json.dumps(
    _RESULT_SCHEMA, ensure_ascii=False, separators=(",", ":")
) + """
impact 只能是 supportive、adverse、neutral、conflicting、insufficient。每个 facts 项分别保留 actual、forecast、previous、unit，不得混合 headline/core 或月率/年率。
每个事实必须引用输入中的 evidence_id；核心结论应有两个独立域名。证据矛盾用 conflicting，证据不足用 insufficient，禁止凭常识补值。
branch_decisions 中 predicate 只能是 supportive、adverse、not_supportive；status 只能是 triggered、not_triggered。neutral 会触发 not_supportive，但不会触发 adverse。条件未触发不是预测未命中。
不要输出思维过程、密钥、Authorization 或搜索工具原始错误。"""
