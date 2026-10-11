"""Bounded OCR over FFmpeg-derived keyframes using local Tesseract only."""
from __future__ import annotations

import csv
import io
import math
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable


MAX_OCR_FRAMES = 12
OCR_TIMEOUT_SECONDS = 30.0
OCR_TOTAL_BASE_SECONDS = 30.0
OCR_TOTAL_PER_FRAME_SECONDS = 20.0
OCR_TOTAL_MAX_SECONDS = 300.0
OCR_OUTER_MARGIN_SECONDS = 60.0
OCR_OUTER_MAX_SECONDS = 360.0
MAX_OCR_OUTPUT_CHARS = 256_000
ALLOWED_FRAME_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def _env_seconds(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and minimum <= value <= maximum else default


def ocr_selected_frame_count(frame_index: list[dict[str, Any]]) -> int:
    return min(sum(isinstance(item, dict) for item in frame_index), MAX_OCR_FRAMES)


def ocr_timeout_policy(frame_count: int) -> dict[str, float | int]:
    selected_frames = min(max(int(frame_count), 0), MAX_OCR_FRAMES)
    per_frame = _env_seconds("MINGJIAN_OCR_FRAME_TIMEOUT_SECONDS", OCR_TIMEOUT_SECONDS, 1.0, OCR_TOTAL_MAX_SECONDS)
    base = _env_seconds("MINGJIAN_OCR_TOTAL_BASE_SECONDS", OCR_TOTAL_BASE_SECONDS, 1.0, OCR_TOTAL_MAX_SECONDS)
    per_frame_total = _env_seconds("MINGJIAN_OCR_TOTAL_PER_FRAME_SECONDS", OCR_TOTAL_PER_FRAME_SECONDS, 1.0, OCR_TOTAL_MAX_SECONDS)
    outer_margin = _env_seconds("MINGJIAN_OCR_OUTER_MARGIN_SECONDS", OCR_OUTER_MARGIN_SECONDS, 1.0, OCR_OUTER_MAX_SECONDS - 1.0)
    total = min(OCR_TOTAL_MAX_SECONDS, base + per_frame_total * selected_frames)
    outer = min(OCR_OUTER_MAX_SECONDS, total + outer_margin)
    return {"selected_frames": selected_frames, "per_frame": per_frame, "total": total, "outer": outer}


class TesseractOcrAdapter:
    """Runs a pinned local executable on bounded, already-derived frames."""

    def __init__(
        self,
        data_dir: Path,
        *,
        executable: str | None = None,
        languages: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.data_dir = data_dir.resolve()
        self.executable = executable or os.environ.get("MINGJIAN_TESSERACT_PATH", "tesseract")
        self.languages = languages or os.environ.get("MINGJIAN_TESSERACT_LANGUAGES", "chi_sim+eng")
        self.runner = runner or subprocess.run

    def describe(self) -> dict[str, Any]:
        resolved = shutil.which(self.executable) if not Path(self.executable).is_file() else self.executable
        maximum_policy = ocr_timeout_policy(MAX_OCR_FRAMES)
        return {
            "name": "Tesseract OCR",
            "version": "5.3.0 Debian package",
            "license": "Apache-2.0",
            "languages": self.languages,
            "executable": resolved or self.executable,
            "available": bool(resolved),
            "remote_downloads_disabled": True,
            "max_frames": MAX_OCR_FRAMES,
            "timeout_seconds_per_frame": maximum_policy["per_frame"],
            "timeout_seconds_total": maximum_policy["total"],
            "timeout_seconds_total_hard_max": OCR_TOTAL_MAX_SECONDS,
            "timeout_seconds_outer": maximum_policy["outer"],
            "timeout_seconds_outer_hard_max": OCR_OUTER_MAX_SECONDS,
        }

    def extract(self, frame_index: list[dict[str, Any]]) -> dict[str, Any]:
        started = time.monotonic()
        selected = self._sample(frame_index)
        timeout_policy = ocr_timeout_policy(len(selected))
        descriptor = self.describe()
        descriptor.update({
            "selected_frames": timeout_policy["selected_frames"],
            "timeout_seconds_per_frame": timeout_policy["per_frame"],
            "timeout_seconds_total": timeout_policy["total"],
            "timeout_seconds_outer": timeout_policy["outer"],
        })
        if not descriptor["available"]:
            return self._unavailable(descriptor, "Tesseract OCR 可执行文件不可用；未联网下载。", len(frame_index))
        items: list[dict[str, Any]] = []
        diagnostics: list[str] = []
        processed_frames = 0
        for frame in selected:
            remaining = float(timeout_policy["total"]) - (time.monotonic() - started)
            if remaining <= 0:
                diagnostics.append(f"OCR module reached its {timeout_policy['total']:.0f}-second total budget; remaining keyframes were skipped.")
                break
            path = self._frame_path(frame)
            if path is None:
                diagnostics.append("关键帧路径未通过派生目录校验。")
                continue
            command = [str(descriptor["executable"]), str(path), "stdout", "-l", self.languages, "--psm", "6", "tsv"]
            try:
                completed = self.runner(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=min(float(timeout_policy["per_frame"]), max(0.1, remaining)),
                    check=False,
                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "OMP_THREAD_LIMIT": "1", "TESSDATA_PREFIX": os.environ.get("TESSDATA_PREFIX", "/usr/share/tesseract-ocr/5/tessdata")},
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                diagnostics.append(f"OCR 帧 {frame.get('index')} 未完成：{str(error)[:120]}")
                continue
            stdout = completed.stdout[:MAX_OCR_OUTPUT_CHARS] if isinstance(completed.stdout, str) else ""
            if completed.returncode != 0:
                stderr = completed.stderr[:160] if isinstance(completed.stderr, str) else ""
                diagnostics.append(f"OCR 帧 {frame.get('index')} 返回 {completed.returncode}：{stderr}")
                continue
            processed_frames += 1
            for item in self._parse_tsv(stdout):
                items.append({
                    "text": item["text"],
                    "frame_index": frame.get("index"),
                    "timestamp_seconds": frame.get("timestamp_seconds"),
                    "timestamp_quality": frame.get("timestamp_quality", "unknown"),
                    "confidence": item["confidence"],
                    "bbox": item["bbox"],
                })
        status = "completed" if items or selected and len(diagnostics) < len(selected) else "unavailable"
        return {
            "schema_version": "tesseract-ocr-result-v1",
            "mock": False,
            "status": status,
            "items": items,
            "frames_considered": len(frame_index),
            "frames_processed": processed_frames,
            "model": descriptor,
            "diagnostics": diagnostics[:20],
            "limitations": [f"OCR processes at most {MAX_OCR_FRAMES} FFmpeg-derived keyframes with a {timeout_policy['total']:.0f}-second dynamic total budget; text remains untrusted media data."],
        }

    def _frame_path(self, frame: dict[str, Any]) -> Path | None:
        value = frame.get("path")
        if not isinstance(value, str):
            return None
        try:
            path = (self.data_dir / value).resolve()
            path.relative_to(self.data_dir / "derived")
        except ValueError:
            return None
        return path if path.is_file() and path.suffix.lower() in ALLOWED_FRAME_SUFFIXES else None

    @staticmethod
    def _sample(frame_index: list[dict[str, Any]]) -> list[dict[str, Any]]:
        values = [item for item in frame_index if isinstance(item, dict)]
        if len(values) <= MAX_OCR_FRAMES:
            return values
        indexes = sorted({round(position * (len(values) - 1) / (MAX_OCR_FRAMES - 1)) for position in range(MAX_OCR_FRAMES)})
        return [values[index] for index in indexes]

    @staticmethod
    def _parse_tsv(value: str) -> list[dict[str, Any]]:
        words: list[dict[str, Any]] = []
        try:
            for row in csv.DictReader(io.StringIO(value), delimiter="\t"):
                word = row.get("text", "").strip()
                if not word:
                    continue
                try:
                    confidence = float(row.get("conf", "-1"))
                    left = int(row.get("left", "0"))
                    top = int(row.get("top", "0"))
                    width = int(row.get("width", "0"))
                    height = int(row.get("height", "0"))
                except (TypeError, ValueError):
                    continue
                if width <= 0 or height <= 0:
                    continue
                try:
                    line_key = tuple(int(row.get(key, "0")) for key in ("page_num", "block_num", "par_num", "line_num"))
                except (TypeError, ValueError):
                    line_key = (0, 0, 0, 0)
                words.append({"text": word, "confidence": confidence / 100.0 if confidence >= 0 else None, "left": left, "top": top, "right": left + width, "bottom": top + height, "line_key": line_key})
        except csv.Error:
            return []
        groups: list[list[dict[str, Any]]] = []
        for word in sorted(words, key=lambda item: (*item["line_key"], item["left"])):
            if not groups:
                groups.append([word])
                continue
            previous = groups[-1][-1]
            max_height = max(previous["bottom"] - previous["top"], word["bottom"] - word["top"])
            near = word["line_key"] == previous["line_key"] and word["left"] - previous["right"] <= max(32, 2 * max_height)
            if near:
                groups[-1].append(word)
            else:
                groups.append([word])
        items: list[dict[str, Any]] = []
        for group in groups:
            confidences = [item["confidence"] for item in group if item["confidence"] is not None]
            items.append({
                "text": " ".join(item["text"] for item in group).strip()[:4_000],
                "confidence": round(sum(confidences) / len(confidences), 4) if confidences else None,
                "bbox": [min(item["left"] for item in group), min(item["top"] for item in group), max(item["right"] for item in group), max(item["bottom"] for item in group)],
            })
        return items

    @staticmethod
    def _unavailable(model: dict[str, Any], limitation: str, frames_considered: int) -> dict[str, Any]:
        return {"schema_version": "tesseract-ocr-result-v1", "mock": False, "status": "unavailable", "items": [], "frames_considered": frames_considered, "frames_processed": 0, "model": model, "diagnostics": [], "limitations": [limitation]}
