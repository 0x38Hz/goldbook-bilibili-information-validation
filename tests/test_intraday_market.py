from datetime import datetime, timezone

import httpx
import pytest

from goldbook.intraday_market import (
    XausIntradayMarketDataSource,
    parse_xaus_intraday_chart,
)
from goldbook.models import IntradayPriceBar


def _valid_hourly_payload() -> dict[str, object]:
    return {
        "symbol": "xau",
        "label": "Gold (XAU/USD)",
        "currency": "USD",
        "interval": "1h",
        "data_state": {"status": "fresh", "source": "upstream", "age_seconds": 0},
        "points": [
            {"t": 1787302800, "o": 4400, "h": 4412, "l": 4398, "c": 4410},
        ],
    }


def test_parse_xaus_intraday_chart_preserves_utc_hours():
    bars = parse_xaus_intraday_chart(_valid_hourly_payload())

    assert bars == [
        IntradayPriceBar(
            datetime.fromtimestamp(1787302800, timezone.utc),
            60,
            4400.0,
            4412.0,
            4398.0,
            4410.0,
            "XAUS (xaus.com; Yahoo Finance proxy)",
        )
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [("symbol", "GC=F"), ("currency", "CNY"), ("interval", "1d")],
)
def test_parse_xaus_intraday_chart_rejects_wrong_market_identity(field, value):
    payload = _valid_hourly_payload()
    payload[field] = value

    with pytest.raises(ValueError, match="hourly XAU/USD"):
        parse_xaus_intraday_chart(payload)


def test_parse_xaus_intraday_chart_rejects_stale_or_invalid_points():
    stale = _valid_hourly_payload()
    stale["data_state"] = {
        "status": "stale",
        "source": "cache",
        "age_seconds": 3600,
    }
    with pytest.raises(ValueError, match="fresh upstream"):
        parse_xaus_intraday_chart(stale)

    invalid = _valid_hourly_payload()
    invalid["points"] = [
        {"t": 1787302800, "o": 4400, "h": 4399, "l": 4398, "c": 4410}
    ]
    with pytest.raises(ValueError, match="range"):
        parse_xaus_intraday_chart(invalid)


def test_fetches_hourly_chart_and_filters_by_aware_utc_range():
    payload = _valid_hourly_payload()
    payload["points"] = [
        {"t": 1787299200, "o": 4390, "h": 4402, "l": 4388, "c": 4400},
        {"t": 1787302800, "o": 4400, "h": 4412, "l": 4398, "c": 4410},
    ]
    seen_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_request
        seen_request = request
        return httpx.Response(200, json=payload)

    start = datetime.fromtimestamp(1787301000, timezone.utc)
    end = datetime.fromtimestamp(1787304600, timezone.utc)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        bars = XausIntradayMarketDataSource(client).fetch(start, end)

    assert [bar.started_at for bar in bars] == [
        datetime.fromtimestamp(1787302800, timezone.utc)
    ]
    assert seen_request is not None
    assert seen_request.url.params["symbol"] == "xau"
    assert seen_request.url.params["range"] == "1y"
    assert seen_request.url.params["interval"] == "1h"


def test_intraday_bar_and_fetch_reject_naive_datetimes():
    with pytest.raises(ValueError, match="started_at must be timezone-aware"):
        IntradayPriceBar(datetime(2026, 8, 21, 10), 60, 4400, 4410, 4390, 4405, "XAUS")

    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as client:
        with pytest.raises(ValueError, match="timezone-aware"):
            XausIntradayMarketDataSource(client).fetch(
                datetime(2026, 8, 21, 10),
                datetime(2026, 8, 21, 11, tzinfo=timezone.utc),
            )
