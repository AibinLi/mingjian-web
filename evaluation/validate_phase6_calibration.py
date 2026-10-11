"""Recompute phase-6 calibration evidence integrity without changing artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.visual_detector import WEIGHT_SHA256
from evaluation.calibrate_scores import _group_split, _read_manifest, _read_official_test_list, _read_predictions, _sha256_file
from evaluation.celeb_df_v2 import sha256_file


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--official-test-list", type=Path, required=True)
    parser.add_argument("--split-seed", required=True)
    parser.add_argument("--validation-fraction", type=float, default=0.20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate(args.dataset_root, args.manifest, args.predictions, args.official_test_list, args.split_seed, args.validation_fraction)
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)


def validate(dataset_root: Path, manifest_path: Path, predictions_path: Path, official_path: Path, split_seed: str, validation_fraction: float) -> dict:
    official_hash, official_paths = _read_official_test_list(official_path)
    manifest_hash = _sha256_file(manifest_path)
    manifest = _read_manifest(manifest_path, official_hash, official_paths)
    source_mismatches: list[str] = []
    for sample_id, row in manifest.items():
        source = (dataset_root / Path(row["relative_path"])).resolve()
        try:
            source.relative_to(dataset_root.resolve())
        except ValueError:
            source_mismatches.append(sample_id)
            continue
        if not source.is_file() or sha256_file(source) != row["source_sha256"]:
            source_mismatches.append(sample_id)
    predictions = _read_predictions(predictions_path, manifest, manifest_hash, "visual.meso4.fake_score", WEIGHT_SHA256)
    fit, validation = _group_split(predictions, split_seed, validation_fraction)
    fit_groups, validation_groups = {row[3] for row in fit}, {row[3] for row in validation}
    return {"schema_version": "phase6-calibration-evidence-validation-v1", "manifest_sha256": manifest_hash, "predictions_sha256": _sha256_file(predictions_path), "official_test_list_sha256": official_hash, "sample_count": len(manifest), "real_count": sum(row["binary_label_fake"] == 0 for row in manifest.values()), "fake_count": sum(row["binary_label_fake"] == 1 for row in manifest.values()), "unique_sample_ids": len(manifest), "official_test_overlap_count": sum(row["relative_path"].replace("\\", "/") in official_paths for row in manifest.values()), "source_hash_match_count": len(manifest) - len(source_mismatches), "source_hash_mismatch_sample_ids": source_mismatches, "prediction_completed_count": len(predictions), "prediction_mock_count": 0, "fit_rows": len(fit), "validation_rows": len(validation), "fit_visible_identity_count": len(fit_groups), "validation_visible_identity_count": len(validation_groups), "visible_identity_overlap_count": len(fit_groups.intersection(validation_groups)), "valid": not source_mismatches and len(manifest) == len(predictions) and not fit_groups.intersection(validation_groups)}


if __name__ == "__main__":
    main()
