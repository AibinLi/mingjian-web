"""Create auditable Meso4 predictions for a frozen, labelled frame manifest.

The script intentionally does not download a dataset.  Each JSONL manifest row
must contain ``sample_id``, ``label`` (0 real / 1 fake) and ``frames``.  Frame
paths are relative to ``--dataset-root`` and must point to already authorised,
non-user test media.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.visual_detector import Meso4VisualDetector  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="Frozen labelled JSONL manifest.")
    parser.add_argument("--dataset-root", type=Path, required=True, help="Root used to resolve each frame path.")
    parser.add_argument("--output", type=Path, required=True, help="Destination raw prediction JSONL.")
    parser.add_argument("--work-dir", type=Path, required=True, help="Writable directory under dataset-root for temporary frame indexes.")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()

    root = args.dataset_root.resolve()
    work = args.work_dir.resolve()
    try:
        work.relative_to(root)
    except ValueError as error:
        raise SystemExit("--work-dir must be inside --dataset-root so paths stay bounded.") from error
    if not args.manifest.is_file():
        raise SystemExit("Manifest does not exist.")
    rows = _read_manifest(args.manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    detector = Meso4VisualDetector(root, device=args.device, batch_size=args.batch_size, timeout_seconds=args.timeout_seconds)
    manifest_sha256 = _sha256(args.manifest)

    with args.output.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            sample_started = time.monotonic()
            sample_id = row["sample_id"]
            frames = _validated_frames(row, root)
            frame_index = work / f"{sample_id}.frame_index.json"
            frame_index.write_text(json.dumps(frames, ensure_ascii=False), encoding="utf-8")
            relative_index = frame_index.relative_to(root).as_posix()
            preprocess = {"status": "completed", "artifacts": [{"name": "frame_index", "path": relative_index}], "quality_gates": []}
            result = detector.detect({"id": f"evaluation-{sample_id}"}, preprocess, lambda: False)
            prediction = {
                "schema_version": "visual-prediction-jsonl-v1",
                "sample_id": sample_id,
                "label": row["label"],
                "manifest_sha256": manifest_sha256,
                "input_sha256": row.get("sha256"),
                "model": result["model"],
                "configuration": result["configuration"],
                "status": result["status"],
                "risk_score": result["video_risk"]["risk_score"] if result["video_risk"] else None,
                "quality_status": (result.get("input_quality") or {}).get("status"),
                "failure_reason": _failure_reason(result),
                "runtime_seconds": round(time.monotonic() - sample_started, 6),
                "model_result": result,
            }
            stream.write(json.dumps(prediction, ensure_ascii=False, sort_keys=True) + "\n")


def _read_manifest(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    sample_ids: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise SystemExit(f"Manifest line {line_number} is not valid JSON.") from error
        if not isinstance(row, dict) or not isinstance(row.get("sample_id"), str) or row["sample_id"] in sample_ids:
            raise SystemExit(f"Manifest line {line_number} requires a unique string sample_id.")
        if row.get("label") not in (0, 1) or not isinstance(row.get("frames"), list) or not row["frames"]:
            raise SystemExit(f"Manifest line {line_number} requires label 0/1 and a non-empty frames list.")
        sample_ids.add(row["sample_id"])
        rows.append(row)
    if not rows:
        raise SystemExit("Manifest contains no samples; no metric may be claimed.")
    return rows


def _validated_frames(row: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for index, raw in enumerate(row["frames"], start=1):
        if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
            raise SystemExit(f"Sample {row['sample_id']} has an invalid frame path.")
        candidate = (root / raw["path"]).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise SystemExit(f"Sample {row['sample_id']} frame path escapes --dataset-root.") from error
        if not candidate.is_file():
            raise SystemExit(f"Sample {row['sample_id']} frame is missing: {raw['path']}")
        frames.append({
            "index": index,
            "timestamp_seconds": float(raw.get("timestamp_seconds", index - 1)),
            "timestamp_quality": raw.get("timestamp_quality", "estimated"),
            "path": candidate.relative_to(root).as_posix(),
        })
    return frames


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _failure_reason(result: dict[str, Any]) -> str | None:
    if result.get("status") == "completed":
        return None
    limitations = result.get("limitations")
    if isinstance(limitations, list) and limitations:
        return str(limitations[0])
    return "Model did not complete; no low-risk inference is made."


if __name__ == "__main__":
    main()
