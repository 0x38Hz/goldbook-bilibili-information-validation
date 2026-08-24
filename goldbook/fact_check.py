"""Domain rules for source-backed checks of externally conditioned forecasts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import Enum
import ipaddress
import re
from typing import Iterable, Sequence
from urllib.parse import urlsplit

from goldbook.models import ForecastClaim, TranscriptSegment, Video


_CHINA = timezone(timedelta(hours=8))
_CONDITIONAL_PATTERN = re.compile(
    r"(?:如果|若|取决于|决定方向|公布后|发布后|利好|利空|不利好|不利空)",
    re.IGNORECASE,
)
_EXTERNAL_EVENT_PATTERN = re.compile(
    r"(?:(?:今晚|明天|今日|今天)\s*)?(?:美国\s*)?"
    r"(?:CPI|非农|FOMC|PCE|GDP|利率决议|财报|监管裁决|法院判决|选举结果|库存数据|就业数据|通胀数据)"
    r"(?:\s*(?:数据|公布|报告|结果))?",
    re.IGNORECASE,
)


class FactCheckImpact(str, Enum):
    SUPPORTIVE = "supportive"
    ADVERSE = "adverse"
    NEUTRAL = "neutral"
    CONFLICTING = "conflicting"
    INSUFFICIENT = "insufficient"


class FactCheckStatus(str, Enum):
    PENDING = "pending"
    SEARCHING = "searching"
    COMPLETED = "completed"
    FAILED = "failed"


class BranchPredicate(str, Enum):
    SUPPORTIVE = "supportive"
    ADVERSE = "adverse"
    NOT_SUPPORTIVE = "not_supportive"


class BranchStatus(str, Enum):
    TRIGGERED = "triggered"
    NOT_TRIGGERED = "not_triggered"


class FactCheckValidationError(ValueError):
    """A fact-check result is unsafe, ungrounded, or structurally invalid."""


@dataclass(frozen=True)
class FactCheckNeed:
    required: bool
    event_description: str | None
    expected_start: datetime | None
    expected_end: datetime | None
    claim_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class SearchEvidence:
    evidence_id: str
    query: str
    title: str
    url: str
    domain: str
    published_at: datetime | None
    snippet: str
    fetched_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.published_at, "published_at")
        _require_aware(self.fetched_at, "fetched_at")


@dataclass(frozen=True)
class FactValue:
    name: str
    actual: str | None
    forecast: str | None
    previous: str | None
    unit: str | None


@dataclass(frozen=True)
class BranchDecision:
    claim_id: str
    predicate: BranchPredicate
    status: BranchStatus
    reason: str


@dataclass(frozen=True)
class FactCheckResult:
    question: str
    event_name: str
    event_time_utc: datetime | None
    facts: tuple[FactValue, ...]
    impact: FactCheckImpact
    reasoning_summary: str
    evidence_ids: tuple[str, ...]
    branch_decisions: tuple[BranchDecision, ...]
    confidence: float

    def __post_init__(self) -> None:
        _require_aware(self.event_time_utc, "event_time_utc")


@dataclass(frozen=True)
class FactCheckRun:
    run_id: str
    bvid: str
    analysis_revision: int
    event_description: str
    status: FactCheckStatus
    model_name: str
    search_count: int
    error_summary: str | None
    created_at: datetime
    completed_at: datetime | None

    def __post_init__(self) -> None:
        _require_aware(self.created_at, "created_at")
        _require_aware(self.completed_at, "completed_at")


@dataclass(frozen=True)
class StoredFactCheck:
    run: FactCheckRun
    evidence: tuple[SearchEvidence, ...]
    result: FactCheckResult


def detect_fact_check_need(
    video: Video,
    claims: Sequence[ForecastClaim],
    segments: Sequence[TranscriptSegment],
) -> FactCheckNeed:
    """Identify external conditions without sending ordinary claims to the network."""
    del segments  # Claims already carry verbatim, locatable evidence from the transcript.
    matched: list[ForecastClaim] = []
    event_descriptions: list[str] = []
    for claim in claims:
        text = claim.condition_text
        event = _EXTERNAL_EVENT_PATTERN.search(text)
        if event is None or _CONDITIONAL_PATTERN.search(text) is None:
            continue
        matched.append(claim)
        event_descriptions.append(re.sub(r"\s+", "", event.group(0)))

    if not matched:
        return FactCheckNeed(False, None, None, None, (), "no_external_condition")

    description = event_descriptions[0]
    start, end = _expected_window(video.published_at, description)
    return FactCheckNeed(
        True,
        description,
        start,
        end,
        tuple(claim.claim_id for claim in matched),
        "external_condition_detected",
    )


def predicate_matches(predicate: BranchPredicate, impact: FactCheckImpact) -> bool:
    if predicate is BranchPredicate.NOT_SUPPORTIVE:
        return impact in {FactCheckImpact.ADVERSE, FactCheckImpact.NEUTRAL}
    return predicate.value == impact.value


def validate_search_evidence(value: SearchEvidence) -> SearchEvidence:
    if not value.evidence_id.strip() or not value.query.strip():
        raise FactCheckValidationError("evidence identity and query are required")
    if not value.title.strip() or not value.snippet.strip():
        raise FactCheckValidationError("evidence title and snippet are required")
    if len(value.title) > 300 or len(value.snippet) > 2_000:
        raise FactCheckValidationError("evidence text exceeds the safe limit")

    parsed = urlsplit(value.url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise FactCheckValidationError("evidence URL must be public HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise FactCheckValidationError("evidence URL must not contain credentials")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise FactCheckValidationError("evidence URL must not target localhost")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise FactCheckValidationError("evidence URL must be globally routable")
    if value.domain.lower().rstrip(".") != hostname:
        raise FactCheckValidationError("evidence domain does not match its URL")
    return value


def validate_fact_check_result(
    result: FactCheckResult,
    evidence: Iterable[SearchEvidence],
    *,
    current_claim_ids: set[str],
) -> FactCheckResult:
    evidence_by_id: dict[str, SearchEvidence] = {}
    for item in evidence:
        checked = validate_search_evidence(item)
        if checked.evidence_id in evidence_by_id:
            raise FactCheckValidationError("duplicate evidence ID")
        evidence_by_id[checked.evidence_id] = checked

    if not result.question.strip() or not result.event_name.strip():
        raise FactCheckValidationError("fact-check question and event are required")
    if not result.reasoning_summary.strip():
        raise FactCheckValidationError("fact-check reasoning summary is required")
    if not 0.0 <= result.confidence <= 1.0:
        raise FactCheckValidationError("fact-check confidence must be between 0 and 1")
    if len(set(result.evidence_ids)) != len(result.evidence_ids):
        raise FactCheckValidationError("duplicate cited evidence ID")
    try:
        cited = tuple(evidence_by_id[item] for item in result.evidence_ids)
    except KeyError as error:
        raise FactCheckValidationError("result cites unknown evidence") from error

    if result.impact is not FactCheckImpact.INSUFFICIENT:
        domains = {item.domain.lower() for item in cited}
        if len(domains) < 2:
            raise FactCheckValidationError("resolved facts require two independent sources")
        if not result.facts:
            raise FactCheckValidationError("resolved fact check requires structured facts")

    decided_claim_ids: set[str] = set()
    for decision in result.branch_decisions:
        if decision.claim_id not in current_claim_ids:
            raise FactCheckValidationError("branch decision references an unknown claim")
        if decision.claim_id in decided_claim_ids:
            raise FactCheckValidationError("duplicate branch decision")
        if not decision.reason.strip():
            raise FactCheckValidationError("branch decision reason is required")
        if result.impact not in {FactCheckImpact.CONFLICTING, FactCheckImpact.INSUFFICIENT}:
            expected = (
                BranchStatus.TRIGGERED
                if predicate_matches(decision.predicate, result.impact)
                else BranchStatus.NOT_TRIGGERED
            )
            if decision.status is not expected:
                raise FactCheckValidationError("branch status contradicts the fact impact")
        decided_claim_ids.add(decision.claim_id)
    return result


def _expected_window(
    published_at: datetime, description: str
) -> tuple[datetime | None, datetime | None]:
    local_day = published_at.astimezone(_CHINA).date()
    if "今晚" in description:
        start = datetime.combine(local_day, time(18, 0), _CHINA)
        end = datetime.combine(local_day, time(23, 59, 59), _CHINA)
    elif "明天" in description:
        tomorrow = local_day + timedelta(days=1)
        start = datetime.combine(tomorrow, time(0, 0), _CHINA)
        end = datetime.combine(tomorrow, time(23, 59, 59), _CHINA)
    elif "今日" in description or "今天" in description:
        start = datetime.combine(local_day, time(0, 0), _CHINA)
        end = datetime.combine(local_day, time(23, 59, 59), _CHINA)
    else:
        return None, None
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _require_aware(value: datetime | None, field: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field} must be timezone-aware")
