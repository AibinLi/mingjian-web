"""Deterministically extract Celeb-DF v2 test frames and run local Meso4."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.visual_detector import Meso4VisualDetector  # noqa: E402
from evaluation.celeb_df_v2 import DATASET_NAME, DATASET_VERSION, load_manifest, sha256_file  # noqa: E402


FRAME_RATE_FILTER = "fps=1/2"
FRAME_LIMIT = 150
FRAME_SIZE = [256, 256]
FFMPEG_TIMEOUT_SECONDS = 60.0


def extraction_command(source: Path, target_pattern: Path) -> list[str]:
    return [
        "ffmpeg", "-nostdin", "-y", "-v", "info", "-threads", "1", "-i", str(source),
        "-map", "0:v:0", "-vf", f"{FRAME_RATE_FILTER},scale=256:256:flags=bilinear,showinfo",
        "-frames:v", str(FRAME_LIMIT), "-q:v", "2", str(target_pattern),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True, help="Writable directory outside the original dataset.")
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--model-timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()

    root = args.dataset_root.resolve()
    work = args.work_dir.resolve()
    _outside_dataset(work, root, "--work-dir")
    _outside_dataset(args.predictions.resolve(), root, "--predictions")
    _outside_dataset(args.summary.resolve(), root, "--summary")
    _outside_dataset(args.config.resolve(), root, "--config")
    if args.predictions.exists() or args.summary.exists() or args.config.exists():
        raise SystemExit("Prediction, summary and config destinations must not already exist; choose a fresh run directory.")
    rows = load_manifest(args.manifest)
    if any(row["dataset_name"] != DATASET_NAME or row["dataset_version"] != DATASET_VERSION for row in rows):
        raise SystemExit("Manifest is not a Celeb-DF v2 manifest.")
    if len({row["official_testing_list_sha256"] for row in rows}) != 1:
        raise SystemExit("Manifest has inconsistent official test-list hashes.")
    work.mkdir(parents=True, exist_ok=True)
    configuration = {
        "schema_version": "celeb-df-v2-evaluation-config-v1",
        "dataset_name": DATASET_NAME,
        "dataset_version": DATASET_VERSION,
        "manifest_path": str(args.manifest.resolve()),
        "manifest_sha256": sha256_file(args.manifest),
        "official_testing_list_sha256": rows[0]["official_testing_list_sha256"],
        "frame_extraction": {"ffmpeg": args.ffmpeg, "filter": FRAME_RATE_FILTER, "scale": FRAME_SIZE, "jpeg_quality": 2, "threads": 1, "frame_limit": FRAME_LIMIT, "timeout_seconds": FFMPEG_TIMEOUT_SECONDS},
        "model": {"name": "DeepfakeBench Meso4", "device_requested": args.device, "batch_size": args.batch_size, "timeout_seconds": args.model_timeout_seconds, "input_resolution": FRAME_SIZE, "normalization": {"mean": [0.5, 0.5, 0.5], "std": [0.5, 0.5, 0.5]}},
        "score_semantics": "Meso4 fake-class softmax score is uncalibrated and is not a media-authenticity probability.",
    }
    args.config.parent.mkdir(parents=True, exist_ok=True)
    args.config.write_text(json.dumps(configuration, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    detector = Meso4VisualDetector(work, device=args.device, batch_size=args.batch_size, timeout_seconds=args.model_timeout_seconds)
    started = time.monotonic()
    status_counts: Counter[str] = Counter()
    label_counts: Counter[str] = Counter()
    args.predictions.parent.mkdir(parents=True, exist_ok=True)
    with args.predictions.open("x", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            sample_started = time.monotonic()
            prediction = evaluate_row(row, root, work, detector, args.ffmpeg, configuration["manifest_sha256"])
            prediction["runtime_seconds"] = round(time.monotonic() - sample_started, 6)
            status_counts[prediction["status"]] += 1
            label_counts[prediction["label"]] += 1
            stream.write(json.dumps(prediction, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
    summary = {
        "schema_version": "celeb-df-v2-evaluation-summary-v1",
        "dataset_name": DATASET_NAME,
        "dataset_version": DATASET_VERSION,
        "manifest_path": str(args.manifest.resolve()),
        "manifest_sha256": configuration["manifest_sha256"],
        "official_testing_list_sha256": configuration["official_testing_list_sha256"],
        "predictions_path": str(args.predictions.resolve()),
        "predictions_sha256": sha256_file(args.predictions),
        "total_samples": len(rows),
        "label_counts": dict(sorted(label_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "completed_samples": status_counts["completed"],
        "non_completed_samples": len(rows) - status_counts["completed"],
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "model": detector.describe(),
        "configuration_path": str(args.config.resolve()),
        "score_semantics": configuration["score_semantics"],
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


def evaluate_row(row: dict[str, Any], dataset_root: Path, work: Path, detector: Meso4VisualDetector, ffmpeg: str, manifest_hash: str) -> dict[str, Any]:
    source = (dataset_root / Path(row["relative_path"])).resolve()
    try:
        source.relative_to(dataset_root)
    except ValueError:
        return _failed(row, manifest_hash, "source_path_invalid", "Manifest path escaped dataset root.")
    if not source.is_file():
        return _failed(row, manifest_hash, "source_missing", "Video disappeared after manifest generation.")
    if sha256_file(source) != row["source_sha256"]:
        return _failed(row, manifest_hash, "source_hash_mismatch", "Video SHA-256 changed after manifest generation.")
    frame_dir = work / "frames" / row["sample_id"]
    if frame_dir.exists():
        return _failed(row, manifest_hash, "frame_output_exists", "Evaluation work directory already contains this sample; no files were deleted.")
    frame_dir.mkdir(parents=True)
    command = extraction_command(source, frame_dir / "frame-%06d.jpg")
    try:
        completed = subprocess.run(command if ffmpeg == "ffmpeg" else [ffmpeg, *command[1:]], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=FFMPEG_TIMEOUT_SECONDS, check=False)
    except FileNotFoundError:
        return _failed(row, manifest_hash, "ffmpeg_unavailable", "FFmpeg executable was not found.", command=command)
    except subprocess.TimeoutExpired as error:
        return _failed(row, manifest_hash, "ffmpeg_timeout", "Deterministic frame extraction timed out.", command=command, stderr=(error.stderr or "")[:16384])
    if completed.returncode != 0:
        return _failed(row, manifest_hash, "decode_failed", "FFmpeg could not decode this test video.", command=command, stderr=completed.stderr[:16384])
    paths = sorted(frame_dir.glob("frame-*.jpg"))
    if not paths:
        return _failed(row, manifest_hash, "no_frames", "FFmpeg completed but produced no video frames.", command=command, stderr=completed.stderr[:16384])
    timestamps = [float(value) for value in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", completed.stderr)]
    exact = len(timestamps) >= len(paths)
    index = [{"index": position, "timestamp_seconds": round(timestamps[position - 1], 6) if exact else round((position - 1) * 2.0, 6), "timestamp_quality": "exact" if exact else "estimated", "path": path.resolve().relative_to(work).as_posix()} for position, path in enumerate(paths, start=1)]
    index_path = frame_dir / "frame_index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    result = detector.detect({"id": row["sample_id"]}, {"status": "completed", "artifacts": [{"name": "frame_index", "path": index_path.resolve().relative_to(work).as_posix()}], "quality_gates": []}, lambda: False)
    status = result["status"]
    prediction = _base_prediction(row, manifest_hash)
    prediction.update({"status": status, "risk_score": result["video_risk"]["risk_score"] if result["video_risk"] else None, "quality_status": (result.get("input_quality") or {}).get("status"), "failure_reason": _failure_reason(result), "frame_extraction": {"status": "completed", "frame_count": len(index), "timestamp_quality": "exact" if exact else "estimated", "command": command, "stderr": completed.stderr[:16384]}, "model_result": result})
    if row.get("dataset_role") == "calibration":
        prediction.update({"model_key": "visual.meso4.fake_score", "model_weight_sha256": result["model"].get("weight_sha256")})
    return prediction


def _base_prediction(row: dict[str, Any], manifest_hash: str) -> dict[str, Any]:
    prediction = {"schema_version": "celeb-df-v2-prediction-jsonl-v1", "sample_id": row["sample_id"], "relative_path": row["relative_path"], "label": row["binary_label_fake"], "label_name": row["label"], "source_sha256": row["source_sha256"], "dataset_name": row["dataset_name"], "dataset_version": row["dataset_version"], "official_testing_list_sha256": row["official_testing_list_sha256"], "manifest_sha256": manifest_hash}
    for key in ("dataset_role", "official_test_overlap_count", "group_id", "selection_seed", "selection_rule"):
        if key in row:
            prediction[key] = row[key]
    return prediction


def _failed(row: dict[str, Any], manifest_hash: str, status: str, reason: str, *, command: list[str] | None = None, stderr: str | None = None) -> dict[str, Any]:
    prediction = _base_prediction(row, manifest_hash)
    prediction.update({"status": status, "risk_score": None, "quality_status": "unavailable", "failure_reason": reason, "failure": {"reason": reason, "command": command, "stderr": stderr}})
    return prediction


def _outside_dataset(path: Path, dataset_root: Path, option: str) -> None:
    try:
        path.relative_to(dataset_root)
    except ValueError:
        return
    raise SystemExit(f"{option} must be outside --dataset-root; original dataset is read-only.")


def _failure_reason(result: dict[str, Any]) -> str | None:
    if result.get("status") == "completed":
        return None
    limitations = result.get("limitations")
    if isinstance(limitations, list) and limitations:
        return str(limitations[0])
    return "Model did not complete; no low-risk inference is made."


if __name__ == "__main__":
    main()
