import json
import threading
import time
from datetime import datetime, timezone

import httpx
import pytest

from goldbook.config import Settings
from goldbook.minimax import (
    ClaimExtraction,
    ClaimExtractionError,
    ClaimExtractionFailure,
    MiniMaxClient,
    _is_noncommittal_claim,
    parse_claim_extraction,
    run_claim_extraction_batch,
)
from goldbook.models import (
    ClaimStatus,
    ClaimType,
    Direction,
    HorizonSource,
    Instrument,
    TranscriptSegment,
    Video,
)


VIDEO = Video(
    "BV1CLAIM",
    "42",
    "黄金短中期点位",
    datetime(2026, 8, 3, 4, 0, tzinfo=timezone.utc),
    90,
    "https://www.bilibili.com/video/BV1CLAIM",
)
SEGMENTS = (
    TranscriptSegment(0.0, 5.0, "短期先回踩4650再看4700"),
    TranscriptSegment(5.0, 10.0, "中期如果站稳4700还有上涨空间"),
)


def test_wish_or_watch_level_is_not_treated_as_a_falsifiable_forecast():
    assert _is_noncommittal_claim({"condition_text": "希望黄金直接击穿3900"})
    assert _is_noncommittal_claim({"condition_text": "关注4300支撑"})
    assert not _is_noncommittal_claim({"condition_text": "明天黄金将跌破3900"})


def _primary_trend(
    *, direction: str = "bullish", evidence: list[dict[str, object]] | None = None
) -> dict[str, object]:
    return {
        "direction": direction,
        "condition_text": "短期回调后继续看涨",
        "horizon_text": "短期",
        "horizon_source": "context_inferred",
        "horizon_min_trading_days": 1,
        "horizon_max_trading_days": 3,
        "horizon_point_trading_days": 2,
        "deadline_at": None,
        "time_confidence": 0.8,
        "confidence": 0.9,
        "evidence": evidence
        if evidence is not None
        else [
            {
                "start_sec": 0.0,
                "end_sec": 5.0,
                "quote": "短期先回踩4650再看4700",
            }
        ],
        "status": "auto_validated",
    }


def _payload() -> str:
    return json.dumps(
        {
            "summary": "短线回踩后看涨，中期关注站稳4700",
            "primary_trend": _primary_trend(),
            "claims": [
                {
                    "instrument": "xau_usd_spot",
                    "claim_type": "sequence",
                    "direction": "bullish",
                    "legs": [
                        {"operator": "<=", "level_low": 4650, "level_high": None},
                        {"operator": ">=", "level_low": 4700, "level_high": None},
                    ],
                    "condition_text": "先回踩4650再看4700",
                    "horizon_text": "短期",
                    "horizon_source": "context_inferred",
                    "horizon_min_trading_days": 1,
                    "horizon_max_trading_days": 3,
                    "horizon_point_trading_days": 2,
                    "deadline_at": None,
                    "time_confidence": 0.8,
                    "confidence": 0.9,
                    "evidence": [
                        {
                            "start_sec": 0.0,
                            "end_sec": 5.0,
                            "quote": "短期先回踩4650再看4700",
                        }
                    ],
                    "status": "auto_validated",
                },
                {
                    "instrument": "xau_usd_spot",
                    "claim_type": "hold_above",
                    "direction": "bullish",
                    "legs": [
                        {"operator": ">=", "level_low": 4700, "level_high": None}
                    ],
                    "condition_text": "站稳4700还有上涨空间",
                    "horizon_text": "中期",
                    "horizon_source": "context_inferred",
                    "horizon_min_trading_days": 5,
                    "horizon_max_trading_days": 15,
                    "horizon_point_trading_days": 10,
                    "deadline_at": None,
                    "time_confidence": 0.7,
                    "confidence": 0.85,
                    "evidence": [
                        {
                            "start_sec": 5.0,
                            "end_sec": 10.0,
                            "quote": "中期如果站稳4700还有上涨空间",
                        }
                    ],
                    "status": "auto_validated",
                },
            ],
        },
        ensure_ascii=False,
    )


def test_primary_trend_is_first_and_point_claims_follow():
    payload = json.loads(_payload())

    extraction = parse_claim_extraction(
        json.dumps(payload, ensure_ascii=False),
        VIDEO,
        SEGMENTS,
        revision=2,
        transcript_hash="hash-2",
        model_name="MiniMax-M3",
    )

    assert extraction.claims[0].claim_id == "BV1CLAIM:2:0"
    assert extraction.claims[0].claim_type is ClaimType.DIRECTIONAL_MOVE
    assert extraction.claims[0].direction is Direction.BULLISH
    assert extraction.claims[1].claim_id == "BV1CLAIM:2:1"
    assert extraction.claims[1].claim_type is ClaimType.SEQUENCE
    assert extraction.claims[0].prompt_version == "claims-v3-grounded-context"


def test_evidence_backed_neutral_primary_trend_stays_neutral():
    payload = json.loads(_payload())
    payload["primary_trend"] = _primary_trend(direction="neutral")

    extraction = parse_claim_extraction(
        json.dumps(payload, ensure_ascii=False),
        VIDEO,
        SEGMENTS,
        revision=2,
        transcript_hash="hash-2",
        model_name="MiniMax-M3",
    )

    assert extraction.claims[0].direction is Direction.NEUTRAL
    assert extraction.claims[0].status is ClaimStatus.AUTO_VALIDATED


def test_no_signal_primary_trend_may_be_unresolved_without_evidence():
    payload = json.loads(_payload())
    trend = _primary_trend(direction="no_signal", evidence=[])
    trend.update(
        {
            "condition_text": "未表达可执行趋势",
            "horizon_text": None,
            "horizon_source": "unknown",
            "horizon_min_trading_days": None,
            "horizon_max_trading_days": None,
            "horizon_point_trading_days": None,
            "time_confidence": 0.0,
            "status": "unresolved",
        }
    )
    payload["primary_trend"] = trend

    extraction = parse_claim_extraction(
        json.dumps(payload, ensure_ascii=False),
        VIDEO,
        SEGMENTS,
        revision=2,
        transcript_hash="hash-2",
        model_name="MiniMax-M3",
    )

    primary = extraction.claims[0]
    assert primary.direction is Direction.NO_SIGNAL
    assert primary.evidence == ()
    assert primary.status is ClaimStatus.UNRESOLVED


def test_bullish_primary_trend_requires_locatable_evidence():
    payload = json.loads(_payload())
    payload["primary_trend"] = _primary_trend(evidence=[])

    with pytest.raises(ClaimExtractionError, match="primary trend evidence"):
        parse_claim_extraction(
            json.dumps(payload, ensure_ascii=False),
            VIDEO,
            SEGMENTS,
            revision=2,
            transcript_hash="hash-2",
            model_name="MiniMax-M3",
        )


def test_primary_trend_repairs_verbatim_quote_with_inexact_model_timestamps():
    payload = json.loads(_payload())
    payload["primary_trend"] = _primary_trend(
        evidence=[
            {
                "start_sec": 1.0,
                "end_sec": 4.0,
                "quote": "短期先回踩4650再看4700",
            }
        ]
    )

    extraction = parse_claim_extraction(
        json.dumps(payload, ensure_ascii=False),
        VIDEO,
        SEGMENTS,
        revision=2,
        transcript_hash="hash-2",
        model_name="MiniMax-M3",
    )

    assert extraction.claims[0].evidence == (
        {"start_sec": 0.0, "end_sec": 5.0, "quote": "短期先回踩4650再看4700"},
    )


def test_primary_trend_without_day_estimate_degrades_to_unknown_horizon():
    payload = json.loads(_payload())
    trend = _primary_trend()
    trend.update(
        {
            "horizon_min_trading_days": None,
            "horizon_max_trading_days": None,
            "horizon_point_trading_days": None,
        }
    )
    payload["primary_trend"] = trend

    extraction = parse_claim_extraction(
        json.dumps(payload, ensure_ascii=False),
        VIDEO,
        SEGMENTS,
        revision=2,
        transcript_hash="hash-2",
        model_name="MiniMax-M3",
    )

    assert extraction.claims[0].horizon_source is HorizonSource.UNKNOWN
    assert extraction.claims[0].horizon_point_trading_days is None


def test_primary_trend_drops_a_deadline_not_after_publication():
    payload = json.loads(_payload())
    trend = _primary_trend()
    trend["deadline_at"] = VIDEO.published_at.isoformat()
    payload["primary_trend"] = trend

    extraction = parse_claim_extraction(
        json.dumps(payload, ensure_ascii=False),
        VIDEO,
        SEGMENTS,
        revision=2,
        transcript_hash="hash-2",
        model_name="MiniMax-M3",
    )

    assert extraction.claims[0].deadline_at is None
    assert extraction.claims[0].horizon_point_trading_days == 2


def test_bare_primary_trend_object_is_safely_wrapped_without_point_claims():
    trend = _primary_trend()

    extraction = parse_claim_extraction(
        json.dumps(trend, ensure_ascii=False),
        VIDEO,
        SEGMENTS,
        revision=2,
        transcript_hash="hash-2",
        model_name="MiniMax-M3",
    )

    assert extraction.summary == trend["condition_text"]
    assert len(extraction.claims) == 1
    assert extraction.claims[0].direction is Direction.BULLISH


def test_point_claim_cannot_duplicate_the_primary_directional_trend():
    payload = json.loads(_payload())
    payload["primary_trend"] = _primary_trend()
    payload["claims"][0].update({"claim_type": "directional_move", "legs": []})

    extraction = parse_claim_extraction(
        json.dumps(payload, ensure_ascii=False),
        VIDEO,
        SEGMENTS,
        revision=2,
        transcript_hash="hash-2",
        model_name="MiniMax-M3",
    )

    assert len(extraction.claims) == 2
    assert extraction.claims[0].claim_type is ClaimType.DIRECTIONAL_MOVE
    assert all(
        claim.claim_type is not ClaimType.DIRECTIONAL_MOVE
        for claim in extraction.claims[1:]
    )
    assert extraction.rejected_count == 1


def test_valid_primary_trend_survives_when_every_point_claim_is_rejected():
    payload = json.loads(_payload())
    payload["claims"] = [payload["claims"][0]]
    payload["claims"][0].update({"claim_type": "directional_move", "legs": []})

    extraction = parse_claim_extraction(
        json.dumps(payload, ensure_ascii=False),
        VIDEO,
        SEGMENTS,
        revision=2,
        transcript_hash="hash-2",
        model_name="MiniMax-M3",
    )

    assert len(extraction.claims) == 1
    assert extraction.claims[0].claim_type is ClaimType.DIRECTIONAL_MOVE
    assert extraction.rejected_count == 1


def test_parses_multiple_point_and_horizon_claims_with_locatable_evidence():
    extraction = parse_claim_extraction(
        _payload(),
        VIDEO,
        SEGMENTS,
        revision=1,
        transcript_hash="hash-1",
        model_name="MiniMax-M3",
    )

    assert extraction.summary == "短线回踩后看涨，中期关注站稳4700"
    assert len(extraction.claims) == 3
    primary, first, second = extraction.claims
    assert primary.claim_type is ClaimType.DIRECTIONAL_MOVE
    assert first.claim_id == "BV1CLAIM:1:1"
    assert first.instrument is Instrument.XAU_USD_SPOT
    assert first.claim_type is ClaimType.SEQUENCE
    assert first.direction is Direction.BULLISH
    assert first.legs[1].level_low == 4700
    assert first.horizon_source is HorizonSource.CONTEXT_INFERRED
    assert first.horizon_max_trading_days == 3
    assert first.status is ClaimStatus.AUTO_VALIDATED
    assert second.claim_type is ClaimType.HOLD_ABOVE
    assert second.horizon_point_trading_days == 10


def test_unknown_point_horizon_is_rejected_without_smuggling_twenty_days():
    payload = json.loads(_payload())
    payload["claims"] = [payload["claims"][0]]
    payload["claims"][0].update(
        {
            "horizon_source": "unknown",
            "horizon_min_trading_days": None,
            "horizon_point_trading_days": 20,
            "horizon_max_trading_days": None,
        }
    )

    result = parse_claim_extraction(
        json.dumps(payload, ensure_ascii=False),
        VIDEO,
        SEGMENTS,
        revision=1,
        transcript_hash="hash-1",
        model_name="MiniMax-M3",
    )

    assert len(result.claims) == 1
    assert result.rejected_count == 1
    assert result.claims[0].horizon_point_trading_days != 20


def test_unlocatable_point_evidence_rejects_only_that_point_claim():
    payload = json.loads(_payload())
    payload["claims"] = [payload["claims"][0]]
    payload["claims"][0]["evidence"][0]["quote"] = "字幕里不存在的4700预测"

    result = parse_claim_extraction(
        json.dumps(payload, ensure_ascii=False),
        VIDEO,
        SEGMENTS,
        revision=1,
        transcript_hash="hash-1",
        model_name="MiniMax-M3",
    )

    assert len(result.claims) == 1
    assert result.rejected_count == 1


def test_partial_unlocatable_claims_do_not_discard_valid_claims():
    payload = json.loads(_payload())
    payload["claims"][1]["evidence"][0]["quote"] = "字幕中完全不存在的观点"

    result = parse_claim_extraction(
        json.dumps(payload, ensure_ascii=False),
        VIDEO,
        SEGMENTS,
        revision=1,
        transcript_hash="hash-1",
        model_name="MiniMax-M3",
    )

    assert len(result.claims) == 2
    assert result.claims[1].condition_text == "先回踩4650再看4700"
    assert result.rejected_count == 1


def test_omitted_optional_deadline_is_treated_as_null():
    payload = json.loads(_payload())
    del payload["claims"][0]["deadline_at"]

    result = parse_claim_extraction(
        json.dumps(payload, ensure_ascii=False),
        VIDEO,
        SEGMENTS,
        revision=1,
        transcript_hash="hash-1",
        model_name="MiniMax-M3",
    )

    assert len(result.claims) == 3
    assert result.claims[1].deadline_at is None


def test_intraday_zero_day_horizon_is_preserved_without_rounding_to_one_day():
    payload = json.loads(_payload())
    payload["claims"] = [payload["claims"][0]]
    payload["claims"][0].update(
        {
            "horizon_text": "今天盘中",
            "horizon_source": "explicit_relative",
            "horizon_min_trading_days": 0,
            "horizon_point_trading_days": 0,
            "horizon_max_trading_days": 0,
        }
    )

    result = parse_claim_extraction(
        json.dumps(payload, ensure_ascii=False),
        VIDEO,
        SEGMENTS,
        revision=1,
        transcript_hash="hash-1",
        model_name="MiniMax-M3",
    )

    assert result.claims[1].horizon_point_trading_days == 0


def test_horizon_envelope_expands_to_include_the_models_point_estimate():
    payload = json.loads(_payload())
    payload["claims"] = [payload["claims"][0]]
    payload["claims"][0].update(
        {
            "horizon_min_trading_days": 5,
            "horizon_point_trading_days": 15,
            "horizon_max_trading_days": 10,
        }
    )

    result = parse_claim_extraction(
        json.dumps(payload, ensure_ascii=False),
        VIDEO,
        SEGMENTS,
        revision=1,
        transcript_hash="hash-1",
        model_name="MiniMax-M3",
    )

    claim = result.claims[1]
    assert claim.horizon_min_trading_days == 5
    assert claim.horizon_point_trading_days == 15
    assert claim.horizon_max_trading_days == 15


def test_missing_horizon_bounds_use_the_models_own_point_not_a_default():
    payload = json.loads(_payload())
    payload["claims"] = [payload["claims"][0]]
    payload["claims"][0].update(
        {
            "horizon_min_trading_days": None,
            "horizon_point_trading_days": 7,
            "horizon_max_trading_days": None,
        }
    )

    result = parse_claim_extraction(
        json.dumps(payload, ensure_ascii=False),
        VIDEO,
        SEGMENTS,
        revision=1,
        transcript_hash="hash-1",
        model_name="MiniMax-M3",
    )

    claim = result.claims[1]
    assert (
        claim.horizon_min_trading_days,
        claim.horizon_point_trading_days,
        claim.horizon_max_trading_days,
    ) == (7, 7, 7)


def test_primary_directional_trend_never_has_executable_legs():
    result = parse_claim_extraction(
        _payload(),
        VIDEO,
        SEGMENTS,
        revision=1,
        transcript_hash="hash-1",
        model_name="MiniMax-M3",
    )

    assert result.claims[0].legs == ()


def test_claim_client_retries_invalid_structure_once_then_raises():
    calls = 0

    def invalid(_segments):
        nonlocal calls
        calls += 1
        payload = json.loads(_payload())
        payload["claims"] = "not-a-list"
        return json.dumps(payload, ensure_ascii=False)

    client = MiniMaxClient.for_test(invalid)

    with pytest.raises(ClaimExtractionError, match="claims must be a list"):
        client.analyze_claims(
            VIDEO, SEGMENTS, revision=1, transcript_hash="hash-1"
        )

    assert calls == 2


def test_claim_request_contains_only_publication_context_and_transcript(tmp_path):
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["request_path"] = request.url.path
        observed["read_timeout"] = request.extensions["timeout"]["read"]
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": _payload()}}]},
            request=request,
        )

    settings = Settings.from_env(
        {
            "GOLDBOOK_DATA_DIR": str(tmp_path),
            "MINIMAX_API_KEY": "local-test-key",
            "MINIMAX_MODEL": "MiniMax-M3",
        }
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = MiniMaxClient(settings, http_client=http_client)
        result = client.analyze_claims(
            VIDEO, SEGMENTS, revision=1, transcript_hash="hash-1"
        )

    assert isinstance(result, ClaimExtraction)
    assert observed["request_path"] == "/v1/text/chatcompletion_v2"
    assert observed["read_timeout"] == 600.0
    assert observed["model"] == "MiniMax-M3"
    serialized = json.dumps(observed, ensure_ascii=False)
    assert VIDEO.published_at.isoformat() in serialized
    assert VIDEO.title in serialized
    assert SEGMENTS[0].text in serialized
    assert "future_prices" not in serialized
    assert "future_videos" not in serialized


def test_claim_batch_uses_requested_ten_way_concurrency():
    lock = threading.Lock()
    active = 0
    maximum = 0

    def request(_segments):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return _payload()

    client = MiniMaxClient.for_test(request, max_concurrency=10)
    items = [
        (
            Video(
                f"BV1CLAIM{index}",
                "42",
                "黄金短中期点位",
                VIDEO.published_at,
                90,
                f"https://www.bilibili.com/video/BV1CLAIM{index}",
            ),
            SEGMENTS,
            1,
            f"hash-{index}",
        )
        for index in range(10)
    ]

    results = run_claim_extraction_batch(client, items)

    assert len(results) == 10
    assert all(isinstance(value, ClaimExtraction) for value in results.values())
    assert maximum == 10


def test_claim_batch_keeps_other_videos_running_when_one_response_is_malformed(monkeypatch):
    client = MiniMaxClient.for_test(lambda _segments: _payload(), max_concurrency=2)
    monkeypatch.setattr(
        client, "analyze_claims", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("missing choices"))
    )
    results = run_claim_extraction_batch(client, [(VIDEO, SEGMENTS, 1, "hash")])

    assert isinstance(results[VIDEO.bvid], ClaimExtractionFailure)
