from datetime import date
from pathlib import Path

import httpx
import pytest

from goldbook.market import (
    FallbackMarketDataSource,
    StooqMarketDataSource,
    XausMarketDataSource,
    parse_xaus_chart,
    parse_stooq_csv,
)


FIXTURE = Path(__file__).parent / "fixtures" / "xauusd_daily.csv"


def test_parses_and_sorts_valid_daily_bars():
    bars = parse_stooq_csv(FIXTURE.read_text(encoding="utf-8"))

    assert [bar.trade_date.isoformat() for bar in bars] == [
        "2026-08-03",
        "2026-08-04",
        "2026-08-05",
    ]
    assert bars[1].open == 2410.0


def test_rejects_duplicate_dates():
    csv_text = (
        "Date,Open,High,Low,Close\n"
        "2026-08-01,2400,2420,2390,2410\n"
        "2026-08-01,2401,2421,2391,2411\n"
    )

    with pytest.raises(ValueError, match="duplicate"):
        parse_stooq_csv(csv_text)


@pytest.mark.parametrize(
    "csv_text",
    [
        "Date,Open,High,Low\n2026-08-01,2400,2420,2390\n",
        "Date,Open,High,Low,Close\n2026-08-01,0,2420,2390,2410\n",
        "Date,Open,High,Low,Close\n2026-08-01,2400,2380,2390,2410\n",
        "Date,Open,High,Low,Close,Ignored\n2026-08-01,2400,2420,2390,2410,x\n",
        "Date,Open,High,Low,Close,Close\n2026-08-01,2400,2420,2390,2410,2410\n",
        "Date,Open,High,Low,Close\n2026-08-01,2400,2420,2390,2410,extra\n",
        "Date,Open,High,Low,Close\nnot-a-date,2400,2420,2390,2410\n",
        "Date,Open,High,Low,Close\n2026-08-01,,2420,2390,2410\n",
        "Date,Open,High,Low,Close\n2026-08-01,nan,2420,2390,2410\n",
    ],
)
def test_rejects_noncanonical_schema_or_invalid_daily_rows(csv_text):
    with pytest.raises(ValueError):
        parse_stooq_csv(csv_text)


def test_fetches_daily_stooq_csv_and_filters_requested_range():
    seen_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_request
        seen_request = request
        return httpx.Response(200, text=FIXTURE.read_text(encoding="utf-8"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        bars = StooqMarketDataSource(client).fetch(date(2026, 8, 4), date(2026, 8, 4))

    assert [bar.trade_date for bar in bars] == [date(2026, 8, 4)]
    assert seen_request is not None
    assert seen_request.url.params["s"] == "xauusd"
    assert seen_request.url.params["i"] == "d"


def test_falls_back_to_xaus_spot_ohlc_when_stooq_is_unavailable():
    """Catches a regression that lets Stooq's bot/HTTP failure abort a refresh."""
    seen_requests: list[httpx.Request] = []
    xaus_payload = {
        "symbol": "xau",
        "label": "Gold (XAU/USD)",
        "currency": "USD",
        "interval": "1d",
        "data_state": {"status": "fresh", "source": "upstream", "age_seconds": 0},
        "points": [
            {"t": 1787184000, "o": 4500.0, "h": 4520.0, "l": 4490.0, "c": 4510.0, "v": 10},
            {"t": 1787270400, "o": 4511.0, "h": 4530.0, "l": 4500.0, "c": 4525.0, "v": 11},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        if request.url.host == "stooq.com":
            return httpx.Response(404, text="unavailable")
        if request.url.host == "xaus.com":
            return httpx.Response(200, json=xaus_payload)
        raise AssertionError(f"unexpected request: {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        source = FallbackMarketDataSource(
            StooqMarketDataSource(client), XausMarketDataSource(client)
        )
        bars = source.fetch(date(2026, 8, 20), date(2026, 8, 21))

    assert [(bar.trade_date, bar.open, bar.high, bar.low, bar.close) for bar in bars] == [
        (date(2026, 8, 20), 4500.0, 4520.0, 4490.0, 4510.0),
        (date(2026, 8, 21), 4511.0, 4530.0, 4500.0, 4525.0),
    ]
    assert seen_requests[0].url.host == "stooq.com"
    assert seen_requests[1].url.host == "xaus.com"
    assert seen_requests[1].url.params["symbol"] == "xau"
    assert seen_requests[1].url.params["interval"] == "1d"
    assert source.provider_name == "XAUS (xaus.com; Yahoo Finance proxy)"


def test_rejects_chart_data_that_is_not_identified_as_xauusd_spot():
    """Catches an accidental switch from spot XAU/USD to GC futures data."""
    futures_payload = {
        "symbol": "gc",
        "label": "Gold Futures (GC=F)",
        "currency": "USD",
        "interval": "1d",
        "points": [{"t": 1787184000, "o": 4500.0, "h": 4520.0, "l": 4490.0, "c": 4510.0}],
    }

    with pytest.raises(ValueError, match="not daily XAU/USD spot"):
        parse_xaus_chart(futures_payload)


def test_rejects_stale_or_unattributed_xaus_chart_data():
    """Catches an outage cache being silently scored as fresh market evidence."""
    stale_payload = {
        "symbol": "xau",
        "label": "Gold (XAU/USD)",
        "currency": "USD",
        "interval": "1d",
        "data_state": {"status": "stale", "source": "cache", "age_seconds": 86400},
        "points": [{"t": 1787184000, "o": 4500.0, "h": 4520.0, "l": 4490.0, "c": 4510.0}],
    }

    with pytest.raises(ValueError, match="not fresh upstream XAU/USD data"):
        parse_xaus_chart(stale_payload)
