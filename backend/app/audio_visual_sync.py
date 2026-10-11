"""Bounded, local audio--visual synchronisation baselines.

The Haar/motion adapter is deliberately modest: OpenCV's shipped Haar cascade
locates one face, then this module compares lower-face motion with short-time
audio energy.  It is a deterministic synchrony *screen*, not a lip-reading
model and not a deepfake classifier.  It therefore declines to score material
without enough speech, a single stable face, or usable video frames.
"""

from __future__ import annotations

import json
import math
import multiprocessing
import os
import sys
import time
import wave
from pathlib import Path
from typing import Any, Callable, Protocol

from .security import safe_error_trace


SyncResult = dict[str, Any]
RISK_THRESHOLD = 0.65
SAMPLE_RATE_HZ = 6.0
MIN_SPEECH_SECONDS = 1.5
MIN_SAMPLES = 12
DEFAULT_OPENCV_DIR = Path("models/cache/opencv-haar/python")
SYNC_TIMEOUT_SECONDS = 10.0


def _sync_child(connection: Any, adapter: Any, job: dict[str, Any], preprocess: dict[str, Any] | None, vad: dict[str, Any]) -> None:
    try:
        connection.send(("ok", adapter._analyse(job, preprocess, vad, lambda: False)))
    except BaseException as error:
        connection.send(("error", safe_error_trace(error)))
    finally:
        connection.close()


class AudioVisualSyncAdapter(Protocol):
    def analyse(self, job: dict[str, Any], preprocess: dict[str, Any] | None, vad: dict[str, Any], cancelled: Callable[[], bool]) -> SyncResult: ...

    def describe(self) -> dict[str, Any]: ...


class HaarMotionAudioVisualSyncAdapter:
    """A local single-face motion/audio correlation baseline.

    The dependency is installed outside the project virtual environment by
    default so approved third-party binaries remain on D:.  Deployments can
    provide ``MINGJIAN_OPENCV_DIR`` instead.
    """

    name = "OpenCV Haar lower-face-motion / audio-energy synchrony baseline"
    version = "haar-motion-avsync-v1"
    license = "Apache-2.0 (OpenCV); package metadata also lists MIT wrapper and LGPL-2.1 FFmpeg"

    def __init__(self, data_dir: Path, *, opencv_dir: Path | None = None) -> None:
        self.data_dir = data_dir.resolve()
        self.opencv_dir = opencv_dir or Path(os.environ.get("MINGJIAN_OPENCV_DIR", str(DEFAULT_OPENCV_DIR)))

    def describe(self) -> dict[str, Any]:
        cv2, reason = self._load_cv2()
        cascade = None if cv2 is None else Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        return {
            "name": self.name,
            "version": self.version,
            "license": self.license,
            "mock": False,
            "method": "single-face Haar detection + lower-face motion / short-time audio-energy correlation",
            "dependency_available": cv2 is not None and bool(cascade and cascade.is_file()),
            "opencv_dir": str(self.opencv_dir),
            "limitations": [] if cv2 is not None and cascade and cascade.is_file() else [reason or "OpenCV Haar cascade is unavailable."],
        }

    def analyse(self, job: dict[str, Any], preprocess: dict[str, Any] | None, vad: dict[str, Any], cancelled: Callable[[], bool]) -> SyncResult:
        features = (preprocess or {}).get("input_features", {})
        speech = self._speech_segments(vad)
        speech_seconds = sum(end - start for start, end in speech)
        if cancelled() or not features.get("has_audio") or not features.get("has_video") or vad.get("status") not in {"available", "degraded"} or speech_seconds < MIN_SPEECH_SECONDS:
            return self._analyse(job, preprocess, vad, cancelled)
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(target=_sync_child, args=(child, self, job, preprocess, vad), daemon=True)
        started = time.monotonic()
        try:
            process.start()
            child.close()
            while time.monotonic() - started < SYNC_TIMEOUT_SECONDS:
                if cancelled():
                    process.terminate()
                    process.join(timeout=1.0)
                    return self._result("cancelled", ["任务在音画同步分析期间被取消。"], preprocess, speech_seconds=speech_seconds)
                if parent.poll(0.05):
                    state, value = parent.recv()
                    process.join(timeout=1.0)
                    if state == "ok" and isinstance(value, dict):
                        return value
                    return self._result("unavailable", ["音画同步分析子进程未完成。"], preprocess, speech_seconds=speech_seconds, error_trace=str(value)[:240])
            process.terminate()
            process.join(timeout=1.0)
            if process.is_alive():
                process.kill()
                process.join(timeout=1.0)
            return self._result("unavailable", ["音画同步分析超过 10 秒总预算，子进程已终止。"], preprocess, speech_seconds=speech_seconds, error_trace="TimeoutError: sync module exceeded 10 seconds")
        except BaseException as error:
            return self._result("unavailable", ["音画同步分析运行边界不可用。"], preprocess, speech_seconds=speech_seconds, error_trace=safe_error_trace(error))
        finally:
            parent.close()
            if process.is_alive():
                process.terminate()
                process.join(timeout=1.0)

    def _analyse(self, job: dict[str, Any], preprocess: dict[str, Any] | None, vad: dict[str, Any], cancelled: Callable[[], bool]) -> SyncResult:
        features = (preprocess or {}).get("input_features", {})
        if cancelled():
            return self._result("cancelled", ["任务在同步基线执行前被取消。"], preprocess)
        if not features.get("has_audio"):
            return self._result("not_applicable", ["输入没有音频流；音画同步不适用，不代表低风险。"], preprocess)
        if not features.get("has_video"):
            return self._result("not_applicable", ["输入没有视频流；音画同步不适用，不代表低风险。"], preprocess)
        speech = self._speech_segments(vad)
        speech_seconds = sum(end - start for start, end in speech)
        if vad.get("status") not in {"available", "degraded"} or speech_seconds < MIN_SPEECH_SECONDS:
            return self._result("unavailable", [f"可用 VAD 语音总长不足 {MIN_SPEECH_SECONDS:.1f} 秒，未输出同步风险；这不是低风险证据。"], preprocess, speech_seconds=speech_seconds)
        if float(features.get("width") or 0) < 320 or float(features.get("height") or 0) < 180:
            return self._result("unavailable", ["视频分辨率低于 320x180；无法可靠定位嘴部候选区域，未输出同步风险。"], preprocess, speech_seconds=speech_seconds)

        cv2, reason = self._load_cv2()
        if cv2 is None:
            return self._result("unavailable", [reason or "OpenCV 依赖不可用；未输出同步风险。"], preprocess, speech_seconds=speech_seconds)
        cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        if not cascade_path.is_file():
            return self._result("unavailable", ["OpenCV Haar 人脸级联文件不可用；未输出同步风险。"], preprocess, speech_seconds=speech_seconds)
        media = self._artifact_path(preprocess, "normalized_media")
        audio = self._artifact_path(preprocess, "normalized_audio")
        if media is None or audio is None:
            return self._result("unavailable", ["缺少标准化视频或音频工件；未输出同步风险。"], preprocess, speech_seconds=speech_seconds)

        samples, quality, extraction_reason = self._extract_motion_samples(cv2, cascade_path, media, speech, cancelled)
        if extraction_reason:
            return self._result("cancelled" if extraction_reason == "cancelled" else "unavailable", [self._reason_text(extraction_reason)], preprocess, speech_seconds=speech_seconds, extra_quality=quality)
        audio_energy, audio_reason = self._audio_energy(audio, [item[0] for item in samples])
        if audio_reason:
            return self._result("unavailable", [audio_reason], preprocess, speech_seconds=speech_seconds, extra_quality=quality)
        result = self._score_samples(samples, audio_energy, speech, preprocess, speech_seconds, quality)
        return result

    def _load_cv2(self) -> tuple[Any | None, str | None]:
        if self.opencv_dir.is_dir() and str(self.opencv_dir) not in sys.path:
            sys.path.append(str(self.opencv_dir))
        try:
            import cv2  # type: ignore[import-not-found]
        except (ImportError, OSError) as error:
            return None, f"OpenCV Haar 运行依赖不可用（{str(error)[:160]}）；未输出同步风险。"
        return cv2, None

    def _extract_motion_samples(self, cv2: Any, cascade_path: Path, media: Path, speech: list[tuple[float, float]], cancelled: Callable[[], bool]) -> tuple[list[tuple[float, float]], dict[str, Any], str | None]:
        capture = cv2.VideoCapture(str(media))
        if not capture.isOpened():
            return [], {"status": "unavailable"}, "标准化视频无法由 OpenCV 打开；未输出同步风险。"
        detector = cv2.CascadeClassifier(str(cascade_path))
        if detector.empty():
            capture.release()
            return [], {"status": "unavailable"}, "OpenCV Haar 级联无法加载；未输出同步风险。"
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
        frame_count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        duration = frame_count / fps if fps > 0 else 0.0
        times = self._sample_times(speech, duration)
        previous: Any = None
        samples: list[tuple[float, float]] = []
        face_frames = 0
        multi_face_frames = 0
        try:
            for timestamp in times:
                if cancelled():
                    return [], {"status": "cancelled"}, "cancelled"
                capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
                ok, frame = capture.read()
                if not ok or frame is None:
                    continue
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                faces = detector.detectMultiScale(gray, scaleFactor=1.12, minNeighbors=5, minSize=(48, 48))
                if len(faces) != 1:
                    multi_face_frames += int(len(faces) > 1)
                    previous = None
                    continue
                x, y, width, height = (int(value) for value in faces[0])
                lower_start, lower_end = y + int(height * 0.55), y + int(height * 0.92)
                crop = gray[lower_start:lower_end, x:x + width]
                if crop.size == 0:
                    previous = None
                    continue
                normalized = cv2.resize(crop, (48, 24))
                face_frames += 1
                if previous is not None:
                    motion = float(cv2.absdiff(normalized, previous).mean()) / 255.0
                    samples.append((timestamp, motion))
                previous = normalized
        finally:
            capture.release()
        quality = {
            "status": "available",
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "candidate_frame_count": len(times),
            "single_face_frame_count": face_frames,
            "motion_sample_count": len(samples),
            "multi_face_frame_count": multi_face_frames,
            "face_coverage": round(face_frames / max(1, len(times)), 6),
        }
        if multi_face_frames:
            return samples, quality, "检测到多脸候选帧；基础基线没有活跃说话人识别，未输出同步风险。"
        if face_frames < MIN_SAMPLES or len(samples) < MIN_SAMPLES:
            return samples, quality, "单脸/嘴部候选帧不足，未输出同步风险；无人脸或不足不代表低风险。"
        if face_frames / max(1, len(times)) < 0.70:
            return samples, quality, "单脸候选覆盖率不足 70%，未输出同步风险。"
        return samples, quality, None

    @staticmethod
    def _sample_times(speech: list[tuple[float, float]], duration: float) -> list[float]:
        times: list[float] = []
        step = 1.0 / SAMPLE_RATE_HZ
        for start, end in speech:
            cursor = max(0.0, start)
            while cursor <= min(duration, end):
                times.append(round(cursor, 6))
                cursor += step
        return times

    @staticmethod
    def _audio_energy(audio: Path, timestamps: list[float]) -> tuple[list[float], str | None]:
        try:
            with wave.open(str(audio), "rb") as reader:
                if reader.getnchannels() != 1 or reader.getsampwidth() != 2 or reader.getframerate() <= 0:
                    return [], "标准化音频不符合 16-bit 单声道 WAV 契约；未输出同步风险。"
                rate = reader.getframerate()
                raw = reader.readframes(reader.getnframes())
        except (OSError, wave.Error) as error:
            return [], f"标准化音频无法读取（{str(error)[:160]}）；未输出同步风险。"
        import array
        values = array.array("h")
        values.frombytes(raw)
        half_window = max(1, int(rate / SAMPLE_RATE_HZ / 2))
        energies: list[float] = []
        for timestamp in timestamps:
            centre = int(timestamp * rate)
            chunk = values[max(0, centre - half_window):min(len(values), centre + half_window)]
            if not chunk:
                energies.append(0.0)
                continue
            energies.append(math.sqrt(sum(sample * sample for sample in chunk) / len(chunk)) / 32768.0)
        return energies, None

    def _score_samples(self, samples: list[tuple[float, float]], audio_energy: list[float], speech: list[tuple[float, float]], preprocess: dict[str, Any] | None, speech_seconds: float, quality: dict[str, Any]) -> SyncResult:
        times = [item[0] for item in samples]
        motion = [item[1] for item in samples]
        best_offset, best_correlation, overlap = self._best_offset(times, motion, audio_energy)
        if best_correlation is None or overlap < MIN_SAMPLES:
            return self._result("unavailable", ["音频与视觉有效样本重叠不足，未输出同步风险。"], preprocess, speech_seconds=speech_seconds, extra_quality=quality)
        risk = round(self._risk_from_correlation(best_correlation), 6)
        intervals = self._intervals(times, motion, audio_energy, speech, best_offset)
        quality.update({"status": "available", "audio_visual_overlap_samples": overlap, "best_offset_seconds": best_offset, "best_correlation": round(best_correlation, 6)})
        return {
            "schema_version": "audio-visual-sync-result-v1",
            "status": "completed",
            "mock": False,
            "dependency_available": True,
            "risk_score": risk,
            "risk_threshold": RISK_THRESHOLD,
            "score_semantics": "未校准的低相关性启发式同步异常信号；不是深伪概率、真实性概率或同步事实断言。",
            "anomalous_intervals": intervals,
            "input_quality": {"status": "available", "speech_seconds": round(speech_seconds, 6), "preprocess_quality_gates": (preprocess or {}).get("quality_gates", []), **quality},
            "model": self.describe(),
            "configuration": {"sample_rate_hz": SAMPLE_RATE_HZ, "offset_search_seconds": [-0.5, 0.5], "offset_step_seconds": round(1.0 / SAMPLE_RATE_HZ, 6), "min_speech_seconds": MIN_SPEECH_SECONDS, "min_samples": MIN_SAMPLES},
            "evidence": {"best_offset_seconds": best_offset, "best_correlation": round(best_correlation, 6), "suspicious_intervals": intervals},
            "limitations": ["仅支持单个稳定人脸；没有活跃说话人识别、唇部关键点、ASR 或说话人分离。", "背景音乐、镜头运动、遮挡、静音、剪辑和低清画面可使结果不可用或退化。", "该启发式分数未经项目数据校准，不作为真实性概率或单独裁决依据。"],
        }

    @staticmethod
    def _best_offset(times: list[float], motion: list[float], audio: list[float]) -> tuple[float, float | None, int]:
        step = 1.0 / SAMPLE_RATE_HZ
        candidates = [round(index * step, 6) for index in range(-3, 4)]
        best: tuple[float, float, int] | None = None
        for offset in candidates:
            left: list[float] = []
            right: list[float] = []
            for index, timestamp in enumerate(times):
                target = timestamp + offset
                nearest = min(range(len(times)), key=lambda item: abs(times[item] - target))
                if abs(times[nearest] - target) <= step * 0.55:
                    left.append(motion[index])
                    right.append(audio[nearest])
            correlation = HaarMotionAudioVisualSyncAdapter._pearson(left, right)
            if correlation is None:
                continue
            candidate = (offset, correlation, len(left))
            if best is None or correlation > best[1] or (correlation == best[1] and abs(offset) < abs(best[0])):
                best = candidate
        return best if best is not None else (0.0, None, 0)

    def _intervals(self, times: list[float], motion: list[float], audio: list[float], speech: list[tuple[float, float]], offset: float) -> list[dict[str, Any]]:
        intervals: list[dict[str, Any]] = []
        for start, end in speech:
            indexes = [index for index, value in enumerate(times) if start <= value <= end]
            if len(indexes) < MIN_SAMPLES:
                continue
            local_motion = [motion[index] for index in indexes]
            shifted_audio: list[float] = []
            for index in indexes:
                target = times[index] + offset
                nearest = min(range(len(times)), key=lambda item: abs(times[item] - target))
                shifted_audio.append(audio[nearest])
            correlation = self._pearson(local_motion, shifted_audio)
            if correlation is None:
                continue
            risk = self._risk_from_correlation(correlation)
            if risk >= RISK_THRESHOLD:
                intervals.append({"start_seconds": round(start, 6), "end_seconds": round(end, 6), "risk_score": round(risk, 6), "correlation": round(correlation, 6), "best_offset_seconds": offset, "reason": "该 VAD 语音片段的下半脸运动与音频能量相关性低于配置阈值；仅供复核。"})
        return intervals

    @staticmethod
    def _pearson(left: list[float], right: list[float]) -> float | None:
        if len(left) < MIN_SAMPLES or len(left) != len(right):
            return None
        left_mean, right_mean = sum(left) / len(left), sum(right) / len(right)
        numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
        left_scale = math.sqrt(sum((value - left_mean) ** 2 for value in left))
        right_scale = math.sqrt(sum((value - right_mean) ** 2 for value in right))
        if left_scale <= 1e-12 or right_scale <= 1e-12:
            return None
        return max(-1.0, min(1.0, numerator / (left_scale * right_scale)))

    @staticmethod
    def _risk_from_correlation(correlation: float) -> float:
        return max(0.0, min(1.0, (0.50 - correlation) / 0.75))

    def _artifact_path(self, preprocess: dict[str, Any] | None, name: str) -> Path | None:
        artifact = next((item for item in (preprocess or {}).get("artifacts", []) if item.get("name") == name), None)
        if not artifact or not isinstance(artifact.get("path"), str):
            return None
        try:
            path = (self.data_dir / artifact["path"]).resolve()
            path.relative_to(self.data_dir)
            return path if path.is_file() else None
        except ValueError:
            return None

    @staticmethod
    def _speech_segments(vad: dict[str, Any]) -> list[tuple[float, float]]:
        segments: list[tuple[float, float]] = []
        for item in vad.get("segments", []) if isinstance(vad, dict) else []:
            try:
                start, end = float(item["start_seconds"]), float(item["end_seconds"])
            except (KeyError, TypeError, ValueError):
                continue
            if end > start:
                segments.append((start, end))
        return segments

    def _result(self, status: str, limitations: list[str], preprocess: dict[str, Any] | None, *, speech_seconds: float | None = None, extra_quality: dict[str, Any] | None = None, error_trace: str | None = None) -> SyncResult:
        quality: dict[str, Any] = {"status": status, "preprocess_quality_gates": (preprocess or {}).get("quality_gates", [])}
        if speech_seconds is not None:
            quality["speech_seconds"] = round(speech_seconds, 6)
        if extra_quality:
            quality.update(extra_quality)
        model = self.describe()
        result = {"schema_version": "audio-visual-sync-result-v1", "status": status, "mock": False, "dependency_available": bool(model.get("dependency_available")), "risk_score": None, "risk_threshold": RISK_THRESHOLD, "anomalous_intervals": [], "input_quality": quality, "model": model, "configuration": {"sample_rate_hz": SAMPLE_RATE_HZ, "min_speech_seconds": MIN_SPEECH_SECONDS, "min_samples": MIN_SAMPLES, "timeout_seconds": SYNC_TIMEOUT_SECONDS}, "evidence": {"best_offset_seconds": None, "best_correlation": None, "suspicious_intervals": []}, "limitations": limitations}
        if error_trace:
            result["error_trace"] = error_trace
        return result

    @staticmethod
    def _reason_text(reason: str) -> str:
        return reason if reason.endswith("。") else f"{reason}。"


class MockAudioVisualSyncAdapter:
    """Compatibility-only mock.  Production defaults to the Haar/motion adapter."""

    def describe(self) -> dict[str, Any]:
        return {"name": "AudioVisualSync mock", "version": "mock-v1", "mock": True, "status": "interface_only"}

    def analyse(self, job: dict[str, Any], preprocess: dict[str, Any] | None, vad: dict[str, Any], cancelled: Callable[[], bool]) -> SyncResult:
        features = (preprocess or {}).get("input_features", {})
        if cancelled():
            return self._result("cancelled", "任务在音画同步适配器执行前被取消。", preprocess)
        if not features.get("has_audio"):
            return self._result("not_applicable", "输入没有音频流；音画同步不适用，不代表低风险。", preprocess)
        if not features.get("has_video"):
            return self._result("not_applicable", "输入没有视频流；音画同步不适用，不代表低风险。", preprocess)
        if vad.get("status") not in {"available", "degraded"} or not vad.get("segments"):
            return self._result("unavailable", "没有足够的 VAD 语音区间，不能进行音画同步判断；这不是低风险证据。", preprocess)
        return self._result("unavailable", "此注入 Mock 不包含同步检测能力，只用于接口测试。", preprocess)

    def _result(self, status: str, limitation: str, preprocess: dict[str, Any] | None) -> SyncResult:
        gates = (preprocess or {}).get("quality_gates", [])
        return {"schema_version": "audio-visual-sync-result-v1", "status": status, "mock": True, "dependency_available": False, "risk_score": None, "risk_threshold": None, "anomalous_intervals": [], "input_quality": {"status": status, "preprocess_quality_gates": gates}, "model": self.describe(), "configuration": {}, "evidence": {"best_offset_seconds": None, "best_correlation": None, "suspicious_intervals": []}, "limitations": [limitation, "未完成的同步能力不能作为音画一致或不一致的断言。"]}
