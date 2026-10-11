"""Local OpenCV YuNet face detection with simple IoU track assignment."""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Any

from .security import safe_error_trace


MODEL_SOURCE_URL = "https://github.com/opencv/opencv_zoo/blob/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
DEFAULT_MODEL_PATH = Path("models/cache/opencv-yunet/face_detection_yunet_2023mar.onnx")
DEFAULT_OPENCV_DIR = Path("models/cache/opencv-haar/python")
ALLOWED_FRAME_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class OpenCvYuNetFaceAdapter:
    """Detect five landmarks per face and join adjacent sampled frames by IoU."""

    def __init__(self, data_dir: Path, *, model_path: Path | None = None, opencv_dir: Path | None = None, iou_threshold: float = 0.30) -> None:
        self.data_dir = data_dir.resolve()
        self.model_path = model_path or Path(os.environ.get("MINGJIAN_YUNET_MODEL", str(DEFAULT_MODEL_PATH)))
        self.opencv_dir = opencv_dir or Path(os.environ.get("MINGJIAN_OPENCV_DIR", str(DEFAULT_OPENCV_DIR)))
        self.iou_threshold = min(1.0, max(0.0, iou_threshold))

    def describe(self) -> dict[str, Any]:
        try:
            cv2 = self._load_cv2()
            model_exists = self.model_path.is_file()
            return {
                "name": "OpenCV YuNet face detector",
                "version": "face_detection_yunet_2023mar",
                "license": "OpenCV Zoo model terms; verify before provisioning",
                "model_source": MODEL_SOURCE_URL,
                "model_path": str(self.model_path),
                "model_available": model_exists,
                "model_sha256": _sha256(self.model_path) if model_exists else None,
                "dependency_available": cv2 is not None and hasattr(cv2, "FaceDetectorYN"),
                "remote_downloads_disabled": True,
            }
        except BaseException as error:
            return {
                "name": "OpenCV YuNet face detector",
                "version": "face_detection_yunet_2023mar",
                "model_source": MODEL_SOURCE_URL,
                "model_available": False,
                "dependency_available": False,
                "remote_downloads_disabled": True,
                "error_trace": safe_error_trace(error),
            }

    def detect_and_track(self, frame_index: list[dict[str, Any]]) -> dict[str, Any]:
        descriptor = self.describe()
        base = {
            "schema_version": "face-detection-result-v1",
            "mock": False,
            "tracks": [],
            "frames_considered": len(frame_index),
            "frames_processed": 0,
            "model": descriptor,
            "limitations": [
                "IoU 轨迹只连接相邻采样帧中的几何框，不执行人脸身份识别。",
                "无人脸、遮挡、小脸和侧脸不能被解释为低风险证据。",
            ],
        }
        if not descriptor.get("dependency_available") or not descriptor.get("model_available"):
            return {**base, "status": "unavailable", "error_trace": descriptor.get("error_trace", "YuNet dependency or local ONNX artifact is unavailable.")}
        try:
            cv2 = self._load_cv2()
            detector = cv2.FaceDetectorYN.create(str(self.model_path), "", (320, 320), 0.8, 0.3, 5000)
            tracks: list[dict[str, Any]] = []
            active: dict[int, list[float]] = {}
            next_track_id = 1
            processed = 0
            for frame_record in frame_index:
                path = self._frame_path(frame_record)
                if path is None:
                    continue
                image = cv2.imread(str(path))
                if image is None or getattr(image, "size", 0) == 0:
                    continue
                height, width = image.shape[:2]
                detector.setInputSize((int(width), int(height)))
                _retval, faces = detector.detect(image)
                processed += 1
                detections = [] if faces is None else [self._detection(row, width, height) for row in faces]
                detections = [item for item in detections if item is not None]
                unmatched = set(active)
                next_active: dict[int, list[float]] = {}
                for detection in sorted(detections, key=lambda item: item["confidence"], reverse=True):
                    candidates = [(self._iou(detection["bbox"], active[track_id]), track_id) for track_id in unmatched]
                    overlap, track_id = max(candidates, default=(0.0, 0))
                    if overlap < self.iou_threshold:
                        track_id = next_track_id
                        next_track_id += 1
                        tracks.append({"track_id": track_id, "frame_indices": [], "bboxes": [], "landmarks": [], "confidences": []})
                    else:
                        unmatched.discard(track_id)
                    track = next(item for item in tracks if item["track_id"] == track_id)
                    track["frame_indices"].append(frame_record.get("index"))
                    track["bboxes"].append(detection["bbox"])
                    track["landmarks"].append(detection["landmarks"])
                    track["confidences"].append(detection["confidence"])
                    next_active[track_id] = detection["bbox"]
                active = next_active
            return {**base, "status": "available", "tracks": tracks, "frames_processed": processed}
        except BaseException as error:
            return {**base, "status": "unavailable", "error_trace": safe_error_trace(error)}

    def _load_cv2(self) -> Any:
        if self.opencv_dir.is_dir() and str(self.opencv_dir) not in sys.path:
            sys.path.append(str(self.opencv_dir))
        import cv2  # type: ignore[import-not-found]
        return cv2

    def _frame_path(self, record: dict[str, Any]) -> Path | None:
        value = record.get("path")
        if not isinstance(value, str):
            return None
        try:
            path = (self.data_dir / value).resolve()
            path.relative_to((self.data_dir / "derived").resolve())
        except ValueError:
            return None
        return path if path.is_file() and path.suffix.lower() in ALLOWED_FRAME_SUFFIXES else None

    @staticmethod
    def _detection(row: Any, width: int, height: int) -> dict[str, Any] | None:
        values = [float(item) for item in row]
        if len(values) < 15:
            return None
        x1, y1 = max(0.0, values[0]), max(0.0, values[1])
        x2, y2 = min(float(width), x1 + max(0.0, values[2])), min(float(height), y1 + max(0.0, values[3]))
        if x2 <= x1 or y2 <= y1:
            return None
        landmarks = [[round(values[index], 3), round(values[index + 1], 3)] for index in range(4, 14, 2)]
        return {"bbox": [round(x1, 3), round(y1, 3), round(x2, 3), round(y2, 3)], "landmarks": landmarks, "confidence": round(values[14], 6)}

    @staticmethod
    def _iou(left: list[float], right: list[float]) -> float:
        x1, y1 = max(left[0], right[0]), max(left[1], right[1])
        x2, y2 = min(left[2], right[2]), min(left[3], right[3])
        intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
        left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
        right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
        union = left_area + right_area - intersection
        return intersection / union if union > 0 else 0.0
