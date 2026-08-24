"""Daily XAU/USD market-data adapters."""

import csv
from datetime import date, datetime, timezone
from io import StringIO
import math
from typing import Any, Mapping, Protocol

import httpx

from goldbook.models import PriceBar


_DAILY_COLUMNS = ("Date", "Open", "High", "Low", "Close")
_STOOQ_DAILY_URL = "https://stooq.com/q/d/l/"
_XAUS_CHART_URL = "https://xaus.com/api/v1/chart"


class MarketDataSource(Protocol):
    provider_name: str

    def fetch(self, start: date, end: date) -> list[PriceBar]: ...


def parse_stooq_csv(csv_text: str) -> list[PriceBar]:
    """Parse and validate daily XAU/USD bars returned by Stooq."""
    reader = csv.DictReader(StringIO(csv_text))
    if reader.fieldnames != list(_DAILY_COLUMNS):
        raise ValueError("missing required daily price columns")

    bars: list[PriceBar] = []
    seen_dates: set[date] = set()
    for line_number, row in enumerate(reader, start=2):
        if None in row:
            raise ValueError(f"too many values in daily price row {line_number}")
        try:
            trade_date = date.fromisoformat(_required_value(row, "Date"))
            open_price = _parse_positive_value(row, "Open")
            high = _parse_positive_value(row, "High")
            low = _parse_positive_value(row, "Low")
            close = _parse_positive_value(row, "Close")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid daily price row {line_number}") from exc

        if trade_date in seen_dates:
            raise ValueError(f"duplicate daily price date: {trade_date.isoformat()}")
        if high < low:
            raise ValueError(f"invalid daily price range at {trade_date.isoformat()}")

        seen_dates.add(trade_date)
        bars.append(PriceBar(trade_date, open_price, high, low, close))

    return sorted(bars, key=lambda bar: bar.trade_date)


class StooqMarketDataSource:
    """Fetch daily XAU/USD prices from Stooq using an injected HTTP client."""

    provider_name = "Stooq (stooq.com)"

    def __init__(self, client: httpx.Client):
        self._client = client

    def fetch(self, start: date, end: date) -> list[PriceBar]:
        if end < start:
            raise ValueError("end date must not precede start date")
        response = self._client.get(
            _STOOQ_DAILY_URL,
            params={"s": "xauusd", "i": "d"},
            timeout=20.0,
        )
        response.raise_for_status()
        return [
            bar
            for bar in parse_stooq_csv(response.text)
            if start <= bar.trade_date <= end
        ]


class XausMarketDataSource:
    """Fetch daily XAU/USD spot OHLC from the keyless XAUS chart endpoint."""

    provider_name = "XAUS (xaus.com; Yahoo Finance proxy)"

    def __init__(self, client: httpx.Client):
        self._client = client

    def fetch(self, start: date, end: date) -> list[PriceBar]:
        if end < start:
            raise ValueError("end date must not precede start date")
        response = self._client.get(
            _XAUS_CHART_URL,
            params={"symbol": "xau", "range": "1y", "interval": "1d"},
            timeout=20.0,
        )
        response.raise_for_status()
        return [
            bar
            for bar in parse_xaus_chart(response.json())
            if start <= bar.trade_date <= end
        ]


class FallbackMarketDataSource:
    """Use the secondary source only when the primary cannot provide valid data."""

    def __init__(self, primary: MarketDataSource, secondary: MarketDataSource):
        self._primary = primary
        self._secondary = secondary
        self.provider_name = "unresolved market provider"

    def fetch(self, start: date, end: date) -> list[PriceBar]:
        try:
            bars = self._primary.fetch(start, end)
        except (httpx.HTTPError, ValueError):
            bars = self._secondary.fetch(start, end)
            self.provider_name = _provider_name(self._secondary)
            return bars
        self.provider_name = _provider_name(self._primary)
        return bars


def parse_xaus_chart(payload: Mapping[str, Any]) -> list[PriceBar]:
    """Validate daily XAU/USD spot OHLC returned by the XAUS chart API."""
    if (
        payload.get("symbol") != "xau"
        or payload.get("label") != "Gold (XAU/USD)"
        or payload.get("currency") != "USD"
        or payload.get("interval") != "1d"
    ):
        raise ValueError("response is not daily XAU/USD spot data")
    _validate_xaus_data_state(payload.get("data_state"))

    points = payload.get("points")
    if not isinstance(points, list):
        raise ValueError("missing daily XAU/USD points")

    bars: list[PriceBar] = []
    seen_dates: set[date] = set()
    for index, point in enumerate(points):
        if not isinstance(point, Mapping):
            raise ValueError(f"invalid XAU/USD point {index}")
        try:
            trade_date = _parse_timestamp_date(point.get("t"))
            open_price = _parse_finite_positive_value(point, "o")
            high = _parse_finite_positive_value(point, "h")
            low = _parse_finite_positive_value(point, "l")
            close = _parse_finite_positive_value(point, "c")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"invalid XAU/USD point {index}") from exc

        if trade_date in seen_dates:
            raise ValueError(f"duplicate XAU/USD point date: {trade_date.isoformat()}")
        if low > min(open_price, close) or high < max(open_price, close):
            raise ValueError(f"invalid XAU/USD point range at {trade_date.isoformat()}")

        seen_dates.add(trade_date)
        bars.append(PriceBar(trade_date, open_price, high, low, close))

    return sorted(bars, key=lambda bar: bar.trade_date)


def _required_value(row: dict[str, str | None], field: str) -> str:
    value = row.get(field)
    if value is None or not value.strip():
        raise ValueError(f"missing {field}")
    return value


def _parse_positive_value(row: dict[str, str | None], field: str) -> float:
    value = float(_required_value(row, field))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field} must be positive")
    return value


def _parse_timestamp_date(value: object) -> date:
    if isinstance(value, bool):
        raise ValueError("timestamp must be numeric")
    timestamp = float(value)
    if not math.isfinite(timestamp) or timestamp <= 0:
        raise ValueError("timestamp must be positive")
    return datetime.fromtimestamp(timestamp, timezone.utc).date()


def _parse_finite_positive_value(point: Mapping[str, Any], field: str) -> float:
    value = float(point[field])
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field} must be positive")
    return value


def _validate_xaus_data_state(value: object) -> None:
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


def _provider_name(source: object) -> str:
    name = getattr(source, "provider_name", None)
    return name if isinstance(name, str) and name else type(source).__name__
