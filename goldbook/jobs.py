"""Resumable local processing with a single cooperative background worker."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from queue import Empty, Queue
import shutil
import tempfile
from threading import Event, RLock, Thread
from typing import Any

from goldbook.bilibili import parse_public_source
from goldbook.claim_pipeline import _summary_analysis
from goldbook.claim_evaluation import recompute_claim_evaluations
from goldbook.db import Database, Job
from goldbook.fact_check import detect_fact_check_need
from goldbook.minimax import (
    ClaimExtractionFailure,
    MiniMaxClient,
    run_analysis_batch,
    run_claim_extraction_batch,
)
from goldbook.models import (
    Creator,
    Direction,
    ReviewStatus,
    SignalAnalysis,
    TranscriptSegment,
    Video,
    VideoStatus,
)
from goldbook.scoring import score_signal


_DISCOVERY_FLOOR = datetime(1970, 1, 1, tzinfo=timezone.utc)
_CREATOR_VIDEO_LIMIT = 100


class JobCancelled(Exception):
    """Cooperative cancellation requested by the background runner."""


class JobPaused(Exception):
    """A user paused the running job; its persisted paused state is authoritative."""


class PipelineService:
    """Discover videos synchronously, but reserve processing for BackgroundRunner."""

    def __init__(
        self,
        *,
        db: Database,
        source: Any,
        transcriber: Any,
        analyzer: Any,
        market: Any,
        temp_root: Path,
        fact_checker: Any | None = None,
    ) -> None:
        self.db, self.source, self.transcriber = db, source, transcriber
        self.analyzer, self.market, self.temp_root = analyzer, market, Path(temp_root)
        self.fact_checker = fact_checker
        self._runner: BackgroundRunner | None = None

    def attach_runner(self, runner: "BackgroundRunner") -> None:
        self._runner = runner

    def enqueue_creator_sync(self, source: str, now: datetime) -> Job:
        """Persist creator discovery work; the runner performs the network request later."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        parsed = parse_public_source(source)
        if parsed.kind == "space":
            creator_uid = parsed.value
            self._upsert_creator(creator_uid, source)
        else:
            # A BVID has no author UID until public metadata is read by the worker.
            # Keep it isolated from real creator lines until that safe resolution occurs.
            creator_uid = f"bvid:{parsed.value}"
            self.db.upsert_creator(Creator(creator_uid, creator_uid, source))
        job = self.db.create_or_get_active_creator_sync_job(creator_uid)
        if self._runner is not None:
            self._runner.enqueue(job.id)
        return job

    def retry_job(self, job_id: int | str) -> bool:
        """Persist a valid retry and wake the sole worker without running work inline."""
        if not self.db.retry_job(job_id):
            return False
        if self._runner is not None:
            self._runner.enqueue(int(job_id))
        return True

    def enqueue_fact_check(self, bvid: str) -> Job:
        video = self.db.get_video(bvid)
        if video is None:
            raise ValueError("unknown video")
        analysis = self.db.get_latest_analysis(bvid)
        if analysis is None:
            raise ValueError("video has no analysis to fact-check")
        claims = tuple(self.db.list_forecast_claims(bvid))
        segments = tuple(self.db.list_transcript_segments(bvid))
        need = detect_fact_check_need(video, claims, segments)
        if not need.required:
            raise ValueError("video has no external condition to fact-check")
        self.db.create_fact_check_run(
            bvid,
            analysis.revision,
            need.event_description or "external event",
            "MiniMax-M3",
        )
        job = self.db.create_or_get_active_fact_check_job(video.creator_uid, bvid)
        if self._runner is not None:
            self._runner.enqueue(job.id)
        return job

    def process_fact_check(
        self, bvid: str, *, cancel_event: Event | None = None
    ) -> None:
        job = self.db.get_active_fact_check_job(bvid)
        if job is None or not self.db.claim_pending_job(job.id):
            return
        run = None
        stage = "detecting"
        try:
            self._raise_if_cancelled(cancel_event)
            self._advance(job.id, stage, 0.1)
            video = self.db.get_video(bvid)
            analysis = self.db.get_latest_analysis(bvid)
            if video is None or analysis is None:
                raise ValueError("fact-check video or analysis disappeared")
            claims = tuple(self.db.list_forecast_claims(bvid))
            segments = tuple(self.db.list_transcript_segments(bvid))
            need = detect_fact_check_need(video, claims, segments)
            if not need.required:
                raise ValueError("video no longer requires a fact check")
            run = self.db.create_fact_check_run(
                bvid,
                analysis.revision,
                need.event_description or "external event",
                "MiniMax-M3",
            )
            if self.fact_checker is None:
                raise RuntimeError("fact-check service is not configured")

            stage = "searching"
            self._advance(job.id, stage, 0.25)
            bundle = self.fact_checker.run(video, need, claims, segments)
            self._raise_if_cancelled(cancel_event)
            stage = "validating"
            self._advance(job.id, stage, 0.7)
            self.db.save_fact_check_evidence(run.run_id, bundle.evidence)
            stage = "evaluating"
            self._advance(job.id, stage, 0.9)
            self.db.save_fact_check_result(
                run.run_id,
                bundle.result,
                search_count=bundle.search_count,
            )
            recompute_claim_evaluations(
                self.db, evaluated_at=datetime.now(timezone.utc)
            )
            self.db.complete_job(job.id)
        except (JobCancelled, asyncio.CancelledError):
            self.db.cancel_job(job.id)
            raise
        except Exception as error:
            summary = _safe_error_summary(error, "fact check failed")
            if run is not None:
                self.db.fail_fact_check_run(run.run_id, summary)
            self.db.fail_job(job.id, stage, summary)

    def sync_creator(self, source: str, now: datetime) -> str:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        parsed = parse_public_source(source)
        videos = self.source.list_videos(source, _DISCOVERY_FLOOR)
        if not videos:
            if parsed.kind != "space":
                raise ValueError("a creator source did not yield a creator video")
            self._upsert_creator(parsed.value, source)
            return parsed.value
        videos = sorted(videos, key=lambda video: video.published_at, reverse=True)[
            :_CREATOR_VIDEO_LIMIT
        ]
        creator_uid = videos[0].creator_uid
        self._upsert_creator(creator_uid, source)
        for video in videos:
            if video.creator_uid != creator_uid:
                raise ValueError("creator sync returned videos from multiple creators")
            existing = self.db.get_video(video.bvid)
            if self._is_currently_complete(existing):
                continue
            if existing is None or self.db.get_active_video_job(video.bvid) is None:
                self.db.upsert_video(video)
            self.db.create_or_get_active_video_job(creator_uid, video.bvid)
        return creator_uid

    def process_video(self, bvid: str, *, cancel_event: Event | None = None) -> None:
        video = self.db.get_video(bvid)
        if video is None:
            raise ValueError(f"unknown video: {bvid}")
        job = self.db.get_active_video_job(bvid)
        if job is None:
            job = self.db.create_or_get_active_video_job(video.creator_uid, bvid)
        if self._is_currently_complete(video):
            self.db.complete_job(job.id)
            return
        if not self.db.claim_pending_job(job.id):
            return

        task_dir: Path | None = None
        stage = "downloading"
        succeeded = False
        cancellation: BaseException | None = None
        primary_error: Exception | None = None
        try:
            self._raise_if_cancelled(cancel_event)
            self._advance(job.id, stage, 0.0)
            self.db.update_video_status(bvid, VideoStatus.PROCESSING)
            transcript_identity = self.db.get_transcript_identity(bvid)
            if transcript_identity is None:
                task_dir = self._make_task_dir()
                audio_path = self.source.download_audio(video, task_dir)
                self._raise_if_cancelled(cancel_event)
                stage = "transcribing"
                self._advance(job.id, stage, 0.25)
                segments = tuple(self.transcriber.transcribe(audio_path))
                self._raise_if_cancelled(cancel_event)
                transcript_hash = _transcript_hash(segments)
                self.db.save_transcript(
                    bvid,
                    segments,
                    model=_model_name(self.transcriber),
                    text_hash=transcript_hash,
                )
            else:
                stage = "transcribing"
                self._advance(job.id, stage, 0.25)
                segments = tuple(self.db.list_transcript_segments(bvid))
                transcript_hash = transcript_identity[1]

            stage = "analyzing"
            self._advance(job.id, stage, 0.5)
            if isinstance(self.analyzer, MiniMaxClient):
                revision = self.db.next_analysis_revision(bvid)
                extraction = run_claim_extraction_batch(
                    self.analyzer, [(video, segments, revision, transcript_hash)]
                )[bvid]
                if isinstance(extraction, ClaimExtractionFailure):
                    raise RuntimeError(extraction.reason)
                analysis = _summary_analysis(
                    bvid, revision, transcript_hash, extraction, datetime.now(timezone.utc)
                )
                self.db.save_claim_extraction(analysis, extraction.claims)
            else:
                analysis = replace(self._analyze(bvid, segments), bvid=bvid, transcript_hash=transcript_hash)
                self.db.save_analysis(analysis)
            self._raise_if_cancelled(cancel_event)

            stage = "pricing"
            self._advance(job.id, stage, 0.75)
            bars = tuple(self.market.fetch(video.published_at.date(), date.today()))
            self.db.replace_prices(bars)
            outcome = score_signal(analysis, video.published_at, bars)
            if outcome.entry_date is not None and outcome.entry_price is not None:
                self.db.save_outcome(outcome)
            recompute_claim_evaluations(self.db, evaluated_at=datetime.now(timezone.utc))
            if self.fact_checker is not None:
                need = detect_fact_check_need(video, tuple(self.db.list_forecast_claims(bvid)), segments)
                if need.required:
                    self.enqueue_fact_check(bvid)
            self._raise_if_cancelled(cancel_event)
            succeeded = True
        except JobPaused as error:
            self.db.update_video_status(bvid, VideoStatus.PAUSED, error_summary=_safe_error_summary(error, "paused"))
            cancellation = error
        except (JobCancelled, asyncio.CancelledError) as error:
            self.db.update_video_status(bvid, VideoStatus.CANCELLED, error_summary=_safe_error_summary(error, "cancelled"))
            self.db.cancel_job(job.id)
            cancellation = error
        except Exception as error:
            primary_error = error
            summary = _safe_error_summary(error, "processing failed")
            self.db.update_video_status(bvid, VideoStatus.FAILED, error_summary=summary)
            self.db.fail_job(job.id, stage, summary)
        finally:
            cleanup_error: Exception | None = None
            if task_dir is not None:
                try:
                    self._remove_task_dir(task_dir)
                except Exception as error:
                    cleanup_error = error
                    self.db.record_cleanup_failure(job.id, _safe_error_summary(error, "cleanup failed"))
            if succeeded and cleanup_error is None:
                self.db.update_video_status(bvid, VideoStatus.COMPLETE)
                self.db.complete_job(job.id)
            elif succeeded and cleanup_error is not None:
                summary = _safe_error_summary(cleanup_error, "cleanup failed")
                self.db.update_video_status(bvid, VideoStatus.FAILED, error_summary=summary)
                self.db.fail_job(job.id, stage, summary)
            elif primary_error is not None and cleanup_error is not None:
                # The primary processing error stays authoritative; cleanup is separately observable.
                pass
        if cancellation is not None:
            raise cancellation

    def _advance(self, job_id: int, stage: str, progress: float) -> None:
        if not self.db.advance_job(job_id, stage, progress):
            job = self.db.get_job(job_id)
            if job is not None and job.status == "paused":
                raise JobPaused("job pause requested")
            raise JobCancelled("job was no longer runnable")

    @staticmethod
    def _raise_if_cancelled(cancel_event: Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled("runner stop requested")

    def _upsert_creator(self, creator_uid: str, source: str) -> None:
        space_url = source if "space.bilibili.com" in source else f"https://space.bilibili.com/{creator_uid}"
        existing = self.db.get_creator(creator_uid)
        display_name = (
            existing.name
            if existing is not None and existing.name and existing.name != existing.uid
            else creator_uid
        )
        self.db.upsert_creator(Creator(creator_uid, display_name, space_url))

    def _is_currently_complete(self, video: Video | None) -> bool:
        if video is None or video.status is not VideoStatus.COMPLETE:
            return False
        identity = self.db.get_transcript_identity(video.bvid)
        return identity is not None and identity[0] == _model_name(self.transcriber) and bool(identity[1])

    def _analyze(self, bvid: str, segments: tuple[TranscriptSegment, ...]) -> Any:
        if not segments:
            return SignalAnalysis(
                direction=Direction.NO_SIGNAL,
                strength=1,
                confidence=0.0,
                evidence=(),
                summary="本地转写未检测到可分析语音",
                review_status=ReviewStatus.NEEDS_REVIEW,
            )
        if isinstance(self.analyzer, MiniMaxClient):
            return run_analysis_batch(self.analyzer, [(bvid, segments)])[bvid]
        return self.analyzer.analyze(segments)

    def _make_task_dir(self) -> Path:
        root = self.temp_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        task_dir = Path(tempfile.mkdtemp(dir=root)).resolve()
        _require_descendant(task_dir, root)
        return task_dir

    def _remove_task_dir(self, task_dir: Path) -> None:
        root = self.temp_root.resolve()
        resolved = task_dir.resolve()
        _require_descendant(resolved, root)
        shutil.rmtree(resolved)


class BackgroundRunner:
    """A restart-safe one-worker executor; model concurrency remains inside MiniMaxClient."""

    def __init__(self, service: PipelineService, *, join_timeout: float = 5.0) -> None:
        self._service, self._join_timeout = service, join_timeout
        self._service.attach_runner(self)
        self._lock = RLock()
        self._queue: Queue[int] | None = None
        self._stop_event: Event | None = None
        self._worker: Thread | None = None
        self._desired_running = False
        self._generation = 0

    def start(self) -> None:
        with self._lock:
            self._desired_running = True
            if self._worker is None or not self._worker.is_alive():
                self._start_worker_locked()

    def stop(self) -> None:
        with self._lock:
            worker, stop_event = self._worker, self._stop_event
            if worker is None or stop_event is None:
                return
            self._desired_running = False
            stop_event.set()
        worker.join(timeout=self._join_timeout)

    def retry_failed(self, job_id: str) -> bool:
        if not self._service.db.retry_job(job_id):
            return False
        with self._lock:
            if self._worker is not None and self._worker.is_alive() and self._queue is not None:
                self._queue.put(int(job_id))
        return True

    def enqueue(self, job_id: int) -> None:
        """Wake the existing worker for persisted work without doing it inline."""
        with self._lock:
            if self._worker is not None and self._worker.is_alive() and self._queue is not None:
                self._queue.put(job_id)

    def _start_worker_locked(self) -> None:
        self._service.db.recover_interrupted_jobs()
        queue: Queue[int] = Queue()
        stop_event = Event()
        for job in self._service.db.list_pending_jobs():
            queue.put(job.id)
        self._generation += 1
        generation = self._generation
        self._queue, self._stop_event = queue, stop_event
        self._worker = Thread(
            target=self._run,
            args=(queue, stop_event, generation),
            name="goldbook-pipeline",
            daemon=True,
        )
        self._worker.start()

    def _run(self, queue: Queue[int], stop_event: Event, generation: int) -> None:
        try:
            while not stop_event.is_set():
                try:
                    job_id = queue.get(timeout=0.05)
                except Empty:
                    continue
                if stop_event.is_set():
                    return
                job = self._service.db.get_job(job_id)
                if job is None or job.status != "pending":
                    continue
                if job.kind == "sync_creator":
                    self._process_creator_sync(job, stop_event)
                elif job.kind == "fact_check" and job.video_bvid is not None:
                    try:
                        self._service.process_fact_check(
                            job.video_bvid, cancel_event=stop_event
                        )
                    except (JobPaused, JobCancelled, asyncio.CancelledError):
                        continue
                elif job.kind == "video" and job.video_bvid is not None:
                    try:
                        self._service.process_video(job.video_bvid, cancel_event=stop_event)
                    except (JobPaused, JobCancelled, asyncio.CancelledError):
                        continue
        finally:
            self._worker_exited(generation)

    def _worker_exited(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._worker = None
            if self._desired_running:
                self._start_worker_locked()

    def _process_creator_sync(self, job: Job, stop_event: Event) -> None:
        if not self._service.db.claim_pending_job(job.id):
            return
        try:
            PipelineService._raise_if_cancelled(stop_event)
            creator = self._service.db.get_creator(job.creator_uid)
            if creator is None:
                raise ValueError("creator was deleted before discovery")
            resolved_uid = self._service.sync_creator(creator.space_url, datetime.now(timezone.utc))
            PipelineService._raise_if_cancelled(stop_event)
            if job.creator_uid.startswith("bvid:"):
                try:
                    self._service.db.reassign_job_creator(job.id, resolved_uid)
                except Exception:
                    # A concurrent ordinary creator sync already owns the resolved line.
                    # The bootstrap job can finish without replacing that active job.
                    pass
                self._service.db.delete_creator(job.creator_uid)
            self._service.db.complete_job(job.id)
            for pending in self._service.db.list_pending_video_jobs():
                self.enqueue(pending.id)
        except (JobCancelled, asyncio.CancelledError):
            self._service.db.cancel_job(job.id)
        except Exception as error:
            self._service.db.fail_job(job.id, "discovering", _safe_error_summary(error, "creator discovery failed"))


def _model_name(transcriber: Any) -> str:
    configured = getattr(transcriber, "model_name", getattr(transcriber, "_model_name", None))
    return configured if isinstance(configured, str) and configured else type(transcriber).__name__


def _transcript_hash(segments: tuple[TranscriptSegment, ...]) -> str:
    digest = sha256()
    for segment in segments:
        digest.update(f"{segment.start_sec:.6f}\t{segment.end_sec:.6f}\t{segment.text}\n".encode("utf-8"))
    return digest.hexdigest()


def _safe_error_summary(error: BaseException, category: str) -> str:
    """Persist only a fixed category, never diagnostic text supplied by another component."""
    return f"{type(error).__name__}: {category}"[:300]


def _require_descendant(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("temporary directory must be inside temp_root") from error
    if path == root:
        raise ValueError("temporary directory must not be temp_root itself")
