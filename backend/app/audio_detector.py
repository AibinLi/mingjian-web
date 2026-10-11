"""Replaceable local audio anti-spoofing detector.

The adapter deliberately treats a missing checkpoint, runtime, label mapping, or
usable speech as unavailable evidence.  It never turns any of those conditions
into a low-risk score.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable, Protocol


MODEL_NAME = "AST ASVspoof2019 Synthetic Voice Detection"
MODEL_VERSION = "local frozen artifacts: model.safetensors + config.json + preprocessor_config.json SHA-256"
MODEL_LICENSE = "MIT"
MODEL_SOURCE = "https://huggingface.co/MattyB95/AST-ASVspoof2019-Synthetic-Voice-Detection"
DEFAULT_MODEL_DIR = Path("models/cache/ast-asvspoof2019")
RISK_THRESHOLD = 0.5
MAX_SEGMENT_SECONDS = 10.0
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_FAKE_LABEL = "Spoof"  # verified from this pinned local model's config.json
FROZEN_ARTIFACTS = {
    "model.safetensors": {"size_bytes": 344790040, "sha256": "df8c7cd04c6b81a4f02756cbef777662d0092cf33ab23b14eea0df74c7978db6"},
    "config.json": {"size_bytes": 791, "sha256": "703a05b1c488954226ccd5fc107517de732eb1444c2700a64270786998725933"},
    "preprocessor_config.json": {"size_bytes": 297, "sha256": "8d04ba5a9c6fca5d39d0de2b1fd05ecf79deb589fbba279728bbebac39934231"},
}

AudioResult = dict[str, Any]


class AudioDetector(Protocol):
    def describe(self) -> dict[str, Any]: ...

    def detect(self, job: dict[str, Any], preprocess: dict[str, Any] | None, cancelled: Callable[[], bool]) -> AudioResult: ...


class AstAudioDetector:
    """Hugging Face AST adapter using only an explicitly provisioned local model.

    The service does not use ``from_pretrained`` with a remote identifier: all
    inputs must already exist beneath the administrator-selected D-drive cache.
    """

    def __init__(self, data_dir: Path, *, model_dir: Path | None = None, timeout_seconds: float | None = None) -> None:
        self.data_dir = data_dir.resolve()
        configured = os.environ.get("MINGJIAN_AUDIO_MODEL_DIR")
        self.model_dir = (Path(configured) if configured else model_dir or DEFAULT_MODEL_DIR).resolve()
        self.timeout_seconds = max(1.0, min(float(timeout_seconds or os.environ.get("MINGJIAN_AUDIO_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)), 120.0))
        self._runtime: tuple[Any, Any] | None = None
        self._runtime_lock = threading.Lock()

    def describe(self) -> dict[str, Any]:
        artifacts = {name: _frozen_artifact_record(self.model_dir / name, expected) for name, expected in FROZEN_ARTIFACTS.items()}
        integrity_status = "available" if all(item["valid"] for item in artifacts.values()) else "unavailable"
        weights = artifacts["model.safetensors"]
        description: dict[str, Any] = {
            "name": MODEL_NAME,
            "version": MODEL_VERSION,
            "license": MODEL_LICENSE,
            "source": MODEL_SOURCE,
            "model_dir": str(self.model_dir),
            "weight_path": str(self.model_dir / "model.safetensors"),
            "weight_available": integrity_status == "available",
            "remote_downloads_disabled": True,
            "frozen_artifacts": artifacts,
            "frozen_integrity_status": integrity_status,
        }
        if weights["exists"]:
            description.update({"weight_size_bytes": weights["actual_size_bytes"], "weight_sha256": weights["actual_sha256"]})
        return description

    def detect(self, job: dict[str, Any], preprocess: dict[str, Any] | None, cancelled: Callable[[], bool]) -> AudioResult:
        started = time.monotonic()
        model = self.describe()
        configuration = {"risk_threshold": RISK_THRESHOLD, "timeout_seconds": self.timeout_seconds, "max_segment_seconds": MAX_SEGMENT_SECONDS}
        audio_path, segments, problem, quality = self._input(preprocess)
        if cancelled():
            return self._empty("cancelled", model, configuration, quality, started, "任务在音频推理前被取消。")
        if problem == "not_applicable":
            return self._empty("not_applicable", model, configuration, quality, started, "输入没有音频流；音频伪造分析不适用，不代表低风险。")
        if problem:
            return self._empty("unavailable", model, configuration, quality, started, problem)
        if not model["weight_available"]:
            invalid = [name for name, item in model["frozen_artifacts"].items() if not item["valid"]]
            return self._empty("unavailable", model, configuration, quality, started, f"本地冻结 AST 工件校验失败：{'; '.join(invalid)}；模型未加载，未将其映射为低风险。")
        fake_label = os.environ.get("MINGJIAN_AUDIO_FAKE_LABEL", DEFAULT_FAKE_LABEL)
        try:
            scores = self._infer(audio_path, segments, fake_label, cancelled, started)
        except TimeoutError:
            return self._empty("timed_out", model, configuration, quality, started, "音频推理超过配置时限；没有保留不完整风险结论。")
        except RuntimeError as error:
            return self._empty("unavailable", model, configuration, quality, started, f"音频模型不可用：{_safe_error(error)}")
        if scores is None:
            return self._empty("cancelled", model, configuration, quality, started, "任务在音频推理期间被取消。")
        if not scores:
            return self._empty("unavailable", model, configuration, quality, started, "没有可用的 VAD 语音片段可供音频模型分析；这不是低风险证据。")
        highest = max(scores, key=lambda item: float(item["risk_score"]))
        suspicious = _segments(scores)
        return {
            "schema_version": "audio-detector-result-v1", "status": "completed", "mock": False,
            "model": model, "configuration": configuration, "input_quality": quality,
            "segment_risks": scores,
            "whole_audio_risk": {"risk_score": highest["risk_score"], "aggregation": "max_vad_segment_risk", "score_semantics": "AST 伪造类 softmax 分数；未经本项目数据校准，不是真实性概率。"},
            "evidence": {"highest_risk_segment": highest, "suspicious_segments": suspicious},
            "limitations": ["仅分析本地 VAD 划分的语音片段，不分析静音或非语音片段。", "分数未经本项目数据校准，不能证明媒体真实或虚假。", "模型卡自报评测不作为本项目性能结论。"],
            "elapsed_seconds": round(time.monotonic() - started, 6),
        }

    def _input(self, preprocess: dict[str, Any] | None) -> tuple[Path | None, list[dict[str, Any]], str | None, dict[str, Any]]:
        features = (preprocess or {}).get("input_features", {})
        if not features.get("has_audio"):
            return None, [], "not_applicable", {"status": "not_applicable", "reasons": ["没有可用音频流。"], "preprocess_quality_gates": (preprocess or {}).get("quality_gates", [])}
        if not preprocess or preprocess.get("status") != "completed":
            return None, [], "预处理未完成，音频模型没有可用输入。", {"status": "unavailable", "reasons": ["预处理未完成。"], "preprocess_quality_gates": (preprocess or {}).get("quality_gates", [])}
        artifact = next((item for item in preprocess.get("artifacts", []) if item.get("name") == "normalized_audio"), None)
        # For an already-normalized WAV the preprocessor deduplicates the
        # identical normalized_media/normalized_audio path.  The media artifact
        # is still the approved 16 kHz mono WAV input, so retain that semantic
        # fallback instead of declaring a false missing-audio condition.
        if artifact is None and not features.get("has_video"):
            artifact = next((item for item in preprocess.get("artifacts", []) if item.get("name") == "normalized_media"), None)
        vad_artifact = next((item for item in preprocess.get("artifacts", []) if item.get("name") == "vad"), None)
        if not artifact or not isinstance(artifact.get("path"), str) or not vad_artifact or not isinstance(vad_artifact.get("path"), str):
            return None, [], "预处理未登记标准化音频或 VAD 工件。", {"status": "unavailable", "reasons": ["缺少音频或 VAD 工件。"], "preprocess_quality_gates": preprocess.get("quality_gates", [])}
        try:
            audio_path = _safe_path(self.data_dir, artifact["path"])
            vad = json.loads(_safe_path(self.data_dir, vad_artifact["path"]).read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None, [], "标准化音频或 VAD 工件不可读取。", {"status": "unavailable", "reasons": ["音频或 VAD 工件不可读取。"], "preprocess_quality_gates": preprocess.get("quality_gates", [])}
        segments = [item for item in vad.get("segments", []) if isinstance(item, dict) and item.get("kind") == "speech"] if isinstance(vad, dict) else []
        status = "available" if segments else "degraded"
        reasons = list(vad.get("quality", {}).get("reasons", [])) if isinstance(vad, dict) and isinstance(vad.get("quality"), dict) else []
        if not segments:
            reasons.append("VAD 没有识别到可分析语音片段。")
        return audio_path, segments, None, {"status": status, "vad_status": vad.get("status") if isinstance(vad, dict) else "unavailable", "speech_segments_available": len(segments), "reasons": reasons, "preprocess_quality_gates": preprocess.get("quality_gates", [])}

    def _infer(self, audio_path: Path | None, segments: list[dict[str, Any]], fake_label: str, cancelled: Callable[[], bool], started: float) -> list[dict[str, Any]] | None:
        if audio_path is None:
            raise RuntimeError("标准化音频路径缺失。")
        try:
            import torch
            from transformers import AutoFeatureExtractor, AutoModelForAudioClassification
        except ImportError as error:
            raise RuntimeError("缺少 transformers 运行时；模型不进行远程下载。") from error
        try:
            processor, model = self._load_runtime(AutoFeatureExtractor, AutoModelForAudioClassification)
        except Exception as error:
            raise RuntimeError("本地 AST 模型文件不完整或不可加载。") from error
        id2label = {str(value).strip().lower(): int(key) for key, value in getattr(model.config, "id2label", {}).items()}
        label_key = fake_label.strip().lower()
        if label_key not in id2label:
            raise RuntimeError("MINGJIAN_AUDIO_FAKE_LABEL 不匹配本地模型 id2label。")
        samples, rate = _read_wav(audio_path)
        fake_index = id2label[label_key]
        output: list[dict[str, Any]] = []
        with torch.inference_mode():
            for index, segment in enumerate(segments):
                if cancelled():
                    return None
                _deadline(started, self.timeout_seconds)
                start = max(0, int(float(segment.get("start_seconds", 0.0)) * rate))
                end = min(len(samples), int(float(segment.get("end_seconds", 0.0)) * rate))
                clip = samples[start:min(end, start + int(MAX_SEGMENT_SECONDS * rate))]
                if not clip:
                    continue
                values = processor(clip, sampling_rate=rate, return_tensors="pt")
                logits = model(**values).logits
                score = float(torch.softmax(logits, dim=-1)[0, fake_index].item())
                output.append({"segment_index": index, "start_seconds": round(start / rate, 6), "end_seconds": round(end / rate, 6), "risk_score": round(score, 6), "risk_threshold": RISK_THRESHOLD, "score_semantics": "AST 配置的伪造类 softmax 分数（未经校准）。"})
                _deadline(started, self.timeout_seconds)
        return output

    def _load_runtime(self, AutoFeatureExtractor: Any, AutoModelForAudioClassification: Any) -> tuple[Any, Any]:
        """Load the validated local processor/model once per service adapter."""
        with self._runtime_lock:
            if self._runtime is None:
                processor = AutoFeatureExtractor.from_pretrained(self.model_dir, local_files_only=True)
                model = AutoModelForAudioClassification.from_pretrained(self.model_dir, local_files_only=True).eval().to("cpu")
                self._runtime = (processor, model)
            return self._runtime

    @staticmethod
    def _empty(status: str, model: dict[str, Any], configuration: dict[str, Any], quality: dict[str, Any], started: float, limitation: str) -> AudioResult:
        return {"schema_version": "audio-detector-result-v1", "status": status, "mock": False, "model": model, "configuration": configuration, "input_quality": quality, "segment_risks": [], "whole_audio_risk": None, "evidence": {"highest_risk_segment": None, "suspicious_segments": []}, "limitations": [limitation, "不可用、取消、超时或不适用不表示低风险。"], "elapsed_seconds": round(time.monotonic() - started, 6)}


def _read_wav(path: Path) -> tuple[list[float], int]:
    try:
        with wave.open(str(path), "rb") as reader:
            if reader.getnchannels() != 1 or reader.getsampwidth() != 2:
                raise RuntimeError("标准化音频不是 16-bit 单声道 WAV。")
            rate = reader.getframerate()
            raw = reader.readframes(reader.getnframes())
    except (OSError, wave.Error) as error:
        raise RuntimeError("无法读取标准化 WAV。") from error
    import array
    values = array.array("h")
    values.frombytes(raw)
    if values.itemsize != 2:
        raise RuntimeError("WAV 样本宽度无效。")
    return [item / 32768.0 for item in values], rate


def _segments(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = [item for item in values if float(item["risk_score"]) >= RISK_THRESHOLD]
    if not selected:
        return []
    groups: list[list[dict[str, Any]]] = [[selected[0]]]
    for item in selected[1:]:
        if float(item["start_seconds"]) - float(groups[-1][-1]["end_seconds"]) <= 0.25:
            groups[-1].append(item)
        else:
            groups.append([item])
    return [{"start_seconds": group[0]["start_seconds"], "end_seconds": group[-1]["end_seconds"], "peak_risk_score": max(float(item["risk_score"]) for item in group), "segment_indices": [item["segment_index"] for item in group], "selection_rule": f"AST fake-class score >= {RISK_THRESHOLD}"} for group in groups]


def _safe_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("工件路径位于数据目录之外。") from error
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _frozen_artifact_record(path: Path, expected: dict[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": str(path),
        "expected_size_bytes": expected["size_bytes"],
        "expected_sha256": expected["sha256"],
        "exists": path.is_file(),
        "actual_size_bytes": None,
        "actual_sha256": None,
        "valid": False,
    }
    if not record["exists"]:
        return record
    record["actual_size_bytes"] = path.stat().st_size
    record["actual_sha256"] = _sha256(path)
    record["valid"] = record["actual_size_bytes"] == record["expected_size_bytes"] and record["actual_sha256"] == record["expected_sha256"]
    return record


def _deadline(started: float, timeout_seconds: float) -> None:
    if time.monotonic() - started > timeout_seconds:
        raise TimeoutError()


def _safe_error(error: Exception) -> str:
    return str(error).replace(str(Path.home()), "~")[:240]
