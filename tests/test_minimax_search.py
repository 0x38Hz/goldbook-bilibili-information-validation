import json

import httpx
import pytest

from goldbook.minimax_search import (
    MiniMaxWebSearchClient,
    SearchProviderError,
)


def _response(payload, status_code=200):
    return httpx.Response(status_code, json=payload)


def test_client_calls_official_search_endpoint_and_normalizes_results():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response(
            {
                "organic": [
                    {
                        "title": " CPI release ",
                        "link": "https://example.com/cpi",
                        "snippet": " actual 0.1% ",
                        "date": "2026-08-12",
                    }
                ],
                "related_searches": [],
                "base_resp": {"status_code": 0, "status_msg": "success"},
            }
        )

    client = MiniMaxWebSearchClient(
        "https://api.minimaxi.com/v1",
        "test-secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.search("  US   CPI August 12 2026  ")

    assert result[0].title == "CPI release"
    assert result[0].url == "https://example.com/cpi"
    assert result[0].snippet == "actual 0.1%"
    assert result[0].published_text == "2026-08-12"
    assert requests[0].url == "https://api.minimaxi.com/v1/coding_plan/search"
    assert requests[0].headers["Authorization"] == "Bearer test-secret"
    assert requests[0].headers["MM-API-Source"] == "Goldbook"
    assert json.loads(requests[0].content) == {"q": "US CPI August 12 2026"}


def test_client_retries_one_429_then_succeeds_without_exceeding_two_calls():
    calls = []

    def handler(_request):
        calls.append(1)
        if len(calls) == 1:
            return _response({"error": "rate limit"}, 429)
        return _response(
            {"organic": [], "base_resp": {"status_code": 0, "status_msg": "ok"}}
        )

    delays = []
    client = MiniMaxWebSearchClient(
        "https://api.minimaxi.com/v1",
        "test-secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=delays.append,
    )
    assert client.search("query") == ()
    assert len(calls) == 2
    assert delays == [1.0]


@pytest.mark.parametrize("query", ["", "   ", "x" * 301])
def test_client_rejects_empty_or_oversized_queries_without_network(query):
    client = MiniMaxWebSearchClient(
        "https://api.minimaxi.com/v1",
        "test-secret",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: pytest.fail("network must not be called")
            )
        ),
    )
    with pytest.raises(ValueError, match="1 to 300"):
        client.search(query)


def test_provider_errors_never_contain_key_or_remote_response_body():
    secret = "sk-private-never-print"

    def handler(_request):
        return _response(
            {
                "organic": [],
                "base_resp": {
                    "status_code": 1004,
                    "status_msg": f"invalid {secret}",
                },
            }
        )

    client = MiniMaxWebSearchClient(
        "https://api.minimaxi.com/v1",
        secret,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SearchProviderError, match="search provider unavailable") as error:
        client.search("query")
    assert secret not in str(error.value)
    assert "invalid" not in str(error.value)


def test_malformed_organic_results_fail_closed():
    client = MiniMaxWebSearchClient(
        "https://api.minimaxi.com/v1",
        "test-secret",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: _response(
                    {
                        "organic": [{"title": "missing URL", "snippet": "x"}],
                        "base_resp": {"status_code": 0, "status_msg": "ok"},
                    }
                )
            )
        ),
    )
    with pytest.raises(SearchProviderError, match="invalid search response"):
        client.search("query")


def test_realistic_long_search_snippets_are_bounded_instead_of_rejecting_results():
    client = MiniMaxWebSearchClient(
        "https://api.minimaxi.com/v1",
        "test-secret",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: _response(
                    {
                        "organic": [
                            {
                                "title": "CPI release",
                                "link": "https://example.com/cpi",
                                "snippet": "important evidence " * 300,
                                "date": "2026-08-12 20:30:00",
                            }
                        ],
                        "base_resp": {"status_code": 0, "status_msg": "ok"},
                    }
                )
            )
        ),
    )

    result = client.search("query")

    assert len(result) == 1
    assert len(result[0].snippet) == 2_000
    assert result[0].snippet.startswith("important evidence")


def test_blank_optional_search_date_is_normalized_to_none():
    client = MiniMaxWebSearchClient(
        "https://api.minimaxi.com/v1",
        "test-secret",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: _response(
                    {
                        "organic": [{"title": "CPI", "link": "https://example.com/cpi", "snippet": "release", "date": ""}],
                        "base_resp": {"status_code": 0, "status_msg": "ok"},
                    }
                )
            )
        ),
    )

    assert client.search("query")[0].published_text is None
