from types import SimpleNamespace
import os
import sys

import goldbook.transcribe as transcribe_module
from goldbook.transcribe import WhisperTranscriber


def test_transcriber_loads_model_once_and_preserves_timestamps(tmp_path):
    loads = []

    class FakeModel:
        def transcribe(self, *_args, **_kwargs):
            return iter([
                SimpleNamespace(start=1.25, end=3.5, text=" 黄金继续上涨 "),
            ]), SimpleNamespace(language="zh")

    def factory(model_name, device, compute_type):
        loads.append((model_name, device, compute_type))
        return FakeModel()

    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF-test")
    transcriber = WhisperTranscriber("small", "cpu", factory)
    first = transcriber.transcribe(audio)
    second = transcriber.transcribe(audio)
    assert len(loads) == 1
    assert first[0].start_sec == 1.25
    assert first[0].text == "黄金继续上涨"
    assert second == first


def test_transcriber_invalidates_cached_result_when_audio_content_changes_without_stat_change(tmp_path):
    calls = []

    class FakeModel:
        def transcribe(self, audio_path, **_kwargs):
            calls.append(audio_path)
            return iter([
                SimpleNamespace(start=0, end=1, text=open(audio_path, "rb").read().decode()),
            ]), SimpleNamespace(language="zh")

    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"AAAA")
    original_stat = audio.stat()
    transcriber = WhisperTranscriber("small", "cpu", lambda *_args: FakeModel())
    first = transcriber.transcribe(audio)
    audio.write_bytes(b"BBBB")
    os.utime(audio, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    second = transcriber.transcribe(audio)
    assert first[0].text == "AAAA"
    assert second[0].text == "BBBB"
    assert len(calls) == 2


def test_transcriber_uses_cuda_float16_only_when_cuda_is_available(tmp_path, monkeypatch):
    loads = []
    calls = []

    class FakeModel:
        def transcribe(self, audio_path, **kwargs):
            calls.append((audio_path, kwargs))
            return iter([]), SimpleNamespace(language="zh")

    monkeypatch.setattr(WhisperTranscriber, "_cuda_is_available", staticmethod(lambda: True))
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")
    transcriber = WhisperTranscriber(
        "small",
        "auto",
        lambda *args: loads.append(args) or FakeModel(),
        local_model_root=tmp_path / "missing-models",
    )
    assert transcriber.transcribe(audio) == ()
    assert loads == [("small", "cuda", "float16")]
    assert calls == [(str(audio), {"language": "zh", "vad_filter": True, "beam_size": 5})]


def test_auto_device_retries_on_cpu_when_cuda_runtime_library_is_missing(tmp_path, monkeypatch):
    loads = []

    class FakeModel:
        def __init__(self, device):
            self.device = device

        def transcribe(self, *_args, **_kwargs):
            if self.device == "cuda":
                def broken_segments():
                    raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
                    yield

                return broken_segments(), SimpleNamespace(language="zh")
            return iter([SimpleNamespace(start=0, end=1, text="CPU 转写成功")]), SimpleNamespace(language="zh")

    def factory(_model_name, device, _compute_type):
        loads.append(device)
        return FakeModel(device)

    monkeypatch.setattr(WhisperTranscriber, "_cuda_is_available", staticmethod(lambda: True))
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")
    transcriber = WhisperTranscriber("small", "auto", factory)

    assert transcriber.transcribe(audio)[0].text == "CPU 转写成功"
    assert loads == ["cuda", "cpu"]


def test_default_gpu_model_uses_faster_whisper_batched_inference(monkeypatch):
    calls = []

    class FakeWhisperModel:
        def __init__(self, model_name, *, device, compute_type):
            calls.append(("model", model_name, device, compute_type))

    class FakeBatchedInferencePipeline:
        def __init__(self, model):
            calls.append(("pipeline", model.__class__.__name__))

        def transcribe(self, audio, **kwargs):
            calls.append(("transcribe", audio, kwargs))
            return iter(()), SimpleNamespace(language="zh")

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        SimpleNamespace(
            WhisperModel=FakeWhisperModel,
            BatchedInferencePipeline=FakeBatchedInferencePipeline,
        ),
    )

    model = WhisperTranscriber._default_model_factory("small", "cuda", "float16")
    model.transcribe("audio.wav", language="zh", vad_filter=True, beam_size=5)

    assert calls[-1][2]["batch_size"] == 8


def test_packaged_windows_cublas_directory_is_registered_for_native_loading(
    tmp_path, monkeypatch
):
    cublas_bin = tmp_path / "nvidia" / "cublas" / "bin"
    cublas_bin.mkdir(parents=True)
    (cublas_bin / "cublas64_12.dll").write_bytes(b"dll")
    handles = []
    monkeypatch.setattr(transcribe_module, "_CUDA_DLL_HANDLES", [])
    monkeypatch.setattr(transcribe_module.sysconfig, "get_paths", lambda: {"purelib": str(tmp_path)})
    monkeypatch.setattr(transcribe_module.os, "name", "nt")
    monkeypatch.setattr(
        transcribe_module.os,
        "add_dll_directory",
        lambda path: handles.append(path) or object(),
        raising=False,
    )
    monkeypatch.setenv("PATH", "existing")

    transcribe_module._configure_packaged_cuda_runtime()

    assert os.environ["PATH"].split(os.pathsep)[0] == str(cublas_bin)
    assert handles == [str(cublas_bin)]


def test_transcriber_prefers_a_complete_local_faster_whisper_model(tmp_path):
    local_model = tmp_path / "faster-whisper-small"
    local_model.mkdir()
    (local_model / "model.bin").write_bytes(b"weights")
    (local_model / "config.json").write_text("{}", encoding="utf-8")
    loads = []

    class FakeModel:
        def transcribe(self, *_args, **_kwargs):
            return iter([]), SimpleNamespace(language="zh")

    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")
    transcriber = WhisperTranscriber(
        "small",
        "cpu",
        lambda *args: loads.append(args) or FakeModel(),
        local_model_root=tmp_path,
    )

    assert transcriber.transcribe(audio) == ()
    assert loads == [(str(local_model.resolve()), "cpu", "int8")]
