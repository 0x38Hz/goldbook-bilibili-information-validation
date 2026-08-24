"""Lazy local faster-whisper transcription adapter."""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import sysconfig
from threading import Lock
from typing import Any, Callable

from goldbook.models import TranscriptSegment


ModelFactory = Callable[[str, str, str], Any]
_GPU_BATCH_SIZE = 8
_CUDA_DLL_HANDLES: list[Any] = []


class _BatchedWhisperAdapter:
    def __init__(self, pipeline: Any) -> None:
        self._pipeline = pipeline

    def transcribe(self, audio: str, **kwargs: Any) -> Any:
        return self._pipeline.transcribe(audio, batch_size=_GPU_BATCH_SIZE, **kwargs)


class WhisperTranscriber:
    """Transcribe local audio with one lazily-created faster-whisper model."""

    def __init__(
        self,
        model_name: str = "small",
        device: str = "auto",
        model_factory: ModelFactory | None = None,
        *,
        local_model_root: Path | None = None,
    ) -> None:
        self._model_name = model_name
        model_root = local_model_root or Path(__file__).resolve().parents[1] / "models"
        local_model = (model_root / f"faster-whisper-{model_name}").resolve()
        self._model_source = (
            str(local_model)
            if (local_model / "model.bin").is_file() and (local_model / "config.json").is_file()
            else model_name
        )
        self._requested_device = device
        self._model_factory = model_factory or self._default_model_factory
        self._model: Any | None = None
        self._model_lock = Lock()
        self._transcript_cache: dict[tuple[Path, str], tuple[TranscriptSegment, ...]] = {}

    def transcribe(self, audio_path: Path) -> tuple[TranscriptSegment, ...]:
        """Materialize Chinese transcription segments with their source timestamps."""
        cache_key = self._cache_key(audio_path)
        with self._model_lock:
            cached = self._transcript_cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            transcript = self._transcribe_with_model(self._get_model(), audio_path)
        except RuntimeError as error:
            if self._requested_device != "auto" or not _is_missing_cuda_runtime(error):
                raise
            with self._model_lock:
                self._model = self._model_factory(self._model_source, "cpu", "int8")
            transcript = self._transcribe_with_model(self._model, audio_path)
        with self._model_lock:
            return self._transcript_cache.setdefault(cache_key, transcript)

    def _transcribe_with_model(
        self, model: Any, audio_path: Path
    ) -> tuple[TranscriptSegment, ...]:
        segments, _info = model.transcribe(
            str(audio_path), language="zh", vad_filter=True, beam_size=5
        )
        return tuple(
            TranscriptSegment(
                start_sec=float(segment.start),
                end_sec=float(segment.end),
                text=str(segment.text).strip(),
                model=self._model_name,
            )
            for segment in segments
        )

    def _get_model(self) -> Any:
        if self._model is None:
            with self._model_lock:
                if self._model is None:
                    device, compute_type = self._runtime_options()
                    self._model = self._model_factory(self._model_source, device, compute_type)
        return self._model

    def _runtime_options(self) -> tuple[str, str]:
        if self._requested_device in {"auto", "cuda"} and self._cuda_is_available():
            return "cuda", "float16"
        return "cpu", "int8"

    @staticmethod
    def _cache_key(audio_path: Path) -> tuple[Path, str]:
        path = audio_path.resolve()
        digest = sha256()
        with path.open("rb") as audio_file:
            for chunk in iter(lambda: audio_file.read(1024 * 1024), b""):
                digest.update(chunk)
        return path, digest.hexdigest()

    @staticmethod
    def _cuda_is_available() -> bool:
        try:
            import ctranslate2

            return ctranslate2.get_cuda_device_count() > 0
        except (ImportError, AttributeError, OSError):
            return False

    @staticmethod
    def _default_model_factory(model_name: str, device: str, compute_type: str) -> Any:
        if device == "cuda":
            _configure_packaged_cuda_runtime()
        from faster_whisper import BatchedInferencePipeline, WhisperModel

        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        if device == "cuda":
            return _BatchedWhisperAdapter(BatchedInferencePipeline(model=model))
        return model


def _is_missing_cuda_runtime(error: RuntimeError) -> bool:
    message = str(error).lower()
    return "not found or cannot be loaded" in message and any(
        library in message for library in ("cublas", "cudnn", "cudart")
    )


def _configure_packaged_cuda_runtime() -> None:
    if os.name != "nt" or _CUDA_DLL_HANDLES:
        return
    cublas_bin = Path(sysconfig.get_paths()["purelib"]) / "nvidia" / "cublas" / "bin"
    if not (cublas_bin / "cublas64_12.dll").is_file():
        return
    path_text = str(cublas_bin)
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    if path_text not in path_entries:
        os.environ["PATH"] = path_text + os.pathsep + os.environ.get("PATH", "")
    add_directory = getattr(os, "add_dll_directory", None)
    if add_directory is not None:
        _CUDA_DLL_HANDLES.append(add_directory(path_text))
