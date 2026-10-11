"""One bounded, local visual deepfake baseline for phase 3.

The detector only consumes frames produced by the bounded phase-2 preprocessor.
It never fetches a model or data at runtime.  Scores are model outputs, not
calibrated probabilities and never establish that media is authentic.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Protocol, TypedDict


MODEL_NAME = "DeepfakeBench Meso4"
MODEL_VERSION = "DeepfakeBench release v1.0.1 / meso4_best.pth"
MODEL_LICENSE = "CC BY-NC 4.0"
MODEL_SOURCE = "https://github.com/SCLBD/DeepfakeBench/releases/tag/v1.0.1"
WEIGHT_SHA256 = "a362ebbef2658daa551d808fbfccd57f0b5fd34fda46a05996e807cccddff4e0"
DEFAULT_BATCH_SIZE = 16
DEFAULT_TIMEOUT_SECONDS = 30.0
RISK_THRESHOLD = 0.5


class ModelResult(TypedDict):
    """Stable, persisted result schema for one visual detector invocation."""

    schema_version: str
    status: str
    mock: bool
    model: dict[str, Any]
    configuration: dict[str, Any]
    input_quality: dict[str, Any]
    frame_risks: list[dict[str, Any]]
    track_risks: list[dict[str, Any]]
    video_risk: dict[str, Any] | None
    evidence: dict[str, Any]
    limitations: list[str]
    elapsed_seconds: float


class VisualDetector(Protocol):
    def detect(
        self,
        job: dict[str, Any],
        preprocess: dict[str, Any] | None,
        cancelled: Callable[[], bool],
    ) -> ModelResult:
        """Return a ModelResult without making network calls."""


class Meso4VisualDetector:
    """Adapter for the single, public DeepfakeBench Meso4 checkpoint.

    Meso4 is a frame classifier.  A "track" in this result is therefore an
    explicitly named sampled visual-frame sequence, not a person/face identity
    track.  That distinction prevents the preprocessor's mock face adapter
    from being presented as a real tracker.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        weights_path: Path | None = None,
        device: str | None = None,
        batch_size: int | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.data_dir = data_dir.resolve()
        project_root = Path(__file__).resolve().parents[2]
        configured_path = os.environ.get("MINGJIAN_MESO4_WEIGHTS")
        self.weights_path = (Path(configured_path) if configured_path else weights_path) or project_root / "models" / "cache" / "deepfakebench" / "meso4_best.pth"
        self.requested_device = (device or os.environ.get("MINGJIAN_VISUAL_DEVICE", "auto")).lower()
        self.batch_size = max(1, min(int(batch_size or os.environ.get("MINGJIAN_VISUAL_BATCH_SIZE", DEFAULT_BATCH_SIZE)), 32))
        self.timeout_seconds = max(1.0, min(float(timeout_seconds or os.environ.get("MINGJIAN_VISUAL_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)), 120.0))
        self._loaded: tuple[Any, Any, str, Any] | None = None

    def describe(self) -> dict[str, Any]:
        description = {
            "name": MODEL_NAME,
            "version": MODEL_VERSION,
            "license": MODEL_LICENSE,
            "weight_path": str(self.weights_path),
            "weight_available": self.weights_path.is_file(),
            "expected_weight_sha256": WEIGHT_SHA256,
        }
        if self.weights_path.is_file():
            description["weight_size_bytes"] = self.weights_path.stat().st_size
            description["weight_sha256"] = _sha256(self.weights_path)
        return description

    def detect(self, job: dict[str, Any], preprocess: dict[str, Any] | None, cancelled: Callable[[], bool]) -> ModelResult:
        started = time.monotonic()
        model = self.describe()
        configuration = {
            "input_resolution": [256, 256],
            "normalization": {"mean": [0.5, 0.5, 0.5], "std": [0.5, 0.5, 0.5]},
            "batch_size": self.batch_size,
            "timeout_seconds": self.timeout_seconds,
            "device_requested": self.requested_device,
        }
        frames, frame_problem = self._frames(preprocess)
        quality = self._quality(preprocess, frames, frame_problem)
        if cancelled():
            return self._empty("cancelled", model, configuration, quality, started, "任务在视觉推理开始前被取消。")
        if frame_problem:
            return self._empty("unavailable", model, configuration, quality, started, frame_problem)
        if not self.weights_path.is_file():
            return self._empty("unavailable", model, configuration, quality, started, "Meso4 权重文件不可用；未将缺失模型映射为低风险。")
        actual_hash = _sha256(self.weights_path)
        model["weight_sha256"] = actual_hash
        model["weight_size_bytes"] = self.weights_path.stat().st_size
        if actual_hash != WEIGHT_SHA256:
            return self._empty("unavailable", model, configuration, quality, started, "Meso4 权重 SHA-256 与已核验发布文件不一致，已拒绝加载。")
        try:
            torch, Image, selected_device = self._runtime()
            configuration["device_selected"] = selected_device
            result = self._infer(torch, Image, selected_device, frames, cancelled, started)
        except TimeoutError:
            return self._empty("timed_out", model, configuration, quality, started, "视觉推理超过配置时限；没有保留不完整的风险结论。")
        except RuntimeError as error:
            return self._empty("unavailable", model, configuration, quality, started, f"视觉模型不可用：{_safe_error(error)}")
        if result is None:
            return self._empty("cancelled", model, configuration, quality, started, "任务在视觉推理期间被取消。")
        frame_risks, unreadable = result
        if not frame_risks:
            quality["status"] = "unavailable"
            quality["reasons"].append("所有预处理帧均无法由图像解码器读取。")
            return self._empty("unavailable", model, configuration, quality, started, "没有可推理的图像帧；这不是低风险证据。")
        if unreadable:
            quality["status"] = "degraded"
            quality["reasons"].append(f"{unreadable} 个预处理帧无法读取，未参与风险聚合。")
        video_risk = max(frame_risks, key=lambda item: float(item["risk_score"]))
        track = {
            "track_id": "visual-stream-1",
            "track_type": "sampled_visual_frame_sequence",
            "risk_score": video_risk["risk_score"],
            "aggregation": "max_frame_risk",
            "frames_considered": len(frame_risks),
            "start_seconds": frame_risks[0]["timestamp_seconds"],
            "end_seconds": frame_risks[-1]["timestamp_seconds"],
            "limitation": "这是采样视觉帧序列聚合，不是人脸身份检测或跟踪。",
        }
        suspicious = _segments(frame_risks)
        return {
            "schema_version": "visual-detector-result-v1",
            "status": "completed",
            "mock": False,
            "model": model,
            "configuration": configuration,
            "input_quality": quality,
            "frame_risks": frame_risks,
            "track_risks": [track],
            "video_risk": {
                "risk_score": video_risk["risk_score"],
                "aggregation": "max_frame_risk",
                "score_semantics": "未经校准的 Meso4 假类 softmax 分数；不是真实性概率。",
            },
            "evidence": {"highest_risk_frame": video_risk, "suspicious_segments": suspicious},
            "limitations": [
                "Meso4 是单一视觉基线；仅分析预处理产生的采样帧。",
                "模型分数未经本项目数据校准，不能证明媒体真实或虚假。",
                "内容可信度和来源可靠性仍未由真实模型分析。",
            ],
            "elapsed_seconds": round(time.monotonic() - started, 6),
        }

    def _frames(self, preprocess: dict[str, Any] | None) -> tuple[list[dict[str, Any]], str | None]:
        if not preprocess or preprocess.get("status") != "completed":
            return [], "预处理未完成，视觉模型没有可用帧。"
        artifact = next((item for item in preprocess.get("artifacts", []) if item.get("name") == "frame_index"), None)
        if not artifact or not isinstance(artifact.get("path"), str):
            return [], "预处理未登记 frame_index 工件。"
        try:
            path = (self.data_dir / artifact["path"]).resolve()
            path.relative_to(self.data_dir)
            values = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return [], "frame_index 工件不可读取。"
        if not isinstance(values, list) or not values:
            return [], "预处理没有可用视频或图像帧。"
        return [item for item in values if isinstance(item, dict)], None

    @staticmethod
    def _quality(preprocess: dict[str, Any] | None, frames: list[dict[str, Any]], problem: str | None) -> dict[str, Any]:
        reasons: list[str] = []
        if problem:
            reasons.append(problem)
        estimated = sum(1 for frame in frames if frame.get("timestamp_quality") != "exact")
        if estimated:
            reasons.append(f"{estimated} 个帧时间戳为估算值。")
        reasons.append("当前基线直接使用整帧；未执行真实人脸检测、对齐或身份跟踪。")
        return {
            "status": "unavailable" if problem else ("degraded" if estimated else "available"),
            "frames_available": len(frames),
            "estimated_timestamp_frames": estimated,
            "frame_scope": "full_frame",
            "reasons": reasons,
            "preprocess_quality_gates": (preprocess or {}).get("quality_gates", []),
        }

    def _runtime(self) -> tuple[Any, Any, str]:
        if self._loaded:
            return self._loaded[0], self._loaded[1], self._loaded[2]
        try:
            import torch
            from PIL import Image
        except ImportError as error:
            raise RuntimeError("PyTorch 或 Pillow 未安装。") from error
        if self.requested_device not in {"auto", "cpu", "cuda"}:
            raise RuntimeError("MINGJIAN_VISUAL_DEVICE 只能为 auto、cpu 或 cuda。")
        cuda_ready = bool(torch.cuda.is_available())
        if self.requested_device == "cuda" and not cuda_ready:
            raise RuntimeError("请求 CUDA，但当前 PyTorch 运行时没有可用 CUDA 设备。")
        selected = "cuda" if self.requested_device == "cuda" or (self.requested_device == "auto" and cuda_ready) else "cpu"
        model = _meso4(torch)
        try:
            checkpoint = torch.load(self.weights_path, map_location="cpu", weights_only=True)
        except TypeError:  # only for an older explicitly installed PyTorch runtime
            checkpoint = torch.load(self.weights_path, map_location="cpu")
        state = _state_dict(checkpoint)
        try:
            model.load_state_dict(state, strict=True)
        except RuntimeError as error:
            raise RuntimeError("Meso4 权重结构与公开网络定义不匹配。") from error
        model.eval().to(selected)
        self._loaded = (torch, Image, selected, model)
        return torch, Image, selected

    def _infer(self, torch: Any, Image: Any, selected_device: str, frames: list[dict[str, Any]], cancelled: Callable[[], bool], started: float) -> tuple[list[dict[str, Any]], int] | None:
        if not self._loaded:
            raise RuntimeError("视觉运行时未初始化。")
        model = self._loaded[3]
        prepared: list[tuple[dict[str, Any], Any]] = []
        unreadable = 0
        for frame in frames:
            if cancelled():
                return None
            _deadline(started, self.timeout_seconds)
            path = frame.get("path")
            if not isinstance(path, str):
                unreadable += 1
                continue
            try:
                frame_path = (self.data_dir / path).resolve()
                frame_path.relative_to(self.data_dir)
                with Image.open(frame_path) as image:
                    image = image.convert("RGB").resize((256, 256), Image.Resampling.BILINEAR)
                    pixels = torch.frombuffer(bytearray(image.tobytes()), dtype=torch.uint8).reshape(256, 256, 3)
                tensor = pixels.permute(2, 0, 1).to(dtype=torch.float32).div(255.0).sub(0.5).div(0.5)
                prepared.append((frame, tensor))
            except (OSError, ValueError):
                unreadable += 1
        output: list[dict[str, Any]] = []
        with torch.inference_mode():
            for offset in range(0, len(prepared), self.batch_size):
                if cancelled():
                    return None
                _deadline(started, self.timeout_seconds)
                batch = torch.stack([tensor for _, tensor in prepared[offset:offset + self.batch_size]]).to(selected_device)
                scores = torch.softmax(model(batch), dim=1)[:, 1].detach().to("cpu").tolist()
                _deadline(started, self.timeout_seconds)
                for (frame, _), score in zip(prepared[offset:offset + self.batch_size], scores):
                    output.append({
                        "frame_index": frame.get("index"),
                        "timestamp_seconds": float(frame.get("timestamp_seconds", 0.0)),
                        "timestamp_quality": frame.get("timestamp_quality", "estimated"),
                        "path": frame.get("path"),
                        "risk_score": round(float(score), 6),
                        "risk_threshold": RISK_THRESHOLD,
                        "score_semantics": "Meso4 假类 softmax 分数（未经校准）。",
                    })
        return output, unreadable

    def _empty(self, status: str, model: dict[str, Any], configuration: dict[str, Any], quality: dict[str, Any], started: float, limitation: str) -> ModelResult:
        return {
            "schema_version": "visual-detector-result-v1",
            "status": status,
            "mock": False,
            "model": model,
            "configuration": configuration,
            "input_quality": quality,
            "frame_risks": [],
            "track_risks": [],
            "video_risk": None,
            "evidence": {"highest_risk_frame": None, "suspicious_segments": []},
            "limitations": [limitation, "不可用、取消或超时不表示低风险。"],
            "elapsed_seconds": round(time.monotonic() - started, 6),
        }


def _meso4(torch: Any) -> Any:
    nn = torch.nn

    class Meso4(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.conv1 = nn.Conv2d(3, 8, 3, padding=1, bias=False)
            self.bn1 = nn.BatchNorm2d(8)
            self.relu = nn.ReLU(inplace=True)
            self.leakyrelu = nn.LeakyReLU(0.1)
            self.conv2 = nn.Conv2d(8, 8, 5, padding=2, bias=False)
            self.bn2 = nn.BatchNorm2d(16)
            self.conv3 = nn.Conv2d(8, 16, 5, padding=2, bias=False)
            self.conv4 = nn.Conv2d(16, 16, 5, padding=2, bias=False)
            self.maxpooling1 = nn.MaxPool2d(kernel_size=(2, 2))
            self.maxpooling2 = nn.MaxPool2d(kernel_size=(4, 4))
            # The published reference uses Dropout2d after flattening.  In
            # evaluation mode both variants are identities; Dropout avoids a
            # current PyTorch deprecation warning for this two-dimensional use.
            self.dropout = nn.Dropout(0.5)
            self.fc1 = nn.Linear(16 * 8 * 8, 16)
            self.fc2 = nn.Linear(16, 2)

        def forward(self, value: Any) -> Any:
            value = self.maxpooling1(self.bn1(self.relu(self.conv1(value))))
            value = self.maxpooling1(self.bn1(self.relu(self.conv2(value))))
            value = self.maxpooling1(self.bn2(self.relu(self.conv3(value))))
            value = self.maxpooling2(self.bn2(self.relu(self.conv4(value))))
            value = value.reshape(value.size(0), -1)
            value = self.dropout(value)
            value = self.leakyrelu(self.fc1(value))
            value = self.dropout(value)
            return self.fc2(value)

    return Meso4()


def _state_dict(checkpoint: Any) -> dict[str, Any]:
    if not isinstance(checkpoint, dict):
        raise RuntimeError("Meso4 权重不是预期的 state_dict。")
    for key in ("state_dict", "model", "model_state_dict"):
        if isinstance(checkpoint.get(key), dict):
            checkpoint = checkpoint[key]
            break
    if not all(isinstance(key, str) for key in checkpoint):
        raise RuntimeError("Meso4 权重键无效。")
    return {key.removeprefix("module.").removeprefix("backbone."): value for key, value in checkpoint.items()}


def _segments(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = [frame for frame in frames if float(frame["risk_score"]) >= RISK_THRESHOLD]
    if not selected:
        return []
    segments: list[list[dict[str, Any]]] = [[selected[0]]]
    for frame in selected[1:]:
        previous = segments[-1][-1]
        if float(frame["timestamp_seconds"]) - float(previous["timestamp_seconds"]) <= 2.5:
            segments[-1].append(frame)
        else:
            segments.append([frame])
    return [{
        "start_seconds": group[0]["timestamp_seconds"],
        "end_seconds": group[-1]["timestamp_seconds"],
        "peak_risk_score": max(float(frame["risk_score"]) for frame in group),
        "frame_indices": [frame["frame_index"] for frame in group],
        "selection_rule": f"Meso4 score >= {RISK_THRESHOLD}",
    } for group in segments]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _deadline(started: float, timeout_seconds: float) -> None:
    if time.monotonic() - started > timeout_seconds:
        raise TimeoutError()


def _safe_error(error: Exception) -> str:
    return str(error).replace(str(Path.home()), "~")[:240]
