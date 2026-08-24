"""Small, auditable client for MiniMax Coding Plan's generic web search."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable, Mapping, Protocol

import httpx


_SEARCH_TIMEOUT_SECONDS = 20.0
_SEARCH_ATTEMPTS = 2
_MAX_RESULTS = 10


class SearchProviderError(RuntimeError):
    """A fixed, retry-safe error that never contains remote or credential data."""


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    published_text: str | None = None


class WebSearchProvider(Protocol):
    def search(self, query: str) -> tuple[SearchResult, ...]:
        raise NotImplementedError


class MiniMaxWebSearchClient:
    """Call the same generic search endpoint used by MiniMax's official MCP."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("MINIMAX_API_KEY is required for web search")
        normalized_base = base_url.rstrip("/")
        if not normalized_base.startswith("https://"):
            raise ValueError("MiniMax search base URL must use HTTPS")
        self._url = f"{normalized_base}/coding_plan/search"
        self._api_key = api_key
        self._http_client = http_client or httpx.Client()
        self._sleep = sleep

    def search(self, query: str) -> tuple[SearchResult, ...]:
        normalized = " ".join(query.split())
        if not normalized or len(normalized) > 300:
            raise ValueError("search query must contain 1 to 300 characters")

        for attempt in range(_SEARCH_ATTEMPTS):
            try:
                response = self._http_client.post(
                    self._url,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "MM-API-Source": "Goldbook",
                    },
                    json={"q": normalized},
                    timeout=_SEARCH_TIMEOUT_SECONDS,
                )
            except httpx.TransportError:
                if attempt + 1 == _SEARCH_ATTEMPTS:
                    raise SearchProviderError("MiniMax search provider unavailable") from None
                self._sleep(float(attempt + 1))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 == _SEARCH_ATTEMPTS:
                    raise SearchProviderError("MiniMax search provider unavailable")
                self._sleep(float(attempt + 1))
                continue
            if response.status_code >= 400:
                raise SearchProviderError("MiniMax search provider unavailable")
            try:
                return _parse_search_payload(response.json())
            except SearchProviderError:
                raise
            except Exception:
                raise SearchProviderError("invalid search response") from None
        raise SearchProviderError("MiniMax search provider unavailable")


def _parse_search_payload(payload: object) -> tuple[SearchResult, ...]:
    if not isinstance(payload, Mapping):
        raise SearchProviderError("invalid search response")
    base_response = payload.get("base_resp")
    if not isinstance(base_response, Mapping) or base_response.get("status_code") != 0:
        raise SearchProviderError("MiniMax search provider unavailable")
    organic = payload.get("organic")
    if not isinstance(organic, list):
        raise SearchProviderError("invalid search response")

    results: list[SearchResult] = []
    for raw in organic[:_MAX_RESULTS]:
        if not isinstance(raw, Mapping):
            raise SearchProviderError("invalid search response")
        title = _bounded_text(raw.get("title"), 300, truncate=True)
        url = _bounded_text(raw.get("link"), 2_048)
        snippet = _bounded_text(raw.get("snippet"), 2_000, truncate=True)
        date_text = raw.get("date")
        if date_text is not None:
            if not isinstance(date_text, str):
                raise SearchProviderError("invalid search response")
            date_text = None if not date_text.strip() else _bounded_text(date_text, 100)
        if not title or not url or not snippet:
            raise SearchProviderError("invalid search response")
        results.append(SearchResult(title, url, snippet, date_text or None))
    return tuple(results)


def _bounded_text(value: Any, limit: int, *, truncate: bool = False) -> str:
    if not isinstance(value, str):
        raise SearchProviderError("invalid search response")
    normalized = " ".join(value.split())
    if not normalized or (len(normalized) > limit and not truncate):
        raise SearchProviderError("invalid search response")
    return normalized[:limit]
