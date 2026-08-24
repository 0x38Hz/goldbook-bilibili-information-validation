import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event, Lock, Thread
from types import SimpleNamespace

import pytest
import httpx

from goldbook.db import Database
from goldbook.fact_check import FactCheckImpact, FactCheckResult, FactValue
from goldbook.fact_check_agent import FactCheckBundle
from goldbook.jobs import BackgroundRunner, PipelineService
from goldbook.minimax import ClaimExtraction, MiniMaxClient
from goldbook.models import (
    ClaimLeg,
    ClaimStatus,
    ClaimType,
    Creator,
    Direction,
    ForecastClaim,
    HorizonSource,
    Instrument,
    PriceBar,
    ReviewStatus,
    SignalAnalysis,
    TranscriptSegment,
    Video,
    VideoStatus,
)


class FakeSource:
    def __init__(self, videos: list[Video]) -> None:
        self.videos, self.download_count = videos, 0
        self.started, self.release, self.block_download = Event(), Event(), False

    def list_videos(self, source: str, published_after: datetime) -> list[Video]:
        return [video for video in self.videos if video.published_at > published_after]

    def download_audio(self, video: Video, destination: Path) -> Path:
        self.download_count += 1
        self.started.set()
        if self.block_download:
            assert self.release.wait(1.0)
        destination.mkdir(parents=True, exist_ok=True)
        audio = destination / f"{video.bvid}.wav"
        audio.write_bytes(b"fake audio")
        return audio


class FakeTranscriber:
    model_name = "fake-whisper-v1"

    def __init__(self) -> None:
        self.error: BaseException | None = None

    def transcribe(self, audio_path: Path) -> tuple[TranscriptSegment, ...]:
        if self.error is not None:
            raise self.error
        return (TranscriptSegment(0.0, 3.0, "黄金将上涨"),)


class FakeAnalyzer:
    def __init__(self) -> None:
        self.call_count = 0

    def analyze(self, segments: tuple[TranscriptSegment, ...]) -> SignalAnalysis:
        self.call_count += 1
        return SignalAnalysis(Direction.BULLISH, 4, 0.9, evidence=({"start_sec": 0.0, "end_sec": 3.0, "quote": "黄金将上涨"},), summary="看多黄金", review_status=ReviewStatus.APPROVED)


class FakeMarket:
    def fetch(self, start: date, end: date) -> list[PriceBar]:
        return [PriceBar("2026-08-20", 2400.0, 2410.0, 2390.0, 2405.0), PriceBar("2026-08-21", 2405.0, 2420.0, 2400.0, 2415.0)]


class FakeFactChecker:
    def __init__(self, error=None):
        self.calls = 0
        self.error = error

    def run(self, video, need, claims, segments):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return FactCheckBundle(
            evidence=(),
            result=FactCheckResult(
                question="Was the event supportive?",
                event_name="US CPI",
                event_time_utc=None,
                facts=(),
                impact=FactCheckImpact.INSUFFICIENT,
                reasoning_summary="No conclusive public evidence was returned.",
                evidence_ids=(),
                branch_decisions=(),
                confidence=0.1,
            ),
            search_count=1,
        )


@dataclass
class FakePipelinePorts:
    db: Database
    source: FakeSource
    transcriber: FakeTranscriber
    analyzer: FakeAnalyzer
    market: FakeMarket


@pytest.fixture
def fake_pipeline_ports(tmp_path) -> FakePipelinePorts:
    db = Database(tmp_path / "goldbook.db")
    db.initialize()
    db.upsert_creator(Creator("42", "测试UP", "https://space.bilibili.com/42"))
    video = Video("BVNEW", "42", "黄金观点", datetime(2026, 8, 19, tzinfo=timezone.utc), 60, "https://www.bilibili.com/video/BVNEW")
    return FakePipelinePorts(db, FakeSource([video]), FakeTranscriber(), FakeAnalyzer(), FakeMarket())


def _service(tmp_path: Path, ports: FakePipelinePorts) -> PipelineService:
    return PipelineService(db=ports.db, source=ports.source, transcriber=ports.transcriber, analyzer=ports.analyzer, market=ports.market, temp_root=tmp_path / "tmp")


def _sync(service: PipelineService) -> None:
    service.sync_creator("https://space.bilibili.com/42", datetime(2026, 8, 20, tzinfo=timezone.utc))


def _wait_for(predicate) -> None:
    for _ in range(40):
        if predicate():
            return
        Event().wait(0.025)
    assert predicate()


def _add_fact_check_video(database: Database):
    video = Video(
        "BV1CPI",
        "42",
        "CPI预测",
        datetime(2026, 8, 12, 0, 51, tzinfo=timezone.utc),
        120,
        "https://www.bilibili.com/video/BV1CPI",
        VideoStatus.COMPLETE,
    )
    database.upsert_video(video)
    segments = (TranscriptSegment(1.0, 4.0, "若今晚CPI利好黄金将突破4400", bvid=video.bvid),)
    database.save_transcript(video.bvid, segments, model="fake", text_hash="hash-cpi")
    analysis = SignalAnalysis(
        Direction.NEUTRAL,
        2,
        0.8,
        bvid=video.bvid,
        transcript_hash="hash-cpi",
        revision=1,
        model_name="MiniMax-M3",
    )
    claim = ForecastClaim(
        "BV1CPI:1:1",
        video.bvid,
        1,
        1,
        Instrument.XAU_USD_SPOT,
        ClaimType.SEQUENCE,
        Direction.BULLISH,
        (ClaimLeg(">=", 4400.0, None),),
        "若今晚CPI数据利好，黄金将突破4400",
        "今晚CPI公布后",
        HorizonSource.EXPLICIT_RELATIVE,
        0,
        1,
        1,
        None,
        0.9,
        0.8,
        ({"start_sec": 1.0, "end_sec": 4.0, "quote": "若今晚CPI利好黄金将突破4400"},),
        None,
        ClaimStatus.AUTO_VALIDATED,
        "MiniMax-M3",
        "claims-v2-primary-trend",
        "hash-cpi",
    )
    database.save_claim_extraction(analysis, (claim,))
    return video


def test_fact_check_is_enqueued_once_and_runs_only_in_background(
    tmp_path, fake_pipeline_ports, monkeypatch
):
    video = _add_fact_check_video(fake_pipeline_ports.db)
    recomputed = []
    monkeypatch.setattr(
        "goldbook.jobs.recompute_claim_evaluations",
        lambda database, evaluated_at: recomputed.append((database, evaluated_at)),
        raising=False,
    )
    checker = FakeFactChecker()
    service = PipelineService(
        db=fake_pipeline_ports.db,
        source=fake_pipeline_ports.source,
        transcriber=fake_pipeline_ports.transcriber,
        analyzer=fake_pipeline_ports.analyzer,
        market=fake_pipeline_ports.market,
        temp_root=tmp_path / "tmp",
        fact_checker=checker,
    )
    runner = BackgroundRunner(service)

    first = service.enqueue_fact_check(video.bvid)
    second = service.enqueue_fact_check(video.bvid)
    assert first.id == second.id
    assert checker.calls == 0

    runner.start()
    try:
        _wait_for(lambda: fake_pipeline_ports.db.get_job(first.id).status == "complete")
    finally:
        runner.stop()
    assert checker.calls == 1
    stored = fake_pipeline_ports.db.get_current_fact_check(video.bvid)
    assert stored is not None
    assert stored.result.impact is FactCheckImpact.INSUFFICIENT
    assert recomputed and recomputed[0][0] is fake_pipeline_ports.db


def test_fact_check_failure_is_retryable_and_never_persists_remote_text(tmp_path, fake_pipeline_ports):
    video = _add_fact_check_video(fake_pipeline_ports.db)
    checker = FakeFactChecker(RuntimeError("Authorization: Bearer secret; private search body"))
    service = PipelineService(
        db=fake_pipeline_ports.db,
        source=fake_pipeline_ports.source,
        transcriber=fake_pipeline_ports.transcriber,
        analyzer=fake_pipeline_ports.analyzer,
        market=fake_pipeline_ports.market,
        temp_root=tmp_path / "tmp",
        fact_checker=checker,
    )
    runner = BackgroundRunner(service)
    job = service.enqueue_fact_check(video.bvid)
    runner.start()
    try:
        _wait_for(lambda: fake_pipeline_ports.db.get_job(job.id).status == "failed")
    finally:
        runner.stop()
    failed = fake_pipeline_ports.db.get_job(job.id)
    assert failed.error == "RuntimeError: fact check failed"
    assert "secret" not in failed.error
    assert service.retry_job(job.id) is True


def test_sync_creator_only_enqueues_and_runner_processes_idempotently(tmp_path, fake_pipeline_ports):
    service = _service(tmp_path, fake_pipeline_ports)
    _sync(service)
    _sync(service)
    assert fake_pipeline_ports.source.download_count == 0
    assert fake_pipeline_ports.db.get_active_video_job("BVNEW").status == "pending"
    runner = BackgroundRunner(service)
    runner.start()
    try:
        _wait_for(lambda: fake_pipeline_ports.db.get_video("BVNEW").status is VideoStatus.COMPLETE)
    finally:
        runner.stop()
    assert fake_pipeline_ports.source.download_count == fake_pipeline_ports.analyzer.call_count == 1
    assert list((tmp_path / "tmp").glob("**/*.wav")) == []


def test_sync_creator_discovers_history_and_keeps_latest_one_hundred(tmp_path):
    class CapturingSource(FakeSource):
        published_after = None

        def list_videos(self, source, published_after):
            self.published_after = published_after
            return super().list_videos(source, published_after)

    db = Database(tmp_path / "goldbook.db")
    db.initialize()
    db.upsert_creator(Creator("42", "测试UP", "https://space.bilibili.com/42"))
    beginning = datetime(2024, 1, 1, tzinfo=timezone.utc)
    videos = [
        Video(
            f"BV{index:010d}",
            "42",
            f"历史观点 {index}",
            beginning + timedelta(days=index),
            60,
            f"https://www.bilibili.com/video/BV{index:010d}",
        )
        for index in range(120)
    ]
    source = CapturingSource(list(reversed(videos)))
    service = PipelineService(
        db=db,
        source=source,
        transcriber=FakeTranscriber(),
        analyzer=FakeAnalyzer(),
        market=FakeMarket(),
        temp_root=tmp_path / "tmp",
    )

    service.sync_creator("42", datetime(2026, 8, 22, tzinfo=timezone.utc))

    assert source.published_after == datetime(1970, 1, 1, tzinfo=timezone.utc)
    stored = db.list_videos("42")
    assert len(stored) == 100
    assert stored[0].bvid == "BV0000000119"
    assert stored[-1].bvid == "BV0000000020"


def test_sync_creator_does_not_use_related_recommendations_when_space_listing_is_blocked(
    tmp_path,
):
    existing = Video(
        "BV10000000000",
        "42",
        "已有视频",
        datetime(2026, 1, 2, tzinfo=timezone.utc),
        60,
        "https://www.bilibili.com/video/BV10000000000",
        VideoStatus.COMPLETE,
    )
    class BlockedSource(FakeSource):
        expand_related_called = False

        def list_videos(self, source, published_after):
            raise RuntimeError("public listing blocked")

        def expand_related(self, creator_uid, seeds, published_after, *, limit):
            self.expand_related_called = True
            raise AssertionError("相关推荐不得作为创作者历史补洞")

    db = Database(tmp_path / "goldbook.db")
    db.initialize()
    db.upsert_creator(Creator("42", "测试UP", "https://space.bilibili.com/42"))
    db.upsert_video(existing)
    source = BlockedSource([])
    service = PipelineService(
        db=db,
        source=source,
        transcriber=FakeTranscriber(),
        analyzer=FakeAnalyzer(),
        market=FakeMarket(),
        temp_root=tmp_path / "tmp",
    )

    with pytest.raises(RuntimeError, match="public listing blocked"):
        service.sync_creator("42", datetime(2026, 8, 22, tzinfo=timezone.utc))

    assert source.expand_related_called is False
    assert db.get_video(existing.bvid) == existing


def test_creator_sync_preserves_an_existing_public_display_name(
    tmp_path, fake_pipeline_ports
):
    service = _service(tmp_path, fake_pipeline_ports)

    _sync(service)

    assert fake_pipeline_ports.db.get_creator("42").name == "测试UP"


def test_retry_reuses_a_persisted_transcript_without_redownloading_audio(
    tmp_path, fake_pipeline_ports
):
    service = _service(tmp_path, fake_pipeline_ports)
    _sync(service)
    fake_pipeline_ports.db.save_transcript(
        "BVNEW",
        (TranscriptSegment(0.0, 3.0, "黄金将上涨"),),
        model="fake-whisper-v1",
        text_hash="cached-transcript-hash",
    )

    service.process_video("BVNEW")

    assert fake_pipeline_ports.source.download_count == 0
    assert fake_pipeline_ports.analyzer.call_count == 1
    assert fake_pipeline_ports.db.get_video("BVNEW").status is VideoStatus.COMPLETE


def test_minimax_video_processing_persists_structured_claim_evaluation_and_fact_check(
    tmp_path, fake_pipeline_ports, monkeypatch
):
    service = _service(tmp_path, fake_pipeline_ports)
    _sync(service)
    video = fake_pipeline_ports.db.get_video("BVNEW")
    transcript_hash = "structured-hash"
    fake_pipeline_ports.db.save_transcript(
        video.bvid,
        (TranscriptSegment(0.0, 3.0, "黄金将上涨"),),
        model="fake-whisper-v1",
        text_hash=transcript_hash,
    )
    claim = ForecastClaim(
        "BVNEW:0:0", video.bvid, 0, 0, Instrument.XAU_USD_SPOT,
        ClaimType.DIRECTIONAL_MOVE, Direction.BULLISH, (), "黄金将上涨", "明天",
        HorizonSource.EXPLICIT_RELATIVE, 1, 1, 1, None, 0.9, 0.9,
        ({"start_sec": 0.0, "end_sec": 3.0, "quote": "黄金将上涨"},), None,
        ClaimStatus.AUTO_VALIDATED, "MiniMax-M3", "claims-v2-primary-trend", transcript_hash,
    )
    model = MiniMaxClient.for_test(lambda _segments: "{}")
    service.analyzer = model
    service.fact_checker = FakeFactChecker()
    monkeypatch.setattr(
        "goldbook.jobs.run_claim_extraction_batch",
        lambda _client, _items: {video.bvid: ClaimExtraction("看多黄金", (claim,))},
        raising=False,
    )
    monkeypatch.setattr(
        "goldbook.jobs.detect_fact_check_need",
        lambda *_args: SimpleNamespace(required=True, event_description="CPI"),
    )

    service.process_video(video.bvid)

    assert fake_pipeline_ports.db.get_latest_analysis(video.bvid).summary == "看多黄金"
    assert fake_pipeline_ports.db.list_forecast_claims(video.bvid) == [claim]
    assert fake_pipeline_ports.db.get_claim_evaluation(claim.claim_id) is not None
    assert fake_pipeline_ports.db.get_active_fact_check_job(video.bvid) is not None


def test_empty_local_transcript_becomes_an_explicit_no_signal_without_model_call(
    tmp_path, fake_pipeline_ports
):
    service = _service(tmp_path, fake_pipeline_ports)
    _sync(service)
    fake_pipeline_ports.transcriber.transcribe = lambda _audio: ()

    service.process_video("BVNEW")

    analysis = fake_pipeline_ports.db.get_latest_analysis("BVNEW")
    assert fake_pipeline_ports.analyzer.call_count == 0
    assert analysis.direction is Direction.NO_SIGNAL
    assert analysis.summary == "本地转写未检测到可分析语音"


def test_creator_discovery_is_enqueued_and_worker_never_runs_it_in_caller(tmp_path, fake_pipeline_ports):
    service = _service(tmp_path, fake_pipeline_ports)
    runner = BackgroundRunner(service)
    job = service.enqueue_creator_sync(
        "https://space.bilibili.com/42", datetime(2026, 8, 20, tzinfo=timezone.utc)
    )
    duplicate = service.enqueue_creator_sync(
        "https://space.bilibili.com/42", datetime(2026, 8, 20, tzinfo=timezone.utc)
    )
    assert job.id == duplicate.id
    assert fake_pipeline_ports.source.download_count == 0
    assert fake_pipeline_ports.db.get_video("BVNEW") is None
    runner.start()
    try:
        _wait_for(lambda: fake_pipeline_ports.db.get_job(job.id).status in {"complete", "failed", "cancelled"})
        completed = fake_pipeline_ports.db.get_job(job.id)
        assert completed.status == "complete", completed
        _wait_for(lambda: fake_pipeline_ports.db.get_video("BVNEW") is not None)
    finally:
        runner.stop()


def test_bvid_discovery_resolves_its_public_creator_only_in_worker(tmp_path, fake_pipeline_ports):
    service = _service(tmp_path, fake_pipeline_ports)
    runner = BackgroundRunner(service)
    job = service.enqueue_creator_sync("BVNEW", datetime(2026, 8, 20, tzinfo=timezone.utc))

    assert fake_pipeline_ports.db.get_creator("bvid:BVNEW") is not None
    assert fake_pipeline_ports.db.get_video("BVNEW") is None
    runner.start()
    try:
        _wait_for(lambda: fake_pipeline_ports.db.get_creator("42") is not None)
        _wait_for(lambda: fake_pipeline_ports.db.get_video("BVNEW") is not None)
    finally:
        runner.stop()


def test_audio_is_deleted_when_transcription_fails(tmp_path, fake_pipeline_ports):
    fake_pipeline_ports.transcriber.error = RuntimeError("decode failed")
    service = _service(tmp_path, fake_pipeline_ports)
    _sync(service)
    runner = BackgroundRunner(service)
    runner.start()
    try:
        _wait_for(lambda: fake_pipeline_ports.db.get_video("BVNEW").status is VideoStatus.FAILED)
    finally:
        runner.stop()
    assert list((tmp_path / "tmp").glob("**/*.wav")) == []
    assert fake_pipeline_ports.db.get_video("BVNEW").error_summary == "RuntimeError: processing failed"


def test_audio_is_deleted_when_processing_is_cancelled(tmp_path, fake_pipeline_ports):
    fake_pipeline_ports.transcriber.error = asyncio.CancelledError()
    service = _service(tmp_path, fake_pipeline_ports)
    _sync(service)
    with pytest.raises(asyncio.CancelledError):
        service.process_video("BVNEW")
    assert list((tmp_path / "tmp").glob("**/*.wav")) == []
    assert fake_pipeline_ports.db.get_video("BVNEW").status is VideoStatus.CANCELLED
    assert fake_pipeline_ports.db.get_active_video_job("BVNEW") is None


def test_sensitive_exception_text_is_never_persisted(tmp_path, fake_pipeline_ports):
    secret, transcript = "Bearer super-secret-token", "私人转写原文"
    fake_pipeline_ports.transcriber.error = RuntimeError(f"Authorization: {secret}; api_key=abc; {transcript}")
    service = _service(tmp_path, fake_pipeline_ports)
    _sync(service)
    runner = BackgroundRunner(service)
    runner.start()
    try:
        _wait_for(lambda: fake_pipeline_ports.db.get_video("BVNEW").status is VideoStatus.FAILED)
    finally:
        runner.stop()
    job = fake_pipeline_ports.db.get_job(1)
    persisted = f"{fake_pipeline_ports.db.get_video('BVNEW').error_summary} {job.error}"
    assert secret not in persisted and transcript not in persisted
    assert "authorization" not in persisted.lower() and "api_key" not in persisted.lower()


def test_startup_recovers_interrupted_jobs_as_pending(tmp_path, fake_pipeline_ports):
    job_id = fake_pipeline_ports.db.create_job("sync_creator", "42")
    assert fake_pipeline_ports.db.claim_pending_job(job_id)
    assert fake_pipeline_ports.db.recover_interrupted_jobs() == 1
    job = fake_pipeline_ports.db.get_job(job_id)
    assert (job.status, job.stage) == ("pending", "recovered")


def test_recovered_running_job_for_already_complete_video_is_completed(tmp_path, fake_pipeline_ports):
    service = _service(tmp_path, fake_pipeline_ports)
    _sync(service)
    runner = BackgroundRunner(service)
    runner.start()
    try:
        _wait_for(lambda: fake_pipeline_ports.db.get_video("BVNEW").status is VideoStatus.COMPLETE)
    finally:
        runner.stop()
    job_id = fake_pipeline_ports.db.create_job("42", video_bvid="BVNEW")
    assert fake_pipeline_ports.db.claim_pending_job(job_id)
    runner.start()
    try:
        _wait_for(lambda: fake_pipeline_ports.db.get_job(job_id).status == "complete")
    finally:
        runner.stop()
    assert fake_pipeline_ports.source.download_count == 1


def test_concurrent_enqueue_returns_one_active_job_without_integrity_error(fake_pipeline_ports):
    fake_pipeline_ports.db.upsert_video(fake_pipeline_ports.source.videos[0])
    barrier, results, failures, result_lock = Barrier(2), [], [], Lock()
    def enqueue() -> None:
        try:
            barrier.wait()
            job = fake_pipeline_ports.db.create_or_get_active_video_job("42", "BVNEW")
            with result_lock: results.append(job.id)
        except BaseException as error:
            with result_lock: failures.append(error)
    threads = [Thread(target=enqueue), Thread(target=enqueue)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert failures == [] and results[0] == results[1]


def test_database_rejects_backward_state_and_progress_transitions(fake_pipeline_ports):
    fake_pipeline_ports.db.upsert_video(fake_pipeline_ports.source.videos[0])
    job = fake_pipeline_ports.db.create_or_get_active_video_job("42", "BVNEW")
    assert fake_pipeline_ports.db.claim_pending_job(job.id)
    assert fake_pipeline_ports.db.advance_job(job.id, "downloading", 0.0)
    assert fake_pipeline_ports.db.advance_job(job.id, "transcribing", 0.25)
    assert fake_pipeline_ports.db.complete_job(job.id)
    with pytest.raises(ValueError):
        fake_pipeline_ports.db.update_job(job.id, status="running", stage="downloading", progress=0.0)


def test_paused_job_has_an_explicit_retryable_state(fake_pipeline_ports):
    fake_pipeline_ports.db.upsert_video(fake_pipeline_ports.source.videos[0])
    job = fake_pipeline_ports.db.create_or_get_active_video_job("42", "BVNEW")
    assert fake_pipeline_ports.db.claim_pending_job(job.id)
    assert fake_pipeline_ports.db.pause_job(job.id)
    assert fake_pipeline_ports.db.get_job(job.id).status == "paused"
    assert fake_pipeline_ports.db.retry_job(job.id)
    assert fake_pipeline_ports.db.get_job(job.id).status == "pending"


@pytest.mark.parametrize(
    ("control", "expected_job_status", "expected_video_status"),
    [
        ("pause", "paused", VideoStatus.PAUSED),
        ("cancel", "cancelled", VideoStatus.CANCELLED),
    ],
)
def test_running_control_flow_preserves_state_and_worker_processes_next_video(
    tmp_path, fake_pipeline_ports, control, expected_job_status, expected_video_status
):
    fake_pipeline_ports.source.videos.append(
        Video("BVSECOND", "42", "第二条", datetime(2026, 8, 19, tzinfo=timezone.utc), 60, "https://www.bilibili.com/video/BVSECOND")
    )
    fake_pipeline_ports.source.block_download = True
    service = _service(tmp_path, fake_pipeline_ports)
    _sync(service)
    runner = BackgroundRunner(service)
    runner.start()
    assert fake_pipeline_ports.source.started.wait(0.5)
    job = fake_pipeline_ports.db.get_active_video_job("BVNEW")
    assert getattr(fake_pipeline_ports.db, f"{control}_job")(job.id)
    fake_pipeline_ports.source.release.set()
    try:
        _wait_for(lambda: fake_pipeline_ports.db.get_job(job.id).status == expected_job_status)
        _wait_for(lambda: fake_pipeline_ports.db.get_video("BVSECOND").status is VideoStatus.COMPLETE)
    finally:
        runner.stop()
    assert fake_pipeline_ports.db.get_video("BVNEW").status is expected_video_status


def test_retry_failed_job_is_a_single_compare_and_swap(tmp_path, fake_pipeline_ports):
    fake_pipeline_ports.db.upsert_video(fake_pipeline_ports.source.videos[0])
    job = fake_pipeline_ports.db.create_or_get_active_video_job("42", "BVNEW")
    assert fake_pipeline_ports.db.claim_pending_job(job.id)
    assert fake_pipeline_ports.db.fail_job(job.id, "transcribing", "RuntimeError: processing failed")
    runner, barrier, outcomes, outcomes_lock = BackgroundRunner(_service(tmp_path, fake_pipeline_ports)), Barrier(2), [], Lock()
    def retry() -> None:
        barrier.wait()
        result = runner.retry_failed(str(job.id))
        with outcomes_lock: outcomes.append(result)
    threads = [Thread(target=retry), Thread(target=retry)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert sorted(outcomes) == [False, True]
    assert fake_pipeline_ports.db.get_job(job.id).retries == 1


def test_stop_cancels_running_job_and_restart_has_a_live_worker(tmp_path, fake_pipeline_ports):
    service = _service(tmp_path, fake_pipeline_ports)
    fake_pipeline_ports.source.block_download = True
    _sync(service)
    runner = BackgroundRunner(service, join_timeout=0.01)
    runner.start()
    assert fake_pipeline_ports.source.started.wait(0.5)
    runner.stop()
    runner.start()
    fake_pipeline_ports.source.release.set()
    _wait_for(lambda: fake_pipeline_ports.db.get_video("BVNEW").status is VideoStatus.CANCELLED)
    assert runner.retry_failed("1")
    fake_pipeline_ports.source.block_download = False
    try:
        _wait_for(lambda: fake_pipeline_ports.db.get_video("BVNEW").status is VideoStatus.COMPLETE)
    finally:
        runner.stop()


def test_cleanup_failure_keeps_primary_error_and_is_observable(tmp_path, fake_pipeline_ports, monkeypatch):
    fake_pipeline_ports.transcriber.error = RuntimeError("decode failed")
    service = _service(tmp_path, fake_pipeline_ports)
    _sync(service)
    monkeypatch.setattr(service, "_remove_task_dir", lambda _directory: (_ for _ in ()).throw(PermissionError()))
    service.process_video("BVNEW")
    job = fake_pipeline_ports.db.get_job(1)
    assert fake_pipeline_ports.db.get_video("BVNEW").error_summary == "RuntimeError: processing failed"
    assert job.cleanup_error == "PermissionError: cleanup failed"


def test_exhausted_minimax_provider_failure_is_redacted_failed_and_retryable(
    tmp_path, fake_pipeline_ports
):
    """Completing a provider-exhausted analysis would make the recoverable video unreachable."""
    attempts = 0
    raw_secret = "Bearer provider-secret"
    raw_transcript = "private transcript response"

    def request(_segments):
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise httpx.ConnectError(
                f"Authorization: {raw_secret}; api_key=another-secret; {raw_transcript}"
            )
        return (
            '{"direction":"no_signal","strength":1,"confidence":0.9,'
            '"horizon_text":null,"target_price":null,"stop_price":null,'
            '"conditions":[],"is_retrospective":false,"is_news_only":false,'
            '"evidence":[],"summary":"no signal"}'
        )

    fake_pipeline_ports.analyzer = MiniMaxClient.for_test(request, sleep=lambda _delay: None)
    service = _service(tmp_path, fake_pipeline_ports)
    _sync(service)
    runner = BackgroundRunner(service)
    runner.start()
    try:
        _wait_for(lambda: fake_pipeline_ports.db.get_job(1).status == "failed")
        failed_job = fake_pipeline_ports.db.get_job(1)
        failed_video = fake_pipeline_ports.db.get_video("BVNEW")
        persisted = f"{failed_job.error} {failed_video.error_summary}".lower()
        assert failed_job.stage == "analyzing"
        assert failed_video.status is VideoStatus.FAILED
        assert raw_secret.lower() not in persisted
        assert raw_transcript not in persisted
        assert "authorization" not in persisted and "api_key" not in persisted
        assert runner.retry_failed("1")
        _wait_for(lambda: fake_pipeline_ports.db.get_job(1).status == "complete")
    finally:
        runner.stop()

    assert attempts == 4
    assert fake_pipeline_ports.db.get_video("BVNEW").status is VideoStatus.COMPLETE
