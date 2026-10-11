"""Bounded, local media pre-processing with explicitly mock adapters.

This module creates reproducible derived media and invokes optional adapters
behind bounded subprocesses. It never downloads models or makes direct outbound
network requests.
"""

from __future__ import annotations

import array
import functools
import json
import logging
import multiprocessing
import os
import re
import shutil
import subprocess
import signal
import sys
import struct
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .security import safe_error_trace
from .tesseract_ocr import ocr_selected_frame_count, ocr_timeout_policy




MAX_DURATION_SECONDS = 300.0
MAX_WIDTH = 4096
MAX_HEIGHT = 4096
MAX_FRAME_RATE = 60.0
MIN_USEFUL_WIDTH = 320
MIN_USEFUL_HEIGHT = 180
COMMAND_TIMEOUT_SECONDS = 120.0
OPTIONAL_MODULE_TIMEOUT_SECONDS = 10.0


def _asr_timeout_from_env() -> float:
    try:
        value = float(os.environ.get("MINGJIAN_ASR_PIPELINE_TIMEOUT_SECONDS", "135"))
    except ValueError:
        return 135.0
    return value if 15.0 <= value <= 180.0 else 135.0


ASR_MODULE_TIMEOUT_SECONDS = _asr_timeout_from_env()
MAX_DERIVED_BYTES = 30 * 1024 * 1024
MAX_STDERR_BYTES = 16 * 1024
DERIVED_MEDIA_SUFFIXES = {".aac", ".jpg", ".jpeg", ".m4a", ".mp3", ".mp4", ".png", ".wav", ".webm", ".webp"}
NETWORK_PROTOCOL_PREFIX = re.compile(r"(?i)^(?:https?|tcp|udp|ftp|rtmp|rtsp|srt|gopher):")
SAFE_PROTOCOL_WHITELIST = "file,pipe"
THREAD_LIMIT = "2"
FFPROBE_THREAD_ARGS = ["-threads", THREAD_LIMIT]
FFMPEG_THREAD_ARGS = ["-threads", THREAD_LIMIT, "-filter_threads", THREAD_LIMIT, "-filter_complex_threads", THREAD_LIMIT]
ENCODER_THREAD_ARGS = ["-threads", THREAD_LIMIT]

logger = logging.getLogger(__name__)


def _optional_module_child(connection: Any, adapter: Any, method_name: str, args: tuple[Any, ...]) -> None:
    try:
        value = getattr(adapter, method_name)(*args)
        connection.send(("ok", value))
    except BaseException as error:
        connection.send(("error", safe_error_trace(error)))
    finally:
        connection.close()


def _run_optional_module(adapter: Any, method_name: str, args: tuple[Any, ...], timeout_seconds: float) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_optional_module_child, args=(child, adapter, method_name, args), daemon=True)
    try:
        process.start()
        child.close()
        if not parent.poll(timeout_seconds):
            process.terminate()
            process.join(timeout=1.0)
            if process.is_alive():
                process.kill()
                process.join(timeout=1.0)
            raise TimeoutError(f"optional module exceeded {timeout_seconds:.0f} seconds")
        state, value = parent.recv()
        process.join(timeout=1.0)
        if state != "ok":
            raise RuntimeError(str(value))
        if not isinstance(value, dict):
            raise TypeError("optional module returned non-object JSON")
        return value
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=1.0)


def safe_run(name: str, fallback: Callable[[str], dict[str, Any]], *, timeout_seconds: float = OPTIONAL_MODULE_TIMEOUT_SECONDS) -> Callable[[Callable[..., tuple[Any, str, tuple[Any, ...]]]], Callable[..., dict[str, Any]]]:
    """Decorate an adapter call specification with a bounded process boundary."""
    def decorate(builder: Callable[..., tuple[Any, str, tuple[Any, ...]]]) -> Callable[..., dict[str, Any]]:
        @functools.wraps(builder)
        def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                adapter, method_name, call_args = builder(*args, **kwargs)
                return _run_optional_module(adapter, method_name, call_args, timeout_seconds)
            except BaseException as error:
                trace = safe_error_trace(error)
                logger.warning("optional module %s failed: %s", name, trace)
                return fallback(trace)
        return wrapped
    return decorate


def _face_fallback(trace: str) -> dict[str, Any]:
    return {"schema_version": "face-detection-result-v1", "mock": False, "status": "unavailable", "tracks": [], "frames_considered": 0, "limitations": ["人脸检测模块未完成；无人脸不能据此推断低风险。"], "error_trace": trace}


def _ocr_fallback(trace: str) -> dict[str, Any]:
    return {"schema_version": "ocr-result-v1", "mock": False, "status": "unavailable", "items": [], "frames_considered": 0, "limitations": ["OCR 模块未完成；未输出文字识别结论。"], "error_trace": trace}


def _asr_fallback(trace: str) -> dict[str, Any]:
    return {"schema_version": "asr-result-v1", "mock": False, "status": "unavailable", "transcript": "", "language": "unknown", "segments": [], "confidence": None, "limitations": ["ASR 模块未完成；未输出转写结论。"], "error_trace": trace}


@safe_run("face", _face_fallback)
def _run_face(adapter: Any, frames: list[dict[str, Any]]) -> tuple[Any, str, tuple[Any, ...]]:
    return adapter, "detect_and_track", (frames,)


def _run_ocr(adapter: Any, frames: list[dict[str, Any]]) -> dict[str, Any]:
    timeout_seconds = float(ocr_timeout_policy(ocr_selected_frame_count(frames))["outer"])
    try:
        return _run_optional_module(adapter, "extract", (frames,), timeout_seconds)
    except BaseException as error:
        trace = safe_error_trace(error)
        logger.warning("optional module ocr failed: %s", trace)
        return _ocr_fallback(trace)


@safe_run("asr", _asr_fallback, timeout_seconds=ASR_MODULE_TIMEOUT_SECONDS)
def _run_asr(adapter: Any, audio: Path) -> tuple[Any, str, tuple[Any, ...]]:
    return adapter, "transcribe", (audio,)


class PreprocessError(RuntimeError):
    def __init__(self, code: str, message: str, result: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.result = result


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stderr: str
    stdout: str = ""
    cancelled: bool = False
    timed_out: bool = False


class CommandRunner(Protocol):
    def run(self, command: list[str], timeout_seconds: float, cancelled: Callable[[], bool]) -> CommandResult:
        """Run a command without a shell and return bounded output."""


class SubprocessCommandRunner:
    """Small no-shell runner with timeout, cancellation and a constrained environment.

    Only the internally constructed FFmpeg/FFprobe commands are accepted. POSIX
    child process groups are started through an exec-only helper that applies
    CPU, address-space, file-size, and descriptor limits before
    launching FFmpeg. This avoids unsafe ``preexec_fn`` use in the threaded
    worker; cancellation/timeout terminates the entire process group.
    Docker additionally runs this service as a non-root account with dropped
    capabilities, read-only root filesystem, and resource limits.
    """

    def run(self, command: list[str], timeout_seconds: float, cancelled: Callable[[], bool]) -> CommandResult:
        if cancelled():
            return CommandResult(-1, "Cancelled before command start.", cancelled=True)
        if not command or command[0] not in {"ffmpeg", "ffprobe"} or any(not isinstance(item, str) or "\x00" in item for item in command):
            return CommandResult(126, "Command rejected by FFmpeg allow-list.")
        if any(NETWORK_PROTOCOL_PREFIX.match(item) for item in command[1:]):
            return CommandResult(126, "Network protocol rejected by FFmpeg sandbox.")
        if "-protocol_whitelist" not in command or command[command.index("-protocol_whitelist") + 1:command.index("-protocol_whitelist") + 2] != [SAFE_PROTOCOL_WHITELIST]:
            return CommandResult(126, "FFmpeg protocol whitelist is missing or unsafe.")
        environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP") if key in os.environ}
        environment["LANG"] = "C"
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "shell": False,
            "env": environment,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(self._spawn_command(command), **kwargs)
        except FileNotFoundError as error:
            return CommandResult(127, str(error))
        started = time.monotonic()
        # communicate(timeout=...) drains stdout/stderr while FFmpeg is still
        # running.  Polling alone can deadlock on Windows once showinfo fills
        # the inherited pipe buffer.
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.05)
                return CommandResult(process.returncode, _truncate(stderr), _truncate(stdout))
            except subprocess.TimeoutExpired:
                if cancelled():
                    _terminate(process)
                    _, stderr = process.communicate()
                    return CommandResult(-1, _truncate(stderr), cancelled=True)
                if time.monotonic() - started > timeout_seconds:
                    _terminate(process)
                    _, stderr = process.communicate()
                    return CommandResult(-1, _truncate(stderr), timed_out=True)

    @staticmethod
    def _spawn_command(command: list[str], *, platform_name: str | None = None) -> list[str]:
        """Use the rlimit helper only where POSIX resource limits exist.

        Windows launches FFmpeg/FFprobe directly. Wrapping it in another Python
        process has caused native 0xC0000005 crashes in real media smoke tests.
        The direct path still has no shell, a restricted environment, a new
        process group, wall-clock cancellation, and the protocol whitelist.
        """
        if (platform_name or os.name) == "nt":
            return command
        helper = Path(__file__).with_name("ffmpeg_sandbox.py")
        return [sys.executable, str(helper), "--", *command]


def _terminate(process: subprocess.Popen[str]) -> None:
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()


def _truncate(value: str) -> str:
    return value[:MAX_STDERR_BYTES]


CANONICAL_EXTENSION_MIMES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "video/mp4",
    ".mov": "video/quicktime", ".webm": "video/webm",
}
EXTENSION_MAGIC = {
    ".jpg": {"jpeg"}, ".jpeg": {"jpeg"}, ".png": {"png"}, ".webp": {"webp"},
    ".wav": {"wav"}, ".mp3": {"mp3"}, ".m4a": {"isobmff"}, ".mp4": {"isobmff"},
    ".mov": {"isobmff"}, ".webm": {"webm"},
}
CLIENT_MIMES = {
    ".jpg": {"image/jpeg"}, ".jpeg": {"image/jpeg"}, ".png": {"image/png"},
    ".webp": {"image/webp"}, ".wav": {"audio/wav", "audio/x-wav"},
    ".mp3": {"audio/mpeg"}, ".m4a": {"audio/mp4", "audio/x-m4a"},
    ".mp4": {"video/mp4"}, ".mov": {"video/quicktime"}, ".webm": {"video/webm"},
}


def sniff_magic(header: bytes) -> str | None:
    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return "wav"
    if header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0):
        return "mp3"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "isobmff"
    if header.startswith(b"\x1aE\xdf\xa3") and b"webm" in header[:256].lower():
        return "webm"
    return None


def image_dimensions(path: Path, media_magic: str) -> tuple[int, int] | None:
    content = path.read_bytes()[:256 * 1024]
    if media_magic == "png" and len(content) >= 24:
        return struct.unpack(">II", content[16:24])
    if media_magic == "webp":
        return webp_dimensions(content)
    if media_magic == "jpeg":
        index = 2
        while index + 9 < len(content):
            if content[index] != 0xFF:
                index += 1
                continue
            marker = content[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(content):
                break
            length = int.from_bytes(content[index:index + 2], "big")
            if length < 2 or index + length > len(content):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                return (int.from_bytes(content[index + 5:index + 7], "big"), int.from_bytes(content[index + 3:index + 5], "big"))
            index += length
    return None


def webp_dimensions(content: bytes) -> tuple[int, int] | None:
    """Read dimensions from the common WebP chunk variants without decoding pixels.

    A RIFF/WebP signature alone deliberately does not make this a valid image.
    Unknown, truncated or malformed chunks return ``None`` so ffprobe performs
    the authoritative container/decode check rather than rejecting valid WebP
    files merely because they do not use VP8X.
    """
    if len(content) < 20 or content[:4] != b"RIFF" or content[8:12] != b"WEBP":
        return None
    offset = 12
    while offset + 8 <= len(content):
        chunk_type = content[offset:offset + 4]
        chunk_size = int.from_bytes(content[offset + 4:offset + 8], "little")
        payload_start = offset + 8
        payload_end = payload_start + chunk_size
        if payload_end > len(content):
            return None
        payload = content[payload_start:payload_end]
        if chunk_type == b"VP8X" and len(payload) >= 10:
            width = 1 + int.from_bytes(payload[4:7], "little")
            height = 1 + int.from_bytes(payload[7:10], "little")
            return (width, height) if width > 0 and height > 0 else None
        if chunk_type == b"VP8 " and len(payload) >= 10 and payload[3:6] == b"\x9d\x01\x2a":
            width = int.from_bytes(payload[6:8], "little") & 0x3FFF
            height = int.from_bytes(payload[8:10], "little") & 0x3FFF
            return (width, height) if width > 0 and height > 0 else None
        if chunk_type == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:
            bits = int.from_bytes(payload[1:5], "little")
            width = 1 + (bits & 0x3FFF)
            height = 1 + ((bits >> 14) & 0x3FFF)
            return (width, height) if width > 0 and height > 0 else None
        offset = payload_end + (chunk_size % 2)
    return None


def validate_upload_header(suffix: str, content_type: str, header: bytes) -> tuple[str, str]:
    if suffix not in EXTENSION_MAGIC:
        raise PreprocessError("unsupported_extension", "Unsupported file extension.")
    if content_type and content_type not in CLIENT_MIMES[suffix]:
        raise PreprocessError("declared_mime_mismatch", "File content type does not match its allowed extension.")
    media_magic = sniff_magic(header)
    if media_magic not in EXTENSION_MAGIC[suffix]:
        raise PreprocessError("magic_mismatch", "File signature does not match its extension.")
    # ISO-BMFF carries an MP4-family signature shared by M4A, MP4 and MOV.  The
    # validated extension selects the canonical MIME; it is not guessed from the
    # generic family signature alone.
    return media_magic, CANONICAL_EXTENSION_MIMES[suffix]


class FaceAdapter(Protocol):
    def detect_and_track(self, frame_index: list[dict[str, Any]]) -> dict[str, Any]: ...


class VadAdapter(Protocol):
    def analyse(self, audio_path: Path) -> dict[str, Any]: ...


class AsrAdapter(Protocol):
    def transcribe(self, audio_path: Path) -> dict[str, Any]: ...


class OcrAdapter(Protocol):
    def extract(self, frame_index: list[dict[str, Any]]) -> dict[str, Any]: ...


class MockFaceAdapter:
    def detect_and_track(self, frame_index: list[dict[str, Any]]) -> dict[str, Any]:
        return {"mock": True, "status": "unavailable", "tracks": [], "limitation": "未配置人脸检测/跟踪模型；无人脸不能据此推断低风险。", "frames_considered": len(frame_index)}


class MockVadAdapter:
    def analyse(self, audio_path: Path) -> dict[str, Any]:
        return {"mock": True, "status": "unavailable", "segments": [], "limitation": "未配置 VAD 模型；未输出语音结论。"}


class EnergyVadAdapter:
    """A deterministic local energy VAD, not an ASR or authenticity model."""

    window_seconds = 0.03
    merge_gap_seconds = 0.20
    min_segment_seconds = 0.12

    def analyse(self, audio_path: Path) -> dict[str, Any]:
        try:
            with wave.open(str(audio_path), "rb") as reader:
                channels, width, rate, frames = reader.getnchannels(), reader.getsampwidth(), reader.getframerate(), reader.getnframes()
                raw = reader.readframes(frames)
        except (OSError, wave.Error) as error:
            return {"mock": False, "status": "unavailable", "segments": [], "limitation": "标准化音频无法由本地 VAD 读取；这不是低风险证据。", "quality": {"status": "unavailable", "reasons": [str(error)[:160]]}}
        if channels != 1 or width != 2 or rate <= 0:
            return {"mock": False, "status": "unavailable", "segments": [], "limitation": "标准化音频不是预期的 16-bit 单声道 WAV；未输出语音片段。", "quality": {"status": "unavailable", "reasons": ["WAV 参数不符合预处理契约。"]}}
        values = array.array("h")
        values.frombytes(raw)
        duration = len(values) / rate
        if not values:
            return {"mock": False, "status": "unavailable", "segments": [], "limitation": "音频为空；这不是低风险证据。", "quality": {"status": "unavailable", "reasons": ["没有 PCM 样本。"]}}
        window = max(1, int(rate * self.window_seconds))
        rms = [((sum(sample * sample for sample in values[offset:offset + window]) / max(1, len(values[offset:offset + window]))) ** 0.5) for offset in range(0, len(values), window)]
        peak = max(rms)
        threshold = max(180.0, peak * 0.12)
        active = [index for index, value in enumerate(rms) if value >= threshold]
        groups: list[list[int]] = []
        for index in active:
            if not groups or (index - groups[-1][-1]) * self.window_seconds > self.merge_gap_seconds:
                groups.append([index])
            else:
                groups[-1].append(index)
        segments = []
        for group in groups:
            start, end = group[0] * self.window_seconds, min(duration, (group[-1] + 1) * self.window_seconds)
            if end - start >= self.min_segment_seconds:
                segments.append({"kind": "speech", "start_seconds": round(start, 6), "end_seconds": round(end, 6), "duration_seconds": round(end - start, 6), "method": "local_energy_vad_v1"})
        speech_seconds = sum(float(item["duration_seconds"]) for item in segments)
        reasons: list[str] = []
        status = "available"
        if duration < 0.25:
            status, reasons = "degraded", ["音频短于 0.25 秒；VAD 和后续音频分析受限。"]
        elif not segments:
            status, reasons = "degraded", ["未识别到满足本地能量阈值的语音片段；静音/音乐/低信噪比不能推断低风险。"]
        return {"schema_version": "vad-result-v1", "mock": False, "status": status, "segments": segments, "limitation": "这是本地能量 VAD，只用于划分候选语音区间，不提供 ASR、说话人或真实性结论。", "quality": {"status": status, "duration_seconds": round(duration, 6), "sample_rate_hz": rate, "window_seconds": self.window_seconds, "energy_threshold": round(threshold, 3), "speech_seconds": round(speech_seconds, 6), "speech_ratio": round(speech_seconds / duration, 6), "reasons": reasons}}


class MockAsrAdapter:
    def transcribe(self, audio_path: Path) -> dict[str, Any]:
        return {"mock": True, "status": "unavailable", "transcript": None, "limitation": "未配置 ASR 模型；未输出文本或可信度结论。"}


class MockOcrAdapter:
    def extract(self, frame_index: list[dict[str, Any]]) -> dict[str, Any]:
        return {"mock": True, "status": "unavailable", "items": [], "limitation": "未配置 OCR 模型；未输出文字识别结论。", "frames_considered": len(frame_index)}


class MediaPreprocessor:
    def __init__(self, data_dir: Path, runner: CommandRunner | None = None, *, face: FaceAdapter | None = None, vad: VadAdapter | None = None, asr: AsrAdapter | None = None, ocr: OcrAdapter | None = None, max_derived_bytes: int = MAX_DERIVED_BYTES) -> None:
        self.data_dir = data_dir
        self.derived_dir = data_dir / "derived"
        if max_derived_bytes <= 0:
            raise ValueError("max_derived_bytes must be positive.")
        self.max_derived_bytes = max_derived_bytes
        self.runner = runner or SubprocessCommandRunner()
        self.face, self.vad, self.asr, self.ocr = face or MockFaceAdapter(), vad or EnergyVadAdapter(), asr or MockAsrAdapter(), ocr or MockOcrAdapter()

    def process(self, job: dict[str, Any], cancelled: Callable[[], bool]) -> dict[str, Any]:
        if job["source"]["type"] == "url":
            return {"status": "skipped", "reason": "URL 引用任务不访问网络，未执行媒体预处理。", "artifacts": [], "quality_gates": [{"name": "media_input", "status": "unavailable", "reason": "未取得 URL 媒体。"}], "commands": []}
        job_id = job["id"]
        source = self._source_path(job)
        suffix = job["source"].get("media_extension")
        if not suffix:
            raise PreprocessError("legacy_media_metadata_missing", "Legacy upload lacks its validated extension metadata and cannot be safely pre-processed.")
        output = self.derived_dir / job_id
        self._safe_reset(output)
        output.mkdir(parents=True, exist_ok=True)
        result: dict[str, Any] = {"status": "running", "version": "phase-2-preprocess-v1", "artifacts": [], "quality_gates": [], "commands": [], "input_features": {"size_bytes": source.stat().st_size, "extension": suffix}}
        try:
            with source.open("rb") as stream:
                header = stream.read(4096)
            media_magic, inferred_mime = validate_upload_header(suffix, "", header)
            result["input_features"].update({"magic": media_magic, "inferred_mime": inferred_mime})
            intrinsic = image_dimensions(source, media_magic)
            if intrinsic:
                result["input_features"].update({"width": intrinsic[0], "height": intrinsic[1]})
                if intrinsic[0] > MAX_WIDTH or intrinsic[1] > MAX_HEIGHT:
                    raise PreprocessError("resolution_exceeded", f"Image header exceeds {MAX_WIDTH}x{MAX_HEIGHT}.", result)
            elif media_magic in {"jpeg", "png"}:
                raise PreprocessError("invalid_image_header", "Image header has no valid dimensions.", result)
            probe = self._probe(source, output, result, cancelled)
            features = self._validate_probe(probe, suffix, media_magic, result["input_features"])
            result["input_features"].update(features)
            self._quality_gates(result, features)
            normalized = self._normalize(source, suffix, features, output, result, cancelled)
            frames, shots = self._frames_and_shots(normalized, features, output, result, cancelled)
            self._write_json(output / "shot_list.json", shots, result, "shot_list")
            self._write_json(output / "frame_index.json", frames, result, "frame_index")
            if frames:
                faces = self.face.detect_and_track(frames) if isinstance(self.face, MockFaceAdapter) else _run_face(self.face, frames)
                ocr = self.ocr.extract(frames) if isinstance(self.ocr, MockOcrAdapter) else _run_ocr(self.ocr, frames)
            else:
                faces = {"mock": isinstance(self.face, MockFaceAdapter), "status": "not_applicable", "tracks": [], "limitations": ["没有可用视频帧；无人脸不表示低风险。"]}
                ocr = {"mock": isinstance(self.ocr, MockOcrAdapter), "status": "not_applicable", "items": [], "limitations": ["没有可用视频帧；未执行 OCR。"]}
            self._face_quality_gate(result, faces)
            self._write_json(output / "faces.json", faces, result, "faces")
            self._write_json(output / "ocr.json", ocr, result, "ocr")
            audio = self._extract_audio(normalized, features, output, result, cancelled)
            if audio:
                vad = self.vad.analyse(audio)
                self._write_json(output / "vad.json", vad, result, "vad")
                self._audio_vad_quality_gate(result, vad)
                asr = self.asr.transcribe(audio) if isinstance(self.asr, MockAsrAdapter) else _run_asr(self.asr, audio)
                self._write_json(output / "asr.json", asr, result, "asr")
            else:
                missing = {"mock": False, "status": "not_applicable", "segments": [], "limitation": "输入没有可用音频；音频分析不适用，不代表低风险。"}
                self._write_json(output / "vad.json", missing, result, "vad")
                self._write_json(output / "asr.json", missing, result, "asr")
            result["status"] = "completed"
            self._write_json(output / "metadata.json", result, result, "metadata")
            return result
        except PreprocessError as error:
            failed = error.result or result
            failed["status"] = "cancelled" if error.code == "preprocess_cancelled" else "failed"
            failed["failure"] = {"code": error.code, "message": str(error)}
            self._write_json(output / "metadata.json", failed, failed, "metadata", enforce_budget=False)
            self._enforce_output_budget(output, failed)
            error.result = failed
            raise

    def _source_path(self, job: dict[str, Any]) -> Path:
        path = (self.data_dir / job["source"]["object_key"]).resolve()
        try:
            path.relative_to(self.data_dir.resolve())
        except ValueError as error:
            raise PreprocessError("unsafe_object_path", "Stored object path is outside the data directory.") from error
        if not path.is_file():
            raise PreprocessError("source_missing", "Stored source object is missing.")
        return path

    def _safe_reset(self, output: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            output.resolve().relative_to(self.derived_dir.resolve())
        except ValueError as error:
            raise PreprocessError("unsafe_artifact_path", "Derived artifact path escaped its root.") from error
        if output.exists():
            shutil.rmtree(output)

    def _probe(self, source: Path, output: Path, result: dict[str, Any], cancelled: Callable[[], bool]) -> dict[str, Any]:
        command = ["ffprobe", "-v", "error", "-protocol_whitelist", SAFE_PROTOCOL_WHITELIST, *FFPROBE_THREAD_ARGS, "-show_entries", "format=format_name,duration:stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,sample_rate,channels", "-of", "json", str(source)]
        completed = self._run_command("ffprobe", command, output, result, cancelled)
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise PreprocessError("invalid_probe_output", "ffprobe did not return valid JSON.", result) from error

    def _run_command(self, name: str, command: list[str], output: Path, result: dict[str, Any], cancelled: Callable[[], bool]) -> CommandResult:
        completed = self.runner.run(command, COMMAND_TIMEOUT_SECONDS, cancelled)
        stderr_path = output / f"{len(result['commands']) + 1:02d}-{name}.stderr.log"
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        record = {"name": name, "command": [command[0], *["<media-path>" if Path(item).is_absolute() else item for item in command[1:]]], "returncode": completed.returncode, "stderr_artifact": self._relative(stderr_path)}
        result["commands"].append(record)
        self._add_artifact(result, name + "_stderr", stderr_path)
        over_budget = self._enforce_output_budget(output, result)
        if completed.cancelled:
            self._cleanup_excess_media(output, result)
            raise PreprocessError("preprocess_cancelled", "Pre-processing was cancelled.", result)
        if over_budget:
            raise PreprocessError("derived_size_exceeded", f"Derived artifacts exceeded the {self.max_derived_bytes}-byte limit and media outputs were removed.", result)
        if completed.timed_out:
            self._cleanup_excess_media(output, result)
            raise PreprocessError("ffmpeg_timeout", f"{name} exceeded the {COMMAND_TIMEOUT_SECONDS}-second limit.", result)
        if completed.returncode:
            self._cleanup_excess_media(output, result)
            code = "tool_unavailable" if completed.returncode == 127 else f"{name}_failed"
            raise PreprocessError(code, f"{name} failed; see retained stderr artifact.", result)
        return completed

    def _validate_probe(self, probe: dict[str, Any], suffix: str, media_magic: str, intrinsic: dict[str, Any]) -> dict[str, Any]:
        fmt = probe.get("format") or {}
        streams = probe.get("streams") or []
        format_names = set(str(fmt.get("format_name", "")).split(","))
        expected = {".jpg": {"jpeg_pipe", "image2"}, ".jpeg": {"jpeg_pipe", "image2"}, ".png": {"png_pipe", "image2"}, ".webp": {"webp_pipe", "image2"}, ".wav": {"wav"}, ".mp3": {"mp3"}, ".m4a": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}, ".mp4": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}, ".mov": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}, ".webm": {"matroska", "webm"}}[suffix]
        if not format_names.intersection(expected):
            raise PreprocessError("container_mismatch", "Declared extension and decoded container do not match.")
        video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
        audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
        is_image = media_magic in {"jpeg", "png", "webp"}
        if is_image and not video:
            raise PreprocessError("container_missing_video_stream", "Image container has no decoded image stream.")
        if media_magic in {"wav", "mp3"} and not audio:
            raise PreprocessError("container_missing_audio_stream", "Audio container has no decoded audio stream.")
        if suffix in {".mp4", ".mov", ".webm"} and not video:
            raise PreprocessError("container_missing_video_stream", "Video container has no decoded video stream.")
        width, height = (video or {}).get("width"), (video or {}).get("height")
        if video and (not isinstance(width, int) or not isinstance(height, int) or width < 1 or height < 1):
            raise PreprocessError("invalid_dimensions", "Decoded video/image dimensions are invalid.")
        if video and (width > MAX_WIDTH or height > MAX_HEIGHT):
            raise PreprocessError("resolution_exceeded", f"Dimensions exceed {MAX_WIDTH}x{MAX_HEIGHT}.")
        duration = _number(fmt.get("duration"))
        if not is_image and (duration is None or duration <= 0):
            raise PreprocessError("invalid_duration", "Media duration is missing or invalid.")
        if duration is not None and duration > MAX_DURATION_SECONDS:
            raise PreprocessError("duration_exceeded", f"Duration exceeds {MAX_DURATION_SECONDS} seconds.")
        rate = _parse_rate((video or {}).get("avg_frame_rate") or (video or {}).get("r_frame_rate")) if video else None
        if not is_image and video and (rate is None or rate <= 0):
            raise PreprocessError("invalid_frame_rate", "Video frame rate is missing or invalid.")
        if rate is not None and rate > MAX_FRAME_RATE:
            raise PreprocessError("frame_rate_exceeded", f"Frame rate exceeds {MAX_FRAME_RATE} fps.")
        return {"container": sorted(format_names), "duration_seconds": duration, "width": width, "height": height, "frame_rate": rate, "has_audio": bool(audio), "has_video": bool(video), "video_codec": (video or {}).get("codec_name"), "audio_codec": (audio or {}).get("codec_name")}

    def _quality_gates(self, result: dict[str, Any], features: dict[str, Any]) -> None:
        if features.get("has_video") and (features["width"] < MIN_USEFUL_WIDTH or features["height"] < MIN_USEFUL_HEIGHT):
            result["quality_gates"].append({"name": "resolution", "status": "degraded", "reason": "低清输入：后续分析能力受限，不构成低风险证据。"})
        else:
            result["quality_gates"].append({"name": "resolution", "status": "available"})
        if features.get("has_video") and not features.get("has_audio"):
            result["quality_gates"].append({"name": "audio", "status": "not_applicable", "reason": "无音频：音频分析不适用，不能据此推断低风险。"})
        elif features.get("has_audio"):
            result["quality_gates"].append({"name": "audio", "status": "available"})
        else:
            result["quality_gates"].append({"name": "audio", "status": "not_applicable"})
        result["quality_gates"].append({"name": "face", "status": "pending" if not isinstance(self.face, MockFaceAdapter) else "unavailable", "reason": "人脸模块默认关闭；无人脸不能据此推断低风险。" if isinstance(self.face, MockFaceAdapter) else "等待人脸检测结果。"})

    @staticmethod
    def _face_quality_gate(result: dict[str, Any], faces: dict[str, Any]) -> None:
        gate = next((item for item in result["quality_gates"] if item.get("name") == "face"), None)
        if gate is None:
            return
        tracks = faces.get("tracks") if isinstance(faces.get("tracks"), list) else []
        if faces.get("status") == "available" and tracks:
            gate.update({"status": "available", "reason": "YuNet 已运行并输出可审计人脸框。"})
        elif faces.get("status") == "not_applicable":
            gate.update({"status": "not_applicable", "reason": "输入没有可分析视觉帧。"})
        else:
            gate.update({"status": "unavailable", "reason": "人脸模块不可用或未检测到人脸；不能据此推断低风险。"})

    @staticmethod
    def _audio_vad_quality_gate(result: dict[str, Any], vad: dict[str, Any]) -> None:
        quality = vad.get("quality") if isinstance(vad, dict) else None
        status = quality.get("status") if isinstance(quality, dict) else vad.get("status", "unavailable")
        reasons = quality.get("reasons", []) if isinstance(quality, dict) else []
        gate: dict[str, Any] = {"name": "audio_vad", "status": status}
        if reasons:
            gate["reason"] = "；".join(str(item) for item in reasons)
        result["quality_gates"].append(gate)

    def _normalize(self, source: Path, suffix: str, features: dict[str, Any], output: Path, result: dict[str, Any], cancelled: Callable[[], bool]) -> Path:
        if features["has_video"] and suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            target = output / "normalized.mp4"
            command = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-protocol_whitelist", SAFE_PROTOCOL_WHITELIST, *FFMPEG_THREAD_ARGS, "-i", str(source), "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", *ENCODER_THREAD_ARGS, str(target)]
        elif features["has_audio"] and not features["has_video"]:
            target = output / "normalized.wav"
            command = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-protocol_whitelist", SAFE_PROTOCOL_WHITELIST, *FFMPEG_THREAD_ARGS, "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", *ENCODER_THREAD_ARGS, str(target)]
        else:
            target = output / "normalized.png"
            command = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-protocol_whitelist", SAFE_PROTOCOL_WHITELIST, *FFMPEG_THREAD_ARGS, "-i", str(source), "-frames:v", "1", "-c:v", "png", *ENCODER_THREAD_ARGS, str(target)]
        self._run_command("ffmpeg_normalize", command, output, result, cancelled)
        if not target.is_file() or target.stat().st_size == 0:
            raise PreprocessError("normalized_output_missing", "FFmpeg did not create normalized media.", result)
        self._add_artifact(result, "normalized_media", target)
        return target

    def _frames_and_shots(self, normalized: Path, features: dict[str, Any], output: Path, result: dict[str, Any], cancelled: Callable[[], bool]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not features["has_video"]:
            return [], []
        if normalized.suffix == ".png":
            frame = output / "frames" / "frame-000001.png"
            frame.parent.mkdir(exist_ok=True)
            shutil.copyfile(normalized, frame)
            self._raise_if_over_budget(output, result)
        else:
            frame = output / "frames" / "frame-%06d.jpg"
            frame.parent.mkdir(exist_ok=True)
            keyframes = self._run_command("ffmpeg_keyframes", ["ffmpeg", "-nostdin", "-y", "-v", "info", "-protocol_whitelist", SAFE_PROTOCOL_WHITELIST, *FFMPEG_THREAD_ARGS, "-i", str(normalized), "-vf", "fps=1/2,showinfo", "-frames:v", "150", *ENCODER_THREAD_ARGS, str(frame)], output, result, cancelled)
        produced = sorted((output / "frames").glob("frame-*"))
        if not produced:
            raise PreprocessError("keyframe_output_missing", "No keyframes were produced.", result)
        if normalized.suffix == ".png":
            timestamp_values, timestamp_quality = [0.0], "exact"
        else:
            timestamp_values, timestamp_quality = self._frame_timestamps(keyframes.stderr, len(produced), float(features.get("duration_seconds") or 0))
        frames = [{"index": number + 1, "timestamp_seconds": timestamp_values[number], "timestamp_quality": timestamp_quality, "path": self._relative(path)} for number, path in enumerate(produced)]
        for path in produced:
            self._add_artifact(result, "keyframe", path)
            self._raise_if_over_budget(output, result)
        if normalized.suffix == ".png":
            return frames, [{"shot_id": 1, "start_seconds": 0.0, "end_seconds": 0.0, "keyframe_index": 1, "method": "single_image"}]
        # A null muxer keeps scene detection valid even when no frame crosses
        # the threshold.  Image2 otherwise treats an empty selection as an
        # encoder failure, turning a normal single-shot video into a job error.
        completed = self._run_command("ffmpeg_scene_cut", ["ffmpeg", "-nostdin", "-v", "info", "-protocol_whitelist", SAFE_PROTOCOL_WHITELIST, *FFMPEG_THREAD_ARGS, "-i", str(normalized), "-vf", "select='gt(scene,0.30)',showinfo", "-an", "-f", "null", "-"], output, result, cancelled)
        duration = float(features.get("duration_seconds") or 0)
        scene_times = [float(value) for value in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", completed.stderr)]
        starts = [0.0] + sorted({min(timestamp, duration) for timestamp in scene_times if 0 < timestamp < duration})
        if len(starts) == 1:
            return frames, [{"shot_id": 1, "start_seconds": 0.0, "end_seconds": duration, "keyframe_index": 1, "method": "scene_cut_fallback"}]
        return frames, [{"shot_id": index + 1, "start_seconds": start, "end_seconds": starts[index + 1] if index + 1 < len(starts) else duration, "keyframe_index": self._nearest_frame_index(frames, start), "method": "ffmpeg_scene_threshold_0.30", "timestamp_quality": "exact"} for index, start in enumerate(starts)]

    def _extract_audio(self, normalized: Path, features: dict[str, Any], output: Path, result: dict[str, Any], cancelled: Callable[[], bool]) -> Path | None:
        if not features["has_audio"]:
            return None
        if normalized.suffix == ".wav":
            target = normalized
        else:
            target = output / "audio.wav"
            self._run_command("ffmpeg_audio_extract", ["ffmpeg", "-nostdin", "-y", "-v", "error", "-protocol_whitelist", SAFE_PROTOCOL_WHITELIST, *FFMPEG_THREAD_ARGS, "-i", str(normalized), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", *ENCODER_THREAD_ARGS, str(target)], output, result, cancelled)
        if not target.is_file() or target.stat().st_size == 0:
            raise PreprocessError("audio_output_missing", "Audio extraction produced no audio artifact.", result)
        self._add_artifact(result, "normalized_audio", target)
        return target

    def _write_json(self, path: Path, value: Any, result: dict[str, Any], name: str, *, enforce_budget: bool = True) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        self._add_artifact(result, name, path)
        if enforce_budget:
            self._raise_if_over_budget(path.parent, result)

    def _add_artifact(self, result: dict[str, Any], name: str, path: Path) -> None:
        relative = self._relative(path)
        if not any(item["path"] == relative for item in result["artifacts"]):
            result["artifacts"].append({"name": name, "path": relative, "size_bytes": path.stat().st_size})

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.data_dir.resolve()).as_posix()

    def _frame_timestamps(self, stderr: str, frame_count: int, duration: float) -> tuple[list[float], str]:
        parsed = [float(value) for value in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", stderr)]
        exact = sorted(min(max(value, 0.0), duration) for value in parsed if 0.0 <= value <= duration)
        if len(exact) >= frame_count:
            return exact[:frame_count], "exact"
        # The fixed sampling filter has a nominal cadence, but missing showinfo
        # output cannot be represented as a source PTS.  Keep the estimate
        # bounded and visibly label it rather than claiming exact timing.
        if frame_count == 1:
            return [0.0], "estimated"
        return [min(duration, duration * index / frame_count) for index in range(frame_count)], "estimated"

    @staticmethod
    def _nearest_frame_index(frames: list[dict[str, Any]], timestamp: float) -> int:
        return min(frames, key=lambda frame: abs(float(frame["timestamp_seconds"]) - timestamp))["index"]

    def _raise_if_over_budget(self, output: Path, result: dict[str, Any]) -> None:
        if self._enforce_output_budget(output, result):
            raise PreprocessError("derived_size_exceeded", f"Derived artifacts exceeded the {self.max_derived_bytes}-byte limit and media outputs were removed.", result)

    def _enforce_output_budget(self, output: Path, result: dict[str, Any]) -> bool:
        if self._derived_size(output) <= self.max_derived_bytes:
            return False
        self._cleanup_excess_media(output, result)
        return True

    @staticmethod
    def _derived_size(output: Path) -> int:
        return sum(path.stat().st_size for path in output.rglob("*") if path.is_file())

    def _cleanup_excess_media(self, output: Path, result: dict[str, Any]) -> None:
        """Remove disposable derived media, never the source object or trace logs."""
        for directory_name in ("frames", "shots"):
            directory = output / directory_name
            if directory.is_dir():
                shutil.rmtree(directory)
        for path in output.rglob("*"):
            if path.is_file() and path.suffix.lower() in DERIVED_MEDIA_SUFFIXES:
                path.unlink()
        result["artifacts"] = [item for item in result["artifacts"] if (self.data_dir / item["path"]).is_file()]


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_rate(value: Any) -> float | None:
    if not value or value == "0/0":
        return None
    try:
        numerator, denominator = str(value).split("/", 1)
        return float(numerator) / float(denominator)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
