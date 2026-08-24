import json
from pathlib import Path

import httpx
import pytest

from goldbook.minimax import MiniMaxClient, MiniMaxProviderError, parse_analysis
from goldbook.config import Settings
from goldbook.models import Direction, ReviewStatus, TranscriptSegment


SEGMENTS = (
    TranscriptSegment(10.0, 18.0, "我认为黄金下周还会继续上涨"),
    TranscriptSegment(18.0, 25.0, "如果跌破两千四就要重新评估"),
)


def test_parses_valid_signal_and_approves_locatable_evidence():
    """Breaking valid JSON decoding or in-range evidence matching must fail."""
    payload = json.dumps(
        {
            "direction": "bullish",
            "strength": 4,
            "confidence": 0.91,
            "horizon_text": "下周",
            "target_price": None,
            "stop_price": 2400,
            "conditions": ["跌破2400重新评估"],
            "is_retrospective": False,
            "is_news_only": False,
            "evidence": [
                {
                    "start_sec": 10.0,
                    "end_sec": 18.0,
                    "quote": "黄金下周还会继续上涨",
                }
            ],
            "summary": "明确看多黄金",
        },
        ensure_ascii=False,
    )

    result = parse_analysis(payload, SEGMENTS)

    assert result.direction is Direction.BULLISH
    assert result.review_status is ReviewStatus.APPROVED


def test_unlocatable_quote_requires_review():
    """Dropping evidence that cannot be located must make the result reviewable."""
    payload = json.dumps(
        {
            "direction": "bearish",
            "strength": 3,
            "confidence": 0.8,
            "horizon_text": None,
            "target_price": None,
            "stop_price": None,
            "conditions": [],
            "is_retrospective": False,
            "is_news_only": False,
            "evidence": [{"start_sec": 10, "end_sec": 18, "quote": "字幕中不存在"}],
            "summary": "看空",
        },
        ensure_ascii=False,
    )

    result = parse_analysis(payload, SEGMENTS)

    assert result.evidence == ()
    assert result.review_status is ReviewStatus.NEEDS_REVIEW


def test_adjacent_segments_support_evidence_spanning_the_claimed_range():
    """Requiring a quote to fit one segment must not reject adjacent evidence."""
    segments = (
        TranscriptSegment(0.0, 1.0, "黄金 "),
        TranscriptSegment(1.0, 2.0, "继续上涨"),
        TranscriptSegment(2.0, 3.0, "范围外文本"),
    )
    payload = json.dumps(
        {
            "direction": "bullish",
            "strength": 4,
            "confidence": 0.91,
            "horizon_text": "下周",
            "target_price": None,
            "stop_price": None,
            "conditions": [],
            "is_retrospective": False,
            "is_news_only": False,
            "evidence": [{"start_sec": 0.0, "end_sec": 2.0, "quote": "黄金继续上涨"}],
            "summary": "跨片段看多",
        },
        ensure_ascii=False,
    )

    result = parse_analysis(payload, segments)

    assert result.evidence == (
        {"start_sec": 0.0, "end_sec": 2.0, "quote": "黄金继续上涨"},
    )
    assert result.review_status is ReviewStatus.APPROVED


def test_evidence_matching_does_not_include_text_outside_the_claimed_range():
    """Expanding the evidence window past its end must not validate a quote."""
    segments = (
        TranscriptSegment(0.0, 1.0, "黄金"),
        TranscriptSegment(1.0, 2.0, "继续上涨"),
        TranscriptSegment(2.0, 3.0, "范围外文本"),
    )
    payload = json.dumps(
        {
            "direction": "bullish",
            "strength": 4,
            "confidence": 0.91,
            "horizon_text": None,
            "target_price": None,
            "stop_price": None,
            "conditions": [],
            "is_retrospective": False,
            "is_news_only": False,
            "evidence": [
                {"start_sec": 0.0, "end_sec": 2.0, "quote": "黄金继续上涨范围外文本"}
            ],
            "summary": "不可跨范围取证",
        },
        ensure_ascii=False,
    )

    result = parse_analysis(payload, segments)

    assert result.evidence == ()
    assert result.review_status is ReviewStatus.NEEDS_REVIEW


def test_batch_never_enters_more_than_three_requests():
    """Removing the shared request semaphore must make the measured peak exceed three."""
    import threading
    import time

    from goldbook.minimax import MiniMaxClient, run_analysis_batch

    lock = threading.Lock()
    active = 0
    peak = 0

    def fake_request(_segments):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return (
            '{"direction":"no_signal","strength":1,"confidence":0.9,'
            '"horizon_text":null,"target_price":null,"stop_price":null,'
            '"conditions":[],"is_retrospective":false,"is_news_only":false,'
            '"evidence":[],"summary":"无信号"}'
        )

    client = MiniMaxClient.for_test(fake_request, max_concurrency=3)
    items = [(str(index), SEGMENTS) for index in range(10)]

    results = run_analysis_batch(client, items)

    assert len(results) == 10
    assert peak == 3


def test_batch_respects_a_smaller_configured_concurrency_limit():
    """Changing a configured limit of one must prevent overlapping requests."""
    import threading
    import time

    from goldbook.minimax import MiniMaxClient, run_analysis_batch

    lock = threading.Lock()
    active = 0
    peak = 0

    def fake_request(_segments):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return _no_signal_payload()

    results = run_analysis_batch(
        MiniMaxClient.for_test(fake_request, max_concurrency=1),
        [(str(index), SEGMENTS) for index in range(4)],
    )

    assert len(results) == 4
    assert peak == 1


def test_test_client_clamps_any_larger_requested_limit_to_ten():
    """A code path bypassing Settings still cannot exceed ten parallel calls."""
    import threading
    import time

    from goldbook.minimax import MiniMaxClient, run_analysis_batch

    lock = threading.Lock()
    active = 0
    peak = 0

    def fake_request(_segments):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return _no_signal_payload()

    results = run_analysis_batch(
        MiniMaxClient.for_test(fake_request, max_concurrency=99),
        [(str(index), SEGMENTS) for index in range(10)],
    )

    assert len(results) == 10
    assert peak == 10


def test_client_retries_429_with_injectable_backoff_and_uses_segment_only_prompt():
    """Removing retry or leaking metadata from the request must fail this client boundary test."""
    import httpx

    from goldbook.minimax import MiniMaxClient

    requests: list[httpx.Request] = []
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": _no_signal_payload()}}]}, request=request)

    client = MiniMaxClient(
        _settings(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=delays.append,
    )

    result = client.analyze(SEGMENTS)

    assert result.review_status is ReviewStatus.NEEDS_REVIEW
    assert len(requests) == 2
    assert delays == [1.0]
    body = json.loads(requests[-1].content)
    prompt = body["messages"][-1]["content"]
    assert "[1] 10.000-18.000: 我认为黄金下周还会继续上涨" in prompt
    assert "creator" not in prompt.lower()
    assert "bvid" not in prompt.lower()


def test_client_converts_invalid_model_payload_to_manual_review():
    """A malformed model payload must remain reviewable rather than fail the provider job."""
    result = MiniMaxClient.for_test(
        lambda _segments: "not JSON", sleep=lambda _delay: None
    ).analyze(SEGMENTS)

    assert result.direction is Direction.NO_SIGNAL
    assert result.review_status is ReviewStatus.NEEDS_REVIEW
    assert result.evidence == ()


def test_client_retries_once_when_m3_returns_an_invalid_structured_payload():
    responses = iter(["not JSON", _no_signal_payload()])
    attempts = []

    def request(_segments):
        attempts.append(1)
        return next(responses)

    result = MiniMaxClient.for_test(request).analyze(SEGMENTS)

    assert len(attempts) == 2
    assert result.summary == "无信号"


def test_client_allows_long_transcript_responses_more_than_the_httpx_default_timeout():
    observed_timeouts = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed_timeouts.append(request.extensions["timeout"])
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": _no_signal_payload()}}]},
            request=request,
        )

    client = MiniMaxClient(
        _settings(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    client.analyze(SEGMENTS)

    assert observed_timeouts[0]["read"] == 120.0


@pytest.mark.parametrize("status_code", [429, 503])
def test_client_raises_sanitized_provider_error_after_retryable_http_exhaustion(status_code):
    """Treating an exhausted 429/5xx as manual review would complete an unretryable job."""
    attempts: list[httpx.Request] = []
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(
            status_code,
            headers={"X-Provider-Token": "provider-secret"},
            content=b'{"error":"raw provider response and transcript"}',
            request=request,
        )

    client = MiniMaxClient(
        _settings(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=delays.append,
    )

    with pytest.raises(MiniMaxProviderError) as captured:
        client.analyze(SEGMENTS)

    assert len(attempts) == 3
    assert delays == [1.0, 2.0]
    assert str(captured.value) == "MiniMax provider unavailable"
    assert "provider-secret" not in str(captured.value)
    assert "raw provider response" not in str(captured.value)


def test_client_retries_network_errors_then_raises_sanitized_provider_error():
    """Returning a manual-review signal for exhausted network errors would hide retryable work."""
    attempts = 0
    delays: list[float] = []

    def fail_request(_segments):
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("Authorization: Bearer network-secret; private transcript")

    with pytest.raises(MiniMaxProviderError) as captured:
        MiniMaxClient.for_test(fail_request, sleep=delays.append).analyze(SEGMENTS)

    assert attempts == 3
    assert delays == [1.0, 2.0]
    assert str(captured.value) == "MiniMax provider unavailable"


def test_fact_check_completion_returns_a_json_object_and_retries_invalid_json():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        content = (
            "not json"
            if len(requests) == 1
            else '{"status":"search","queries":["US CPI actual forecast"]}'
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
            request=request,
        )

    client = MiniMaxClient(
        _settings(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    messages = (
        {"role": "system", "content": "Return JSON."},
        {"role": "user", "content": "Check CPI."},
    )

    result = client.complete_fact_check(messages)

    assert result == {"status": "search", "queries": ["US CPI actual forecast"]}
    assert len(requests) == 2
    body = json.loads(requests[-1].content)
    assert body["model"] == "test-model"
    assert body["messages"] == list(messages)
    assert requests[-1].url.path == "/v1/chat/completions"


def _settings() -> Settings:
    return Settings(
        data_dir=Path("data"),
        web_host="127.0.0.1",
        web_port=8765,
        lookback_days=183,
        minimax_api_key="test-minimax-key",
        minimax_base_url="https://example.invalid/v1",
        minimax_model="test-model",
        minimax_max_concurrency=3,
        whisper_model="small",
        whisper_device="auto",
    )


def _no_signal_payload() -> str:
    return (
        '{"direction":"no_signal","strength":1,"confidence":0.9,'
        '"horizon_text":null,"target_price":null,"stop_price":null,'
        '"conditions":[],"is_retrospective":false,"is_news_only":false,'
        '"evidence":[],"summary":"无信号"}'
    )
