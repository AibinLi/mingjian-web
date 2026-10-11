"""Pinned, offline Whisper ASR adapter for timestamped claim extraction."""

from __future__ import annotations

import hashlib
import json
import math
import os
import wave
from pathlib import Path
from typing import Any

MODEL_SHA256 = "07cadb9f25677c8d50df603e66a98fbd842cce45047139baeb16e6219a1e807b"
MODEL_REVISION = "e37978b90ca9030d5170a5c07aadb050351a65bb"
DEFAULT_MODEL_DIR = Path("models/cache/whisper-base")
MANIFEST_NAME = "mingjian-artifacts.json"


def _sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


class WhisperAsrAdapter:
    """Loads only a provisioned local snapshot; it never contacts Hugging Face."""

    def __init__(self, model_dir: Path | None = None) -> None:
        self.model_dir = model_dir or Path(os.environ.get("MINGJIAN_ASR_MODEL_DIR", str(DEFAULT_MODEL_DIR)))
        self._descriptor: dict[str, Any] | None = None

    def describe(self) -> dict[str, Any]:
        if self._descriptor is not None:
            return self._descriptor
        descriptor: dict[str, Any] = {"name": "OpenAI Whisper base", "revision": MODEL_REVISION, "license": "Apache-2.0", "model_dir": str(self.model_dir), "weight_available": False, "frozen_integrity_status": "unavailable", "remote_downloads_disabled": True, "frozen_artifacts": {}}
        manifest_path = self.model_dir / MANIFEST_NAME
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("schema_version") != "mingjian-whisper-artifacts-v1" or manifest.get("model_id") != "openai/whisper-base" or manifest.get("revision") != MODEL_REVISION:
                raise ValueError("Whisper manifest identity or revision is invalid.")
            records = manifest.get("files")
            if not isinstance(records, list) or not records:
                raise ValueError("Whisper manifest has no frozen files.")
            all_valid = True
            for record in records:
                if not isinstance(record, dict) or not isinstance(record.get("name"), str) or Path(record["name"]).name != record["name"]:
                    raise ValueError("Whisper manifest contains an unsafe file name.")
                path = self.model_dir / record["name"]
                exists = path.is_file()
                size = path.stat().st_size if exists else None
                actual_hash = _sha256(path) if exists and size == record.get("size_bytes") else None
                valid = exists and size == record.get("size_bytes") and actual_hash == record.get("sha256")
                descriptor["frozen_artifacts"][record["name"]] = {"exists": exists, "expected_size_bytes": record.get("size_bytes"), "actual_size_bytes": size, "expected_sha256": record.get("sha256"), "actual_sha256": actual_hash, "valid": valid}
                all_valid = all_valid and valid
            model = descriptor["frozen_artifacts"].get("model.safetensors", {})
            descriptor["weight_available"] = bool(all_valid and model.get("actual_sha256") == MODEL_SHA256)
            descriptor["frozen_integrity_status"] = "available" if descriptor["weight_available"] else "unavailable"
        except (OSError, ValueError, json.JSONDecodeError) as error:
            descriptor["integrity_error"] = str(error)[:160]
        self._descriptor = descriptor
        return descriptor

    def transcribe(self, audio_path: Path) -> dict[str, Any]:
        descriptor = self.describe()
        base = {"schema_version": "whisper-asr-result-v1", "mock": False, "transcript": "", "language": "unknown", "segments": [], "confidence": None, "model": descriptor}
        if not descriptor["weight_available"]:
            return {**base, "status": "unavailable", "limitations": ["Whisper 固定工件不可用或哈希不匹配；未联网下载。"]}
        try:
            import numpy as np
            import torch
            from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline
            with wave.open(str(audio_path), "rb") as reader:
                if reader.getnchannels() != 1 or reader.getsampwidth() != 2 or reader.getframerate() != 16000:
                    raise ValueError("Expected 16 kHz mono 16-bit WAV.")
                samples = np.frombuffer(reader.readframes(reader.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
            if samples.size == 0 or float(np.sqrt(np.mean(np.square(samples)))) < 1e-4:
                return {**base, "status": "failed", "limitations": ["标准化音频为空或未检测到足够语音能量；未生成转写。"]}
            torch.set_num_threads(2)
            processor = AutoProcessor.from_pretrained(self.model_dir, local_files_only=True)
            model = AutoModelForSpeechSeq2Seq.from_pretrained(self.model_dir, local_files_only=True, use_safetensors=True)
            recognizer = pipeline("automatic-speech-recognition", model=model, tokenizer=processor.tokenizer, feature_extractor=processor.feature_extractor, device=-1, chunk_length_s=30)
            result = recognizer({"array": samples, "sampling_rate": 16000}, return_timestamps=True, return_language=True, generate_kwargs={"task": "transcribe"})
            segments = []
            for item in result.get("chunks", []):
                timestamp = item.get("timestamp") or (None, None)
                if not isinstance(item.get("text"), str) or timestamp[0] is None or timestamp[1] is None:
                    continue
                text = item["text"].strip()
                if text:
                    segments.append({"text": text, "start_seconds": round(float(timestamp[0]), 3), "end_seconds": round(float(timestamp[1]), 3), "confidence": self._confidence(item)})
            transcript = str(result.get("text") or "").strip()
            if not transcript:
                transcript = " ".join(item["text"] for item in segments).strip()
            if not transcript or not segments:
                return {**base, "status": "failed", "limitations": ["Whisper 已运行但没有生成带时间戳的语音文本；未创建主张输入。"]}
            confidences = [item["confidence"] for item in segments if item["confidence"] is not None]
            language = result.get("language")
            if not isinstance(language, str) or not language.strip():
                language = next((item.get("language") for item in result.get("chunks", []) if isinstance(item, dict) and isinstance(item.get("language"), str)), "unknown")
            limitations = ["转写文本是不可信媒体数据；仅带时间戳的片段可参与主张提取。"]
            if not confidences:
                limitations.append("当前 Transformers 输出未暴露 token 平均对数概率，confidence 保持为空，未伪造置信度。")
            return {**base, "status": "completed", "transcript": transcript, "segments": segments, "language": language, "confidence": round(sum(confidences) / len(confidences), 6) if confidences else None, "limitations": limitations}
        except BaseException as error:
            from .security import safe_error_trace
            return {**base, "status": "unavailable", "limitations": ["本地 Whisper ASR 加载或推理不可用。"], "error_trace": safe_error_trace(error)}

    @staticmethod
    def _confidence(item: dict[str, Any]) -> float | None:
        """Use exposed token log-probability metadata only; never invent it."""
        for key in ("avg_logprob", "average_logprob", "logprob"):
            value = item.get(key)
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                return round(min(1.0, max(0.0, math.exp(float(value)))), 6)
        value = item.get("score")
        if isinstance(value, (int, float)) and math.isfinite(float(value)) and 0.0 <= float(value) <= 1.0:
            return round(float(value), 6)
        return None
