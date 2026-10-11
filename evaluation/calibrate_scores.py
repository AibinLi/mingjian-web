"""Fit a guarded Platt calibrator from an independent calibration manifest.

This script rejects the phase-3 official-test predictions and will only accept
rows explicitly marked ``dataset_role=calibration`` with audited zero overlap.
It consumes saved scores and manifest metadata, never media.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any


EPSILON = 1e-6
PHASE3_OFFICIAL_TEST_PREDICTIONS_SHA256 = "4e7e87b508d7e1a27d8ff56afe8f84b5685a703e9aaf60a9a61fdd3a36adb8eb"
CALIBRATION_ROLE = "calibration"
HOLDOUT_KIND = "group_aware_deterministic_frozen_holdout"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--official-test-list", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-key", required=True)
    parser.add_argument("--model-weight-sha256", required=True)
    parser.add_argument("--calibrator-version", default="phase6-platt-v2")
    parser.add_argument("--split-seed", default="mingjian-phase6-group-holdout-v1")
    parser.add_argument("--validation-fraction", type=float, default=0.20)
    parser.add_argument("--review-threshold", type=float, default=0.5)
    args = parser.parse_args()
    artifact = calibrate(args.predictions, args.manifest, args.official_test_list, args.model_key, args.model_weight_sha256, args.calibrator_version, args.split_seed, args.validation_fraction, args.review_threshold)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True))


def calibrate(predictions: Path, manifest: Path, official_test_list: Path, model_key: str, model_weight_sha256: str, calibrator_version: str, split_seed: str, validation_fraction: float = 0.20, review_threshold: float = 0.5) -> dict[str, Any]:
    if not 0 < validation_fraction < 1 or not 0 <= review_threshold <= 1:
        raise ValueError("validation fraction must be in (0, 1), and review threshold in [0, 1].")
    prediction_hash = _sha256_file(predictions)
    if prediction_hash == PHASE3_OFFICIAL_TEST_PREDICTIONS_SHA256:
        raise ValueError("Phase-3 official-test predictions are forbidden for calibration.")
    manifest_hash = _sha256_file(manifest)
    official_test_list_hash, official_paths = _read_official_test_list(official_test_list)
    manifest_rows = _read_manifest(manifest, official_test_list_hash, official_paths)
    rows = _read_predictions(predictions, manifest_rows, manifest_hash, model_key, model_weight_sha256)
    train, validation = _group_split(rows, split_seed, validation_fraction)
    if not train or not validation or len({label for label, _, _, _ in train}) < 2 or len({label for label, _, _, _ in validation}) < 2:
        raise ValueError("Group-aware frozen split lacks both classes in training or validation.")
    intercept, slope = _fit_platt([(label, score) for label, score, _, _ in train])
    before = _metrics([(label, score) for label, score, _, _ in validation])
    after = _metrics([(label, _platt(score, intercept, slope)) for label, score, _, _ in validation])
    validation_ids = _hash_values(row[2] for row in validation)
    fit_ids = _hash_values(row[2] for row in train)
    validation_groups = {row[3] for row in validation}
    fit_groups = {row[3] for row in train}
    if validation_groups.intersection(fit_groups):
        raise AssertionError("Group-aware split leaked a group across fit and validation.")
    payload: dict[str, Any] = {
        "schema_version": "score-calibrator-v1",
        "calibrator_version": calibrator_version,
        "deployment_status": "candidate",
        "model_key": model_key,
        "model_weight_sha256": model_weight_sha256,
        "method": "platt_logistic",
        "parameters": {"intercept": round(intercept, 12), "slope": round(slope, 12), "input_transform": "logit(clamp(raw_score, 1e-6, 1-1e-6))"},
        "review_threshold": review_threshold,
        "inputs": {"dataset_role": CALIBRATION_ROLE, "prediction_jsonl_sha256": prediction_hash, "manifest_sha256": manifest_hash, "official_test_list_sha256": official_test_list_hash, "model_key": model_key, "model_weight_sha256": model_weight_sha256, "official_test_overlap_count": 0},
        "validation": {"kind": HOLDOUT_KIND, "dataset_role": CALIBRATION_ROLE, "split_seed": split_seed, "validation_fraction": validation_fraction, "scored_rows": len(rows), "fit_rows": len(train), "validation_rows": len(validation), "fit_sample_ids_sha256": fit_ids, "validation_sample_ids_sha256": validation_ids, "fit_group_ids_sha256": _hash_values(fit_groups), "validation_group_ids_sha256": _hash_values(validation_groups), "group_overlap_count": 0, "official_test_overlap_count": 0, "mock_rows_rejected": True},
        "metrics_before": before,
        "metrics_after": after,
        "limitations": ["Metrics are calculated only on the independent calibration manifest's internal group-aware frozen holdout.", "They are calibration diagnostics, not final-test performance or a generalisation claim.", "This artifact calibrates only the stated model key and frozen weight hash; it does not calibrate audio, synchrony, content credibility, or provenance."],
    }
    payload["artifact_sha256"] = _artifact_sha256(payload)
    return payload


def _read_manifest(path: Path, official_test_list_hash: str, official_paths: set[str]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Manifest line {line_number} is invalid JSON.") from error
        required = {"dataset_role", "sample_id", "relative_path", "label", "binary_label_fake", "source_sha256", "official_testing_list_sha256", "official_test_overlap_count", "group_id"}
        if not isinstance(row, dict) or not required.issubset(row):
            raise ValueError(f"Manifest line {line_number} omits calibration provenance fields.")
        sample_id = row["sample_id"]
        if not isinstance(sample_id, str) or not sample_id or sample_id in rows:
            raise ValueError("Calibration manifest sample_id must be unique.")
        if row["dataset_role"] != CALIBRATION_ROLE:
            raise ValueError("Calibration manifest must declare dataset_role=calibration, never test.")
        if row["label"] not in {"real", "fake"} or row["binary_label_fake"] not in {0, 1} or int(row["official_test_overlap_count"]) != 0:
            raise ValueError("Calibration manifest has an invalid label or non-zero official-test overlap.")
        if not all(isinstance(row[field], str) and row[field] for field in ("relative_path", "source_sha256", "official_testing_list_sha256", "group_id")):
            raise ValueError("Calibration manifest omits immutable provenance values.")
        if row["official_testing_list_sha256"] != official_test_list_hash:
            raise ValueError("Calibration manifest official-test-list hash does not match the supplied list.")
        if row["relative_path"].replace("\\", "/") in official_paths:
            raise ValueError("Calibration manifest contains an actual official-test path despite its declared overlap count.")
        rows[sample_id] = row
    if not rows:
        raise ValueError("Calibration manifest contains no samples.")
    return rows


def _read_predictions(path: Path, manifest: dict[str, dict[str, Any]], manifest_hash: str, model_key: str, model_weight_sha256: str) -> list[tuple[int, float, str, str]]:
    values: list[tuple[int, float, str, str]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Prediction line {line_number} is invalid JSON.") from error
        if row.get("mock") is True or (isinstance(row.get("model_result"), dict) and row["model_result"].get("mock") is True):
            raise ValueError(f"Prediction line {line_number} is mock and cannot calibrate a model.")
        sample_id = row.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id or sample_id in seen:
            raise ValueError("Calibration prediction sample_id must be unique.")
        seen.add(sample_id)
        required = {"dataset_role", "official_test_overlap_count", "manifest_sha256", "model_key", "model_weight_sha256", "source_sha256", "label", "risk_score"}
        if not required.issubset(row) or row.get("dataset_role") != CALIBRATION_ROLE or row.get("dataset_role") == "test":
            raise ValueError("Prediction rows must explicitly declare dataset_role=calibration.")
        if int(row.get("official_test_overlap_count", -1)) != 0:
            raise ValueError("Calibration prediction has non-zero official-test overlap.")
        if row["manifest_sha256"] != manifest_hash or row["model_key"] != model_key or row["model_weight_sha256"] != model_weight_sha256:
            raise ValueError("Prediction provenance does not match the calibration manifest or requested model.")
        manifest_row = manifest.get(sample_id)
        if not manifest_row or any(row.get(field) != manifest_row.get(field) for field in ("source_sha256", "dataset_role", "official_test_overlap_count")) or row["label"] != manifest_row["binary_label_fake"]:
            raise ValueError("Prediction row does not match the frozen calibration manifest.")
        score = row["risk_score"]
        if row.get("status") != "completed" or row["label"] not in (0, 1) or isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)) or not 0 <= float(score) <= 1:
            continue
        values.append((int(row["label"]), float(score), sample_id, manifest_row["group_id"]))
    if set(manifest) != seen:
        raise ValueError("Prediction and manifest sample_id sets must match exactly.")
    if not values:
        raise ValueError("No completed, labelled, non-mock calibration scores are available.")
    return values


def _group_split(rows: list[tuple[int, float, str, str]], seed: str, fraction: float) -> tuple[list[tuple[int, float, str, str]], list[tuple[int, float, str, str]]]:
    groups: dict[str, list[tuple[int, float, str, str]]] = {}
    for row in rows:
        groups.setdefault(row[3], []).append(row)
    if len(groups) < 3:
        raise ValueError("Calibration needs multiple visible-identity groups for a global holdout.")
    ordered = sorted(groups.values(), key=lambda group: hashlib.sha256(f"{seed}|{group[0][3]}".encode("utf-8")).hexdigest())
    cutoff = int(fraction * (2**256 - 1))
    selected = [group for group in ordered if int(hashlib.sha256(f"{seed}|{group[0][3]}".encode("utf-8")).hexdigest(), 16) <= cutoff]
    if not selected or len(selected) == len(ordered):
        raise ValueError("Global visible-identity hash split produced an empty side.")
    validation = [row for group in selected for row in group]
    fit = [row for group in ordered if group not in selected for row in group]
    if {row[0] for row in fit} != {0, 1} or {row[0] for row in validation} != {0, 1}:
        raise ValueError("Global visible-identity split does not contain both classes on both sides.")
    return fit, validation


def _read_official_test_list(path: Path) -> tuple[str, set[str]]:
    if not path.is_file():
        raise ValueError("Official test list is missing.")
    paths: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        pieces = line.split(maxsplit=1)
        if len(pieces) != 2:
            raise ValueError(f"Official test list line {line_number} is malformed.")
        paths.add(pieces[1].replace("\\", "/"))
    return _sha256_file(path), paths


def _fit_platt(values: list[tuple[int, float]]) -> tuple[float, float]:
    intercept, slope, regularisation = 0.0, 0.0, 1e-3
    transformed = [(label, _logit(score)) for label, score in values]
    for _ in range(100):
        probabilities = [_sigmoid(intercept + slope * value) for _, value in transformed]
        gradient_i = sum(probability - label for probability, (label, _) in zip(probabilities, transformed)) + regularisation * intercept
        gradient_s = sum((probability - label) * value for probability, (label, value) in zip(probabilities, transformed)) + regularisation * slope
        weight = [max(EPSILON, probability * (1 - probability)) for probability in probabilities]
        h_ii, h_is = sum(weight) + regularisation, sum(item * value for item, (_, value) in zip(weight, transformed))
        h_ss = sum(item * value * value for item, (_, value) in zip(weight, transformed)) + regularisation
        determinant = h_ii * h_ss - h_is * h_is
        if determinant <= EPSILON:
            break
        delta_i, delta_s = (h_ss * gradient_i - h_is * gradient_s) / determinant, (-h_is * gradient_i + h_ii * gradient_s) / determinant
        intercept, slope = max(-30.0, min(30.0, intercept - delta_i)), max(-30.0, min(30.0, slope - delta_s))
        if max(abs(delta_i), abs(delta_s)) < 1e-10:
            break
    return intercept, slope


def _metrics(values: list[tuple[int, float]]) -> dict[str, float | None]:
    brier = sum((score - label) ** 2 for label, score in values) / len(values)
    log_loss = -sum(label * math.log(max(EPSILON, score)) + (1 - label) * math.log(max(EPSILON, 1 - score)) for label, score in values) / len(values)
    return {"brier_score": round(brier, 6), "log_loss": round(log_loss, 6), "roc_auc": _roc_auc(values)}


def _roc_auc(values: list[tuple[int, float]]) -> float | None:
    positives, negatives = sum(label for label, _ in values), sum(1 - label for label, _ in values)
    if not positives or not negatives:
        return None
    ordered, ranks, cursor = sorted(enumerate(values), key=lambda item: item[1][1]), [0.0] * len(values), 0
    while cursor < len(ordered):
        end = cursor + 1
        while end < len(ordered) and ordered[end][1][1] == ordered[cursor][1][1]:
            end += 1
        for original, _ in ordered[cursor:end]:
            ranks[original] = (cursor + 1 + end) / 2
        cursor = end
    rank_sum = sum(ranks[index] for index, (label, _) in enumerate(values) if label == 1)
    return round((rank_sum - positives * (positives + 1) / 2) / (positives * negatives), 6)


def _logit(score: float) -> float:
    value = max(EPSILON, min(1 - EPSILON, score))
    return math.log(value / (1 - value))


def _platt(score: float, intercept: float, slope: float) -> float:
    return _sigmoid(intercept + slope * _logit(score))


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, value))))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_values(values: Any) -> str:
    return hashlib.sha256("\n".join(sorted(values)).encode("utf-8")).hexdigest()


def _artifact_sha256(record: dict[str, Any]) -> str:
    clean = {key: value for key, value in record.items() if key != "artifact_sha256"}
    return hashlib.sha256(json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


if __name__ == "__main__":
    main()
