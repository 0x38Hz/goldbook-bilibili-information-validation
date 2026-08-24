from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
import math


class Direction(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    NO_SIGNAL = "no_signal"


class ReviewStatus(str, Enum):
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    EXCLUDED = "excluded"


class VideoStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"


class Instrument(str, Enum):
    XAU_USD_SPOT = "xau_usd_spot"
    COMEX_GC = "comex_gc"
    SHFE_AU = "shfe_au"
    UNKNOWN = "unknown"


class ClaimType(str, Enum):
    DIRECTIONAL_MOVE = "directional_move"
    TARGET_TOUCH = "target_touch"
    CROSS_ABOVE = "cross_above"
    CROSS_BELOW = "cross_below"
    HOLD_ABOVE = "hold_above"
    HOLD_BELOW = "hold_below"
    RANGE = "range"
    VOLATILITY = "volatility"
    SEQUENCE = "sequence"
    BREAKOUT_EITHER_SIDE = "breakout_either_side"


class HorizonSource(str, Enum):
    EXPLICIT_EXACT = "explicit_exact"
    EXPLICIT_RELATIVE = "explicit_relative"
    CONTEXT_INFERRED = "context_inferred"
    UNKNOWN = "unknown"


class ClaimStatus(str, Enum):
    AUTO_VALIDATED = "auto_validated"
    UNRESOLVED = "unresolved"
    EXCLUDED = "excluded"
    HUMAN_CORRECTED = "human_corrected"


class EvaluationVerdict(str, Enum):
    HIT = "hit"
    PARTIAL_NEAR = "partial_near"
    MISS = "miss"
    UNRESOLVED = "unresolved"
    SUPERSEDED = "superseded"
    EXCLUDED = "excluded"
    NOT_TRIGGERED = "not_triggered"


@dataclass(frozen=True)
class Creator:
    uid: str
    name: str
    space_url: str
    enabled: bool = True
    synced_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_aware_datetime(self.synced_at, "synced_at")


@dataclass(frozen=True)
class Video:
    bvid: str
    creator_uid: str
    title: str
    published_at: datetime
    duration_sec: int
    url: str
    status: VideoStatus = VideoStatus.PENDING
    error_summary: str | None = None

    def __post_init__(self) -> None:
        _require_aware_datetime(self.published_at, "published_at")


@dataclass(frozen=True)
class TranscriptSegment:
    start_sec: float
    end_sec: float
    text: str
    bvid: str | None = None
    model: str | None = None
    text_hash: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        _require_aware_datetime(self.created_at, "created_at")


@dataclass(frozen=True)
class SignalAnalysis:
    direction: Direction
    strength: int
    confidence: float
    horizon_text: str | None = None
    target_price: float | None = None
    stop_price: float | None = None
    conditions: tuple[str, ...] = ()
    is_retrospective: bool = False
    is_news_only: bool = False
    evidence: tuple[dict[str, object], ...] = ()
    summary: str = ""
    review_status: ReviewStatus = ReviewStatus.NEEDS_REVIEW
    bvid: str | None = None
    transcript_hash: str | None = None
    raw_response_hash: str | None = None
    signal_json: str | None = None
    revision: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    model_name: str | None = None
    prompt_version: str | None = None

    def __post_init__(self) -> None:
        _require_aware_datetime(self.created_at, "created_at")


@dataclass(frozen=True)
class PriceBar:
    trade_date: date | str
    open: float
    high: float
    low: float
    close: float

    def __post_init__(self) -> None:
        if isinstance(self.trade_date, str):
            object.__setattr__(self, "trade_date", date.fromisoformat(self.trade_date))


@dataclass(frozen=True)
class IntradayPriceBar:
    started_at: datetime
    interval_minutes: int
    open: float
    high: float
    low: float
    close: float
    provider: str

    def __post_init__(self) -> None:
        _require_aware_datetime(self.started_at, "started_at")
        if self.interval_minutes != 60:
            raise ValueError("intraday interval must be 60 minutes")
        values = (self.open, self.high, self.low, self.close)
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("intraday OHLC values must be finite and positive")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("invalid intraday OHLC range")
        if not self.provider.strip():
            raise ValueError("intraday provider must not be empty")


@dataclass(frozen=True)
class ClaimLeg:
    operator: str
    level_low: float | None
    level_high: float | None


@dataclass(frozen=True)
class ForecastClaim:
    claim_id: str
    bvid: str
    analysis_revision: int
    claim_index: int
    instrument: Instrument
    claim_type: ClaimType
    direction: Direction | None
    legs: tuple[ClaimLeg, ...]
    condition_text: str
    horizon_text: str | None
    horizon_source: HorizonSource
    horizon_min_trading_days: int | None
    horizon_max_trading_days: int | None
    horizon_point_trading_days: int | None
    deadline_at: datetime | None
    time_confidence: float
    confidence: float
    evidence: tuple[dict[str, object], ...]
    supersedes_claim_id: str | None
    status: ClaimStatus
    model_name: str
    prompt_version: str
    transcript_hash: str

    def __post_init__(self) -> None:
        _require_aware_datetime(self.deadline_at, "deadline_at")


@dataclass(frozen=True)
class ClaimEvaluation:
    claim_id: str
    evaluated_at: datetime
    window_start: date | None
    window_end: date | None
    entry_price: float | None
    observed_min: float | None
    observed_max: float | None
    final_close: float | None
    closest_price: float | None
    closest_date: date | None
    distance_pct: float | None
    first_hit_date: date | None
    verdict: EvaluationVerdict
    mature: bool
    reason: str
    window_start_at: datetime | None = None
    window_end_at: datetime | None = None
    closest_at: datetime | None = None
    first_hit_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in (
            "evaluated_at",
            "window_start_at",
            "window_end_at",
            "closest_at",
            "first_hit_at",
        ):
            _require_aware_datetime(getattr(self, name), name)


@dataclass(frozen=True)
class Outcome:
    direction: Direction
    entry_date: date | str | None = None
    entry_price: float | None = None
    bvid: str | None = None
    exit_1d: float | None = None
    exit_5d: float | None = None
    exit_20d: float | None = None
    return_1d: float | None = None
    return_5d: float | None = None
    return_20d: float | None = None
    mature: bool = False
    signal_id: str | None = None
    review_status: ReviewStatus = ReviewStatus.NEEDS_REVIEW
    included: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.entry_date, str):
            object.__setattr__(self, "entry_date", date.fromisoformat(self.entry_date))


@dataclass(frozen=True)
class CreatorMetricSample:
    """Latest analysis and cached outcome data for one video in creator metrics."""

    bvid: str
    signal_id: str | None
    direction: Direction | None
    review_status: ReviewStatus | None
    included: bool
    mature: bool
    entry_price: float | None
    return_1d: float | None
    return_5d: float | None
    return_20d: float | None
    confidence: float | None
    manual_revision: bool
    disposition: str


def _require_aware_datetime(value: datetime | None, field_name: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field_name} must be timezone-aware")
