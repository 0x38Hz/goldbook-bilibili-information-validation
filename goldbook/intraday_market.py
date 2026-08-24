"""Strict one-hour XAU/USD market-data adapter."""

from datetime import datetime, timezone
import math
from typing import Any, Mapping

import httpx

from goldbook.models import IntradayPriceBar


_XAUS_CHART_URL = "https://xaus.com/api/v1/chart"
_PROVIDER_NAME = "XAUS (xaus.com; Yahoo Finance proxy)"


def parse_xaus_intraday_chart(payload: Mapping[str, Any]) -> list[IntradayPriceBar]:
    """Validate XAUS identity and preserve each chart point as a UTC hour."""
    if (
        payload.get("symbol") != "xau"
        or payload.get("label") != "Gold (XAU/USD)"
        or payload.get("currency") != "USD"
        or payload.get("interval") != "1h"
    ):
        raise ValueError("response is not hourly XAU/USD spot data")
    _validate_data_state(payload.get("data_state"))

    points = payload.get("points")
    if not isinstance(points, list):
        raise ValueError("missing hourly XAU/USD points")

    bars: list[IntradayPriceBar] = []
    seen: set[datetime] = set()
    for index, point in enumerate(points):
        if not isinstance(point, Mapping):
            raise ValueError(f"invalid hourly XAU/USD point {index}")
        try:
            started_at = _parse_timestamp(point.get("t"))
            bar = IntradayPriceBar(
                started_at=started_at,
                interval_minutes=60,
                open=_parse_positive(point, "o"),
                high=_parse_positive(point, "h"),
                low=_parse_positive(point, "l"),
                close=_parse_positive(point, "c"),
                provider=_PROVIDER_NAME,
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"invalid hourly XAU/USD point {index}: {exc}") from exc
        if started_at in seen:
            raise ValueError(f"duplicate hourly XAU/USD point: {started_at.isoformat()}")
        seen.add(started_at)
        bars.append(bar)

    return sorted(bars, key=lambda item: item.started_at)


class XausIntradayMarketDataSource:
    """Fetch strict one-hour XAU/USD spot OHLC with an injected HTTP client."""

    provider_name = _PROVIDER_NAME

    def __init__(self, client: httpx.Client):
        self._client = client

    def fetch(self, start: datetime, end: datetime) -> list[IntradayPriceBar]:
        _require_aware_range(start, end)
        response = self._client.get(
            _XAUS_CHART_URL,
            params={"symbol": "xau", "range": "1y", "interval": "1h"},
            timeout=20.0,
        )
        response.raise_for_status()
        return [
            bar
            for bar in parse_xaus_intraday_chart(response.json())
            if start <= bar.started_at <= end
        ]


def _require_aware_range(start: datetime, end: datetime) -> None:
    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("start and end must be timezone-aware")
    if end.tzinfo is None or end.utcoffset() is None:
        raise ValueError("start and end must be timezone-aware")
    if end < start:
        raise ValueError("end datetime must not precede start datetime")


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, bool):
        raise ValueError("timestamp must be numeric")
    timestamp = float(value)
    if not math.isfinite(timestamp) or timestamp <= 0:
        raise ValueError("timestamp must be positive")
    return datetime.fromtimestamp(timestamp, timezone.utc)


def _parse_positive(point: Mapping[str, Any], field: str) -> float:
    value = float(point[field])
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field} must be positive")
    return value


def _validate_data_state(value: object) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("response is not fresh upstream XAU/USD data")
    try:
        age_seconds = float(value["age_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("response is not fresh upstream XAU/USD data") from exc
    if (
        value.get("status") != "fresh"
        or value.get("source") != "upstream"
        or not math.isfinite(age_seconds)
        or age_seconds < 0
    ):
        raise ValueError("response is not fresh upstream XAU/USD data")
