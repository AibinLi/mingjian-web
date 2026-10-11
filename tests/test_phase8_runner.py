from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation.run_phase8 import _eer, run_phase8


WEIGHT_SHA = "a362ebbef2658daa551d808fbfccd57f0b5fd34fda46a05996e807cccddff4e0"


def test_phase8_runner_generates_metrics_plots_and_failure_cases(tmp_path: Path) -> None:
    predictions = tmp_path / "visual.predictions.jsonl"
    rows = [
        {"sample_id": "real-fp", "status": "completed", "label": 0, "risk_score": 0.9, "quality_status": "available", "runtime_seconds": 0.10},
        {"sample_id": "fake-fn", "status": "completed", "label": 1, "risk_score": 0.2, "quality_status": "available", "runtime_seconds": 0.20},
        {"sample_id": "real-tn", "status": "completed", "label": 0, "risk_score": 0.1, "quality_status": "available", "runtime_seconds": 0.30},
        {"sample_id": "fake-tp", "status": "completed", "label": 1, "risk_score": 0.8, "quality_status": "available", "runtime_seconds": 0.40},
        {"sample_id": "no-evidence", "status": "not_applicable", "label": 1, "risk_score": None, "quality_status": "not_applicable", "failure_reason": "No authorised audio stream.", "runtime_seconds": 0.01},
        {"sample_id": "decode-error", "status": "decode_failed", "label": 0, "risk_score": None, "quality_status": "unavailable", "failure_reason": "FFmpeg failed.", "runtime_seconds": 0.02},
    ]
    predictions.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    suite = tmp_path / "phase8_suite.json"
    suite.write_text(json.dumps({
        "schema_version": "phase8-suite-manifest-v1",
        "random_seed": "mingjian-phase8-fixture-seed",
        "experiments": [
            {"name": "visual-main", "axis": "visual", "kind": "prediction_jsonl", "predictions": str(predictions), "decision_threshold": 0.5},
            {"name": "audio-main", "axis": "audio", "kind": "not_applicable", "reason": "No authorised labelled audio spoofing manifest is frozen."},
            {"name": "facts-main", "axis": "fact_check", "kind": "not_applicable", "reason": "No authorised labelled fact-check manifest is frozen."},
            {"name": "fusion-main", "axis": "fusion", "kind": "not_applicable", "reason": "Fusion has no independent frozen labelled manifest."},
            {"name": "ablation-main", "axis": "ablation", "kind": "not_applicable", "reason": "Ablation requires frozen component manifests."},
            {"name": "robustness-main", "axis": "robustness", "kind": "not_applicable", "reason": "Robustness perturbations are not authorised for this fixture."},
        ],
    }), encoding="utf-8")

    summary = run_phase8(suite, tmp_path / "phase8-output", "mingjian-phase8-fixture-seed")

    metrics = json.loads((tmp_path / "phase8-output" / "metrics" / "visual-main.metrics.json").read_text(encoding="utf-8"))
    assert summary["status"] == "completed"
    assert metrics["metrics"]["f1"] == 0.5
    assert metrics["metrics"]["false_positive_rate"] == 0.5
    assert metrics["metrics"]["false_negative_rate"] == 0.5
    assert metrics["metrics"]["roc_auc"] == 0.5
    assert metrics["calibration"]["brier_score"] == 0.375
    assert metrics["latency"]["sample_count"] == 6
    assert (tmp_path / "phase8-output" / "metrics_table.csv").is_file()
    assert (tmp_path / "phase8-output" / "plots" / "visual-main.roc.svg").is_file()
    failure_cases = (tmp_path / "phase8-output" / "failure_cases.csv").read_text(encoding="utf-8")
    assert "false_positive" in failure_cases
    assert "false_negative" in failure_cases
    assert "unable_to_determine" in failure_cases
    assert "system_failure" in failure_cases
    frozen = json.loads((tmp_path / "phase8-output" / "frozen_run.json").read_text(encoding="utf-8"))
    assert frozen["random_seed"] == "mingjian-phase8-fixture-seed"
    assert frozen["suite_manifest_sha256"]


def test_phase8_suite_must_explicitly_cover_all_required_experiments(tmp_path: Path) -> None:
    suite = tmp_path / "phase8_suite.json"
    suite.write_text(json.dumps({
        "schema_version": "phase8-suite-manifest-v1",
        "random_seed": "seed",
        "experiments": [
            {"name": "visual-main", "axis": "visual", "kind": "not_applicable", "reason": "fixture"},
        ],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="Phase 8 experiments"):
        run_phase8(suite, tmp_path / "phase8-output")


def test_phase8_strict_validation_accepts_matching_predictions_and_manifest(tmp_path: Path) -> None:
    suite = _strict_suite(tmp_path)

    run_phase8(suite, tmp_path / "phase8-output")

    metrics = json.loads((tmp_path / "phase8-output" / "metrics" / "visual-main.metrics.json").read_text(encoding="utf-8"))
    assert metrics["input_validation"]["status"] == "passed"
    assert metrics["input_validation"]["actual_hashes"]["predictions_sha256"] == metrics["prediction_jsonl_sha256"]
    assert metrics["input_validation"]["actual_model_weight_sha256s"] == [WEIGHT_SHA]
    assert metrics["metrics"]["eer_method"] == "linear_interpolation_at_fpr_fnr_crossing"
    assert metrics["latency"]["latency_scope"] == "visual_detector_detect_only"
    assert metrics["end_to_end_latency"]["status"] == "not_applicable"


def test_phase8_strict_validation_rejects_config_hash_tampering(tmp_path: Path) -> None:
    suite = _strict_suite(tmp_path)
    suite_data = json.loads(suite.read_text(encoding="utf-8"))
    config = Path(suite_data["experiments"][0]["config"])
    config.write_text(json.dumps({"fixture": False}), encoding="utf-8")

    with pytest.raises(ValueError, match="config SHA-256 mismatch"):
        run_phase8(suite, tmp_path / "phase8-output")


@pytest.mark.parametrize(("mutation", "message"), [
    (lambda _manifest, _predictions, suite: suite["experiments"][0].update({"expected_predictions_sha256": "0" * 64}), "predictions SHA-256 mismatch"),
    (lambda manifest, _predictions, _suite: _tamper_file(manifest, lambda row: row.__setitem__("source_sha256", "3" * 64)), "manifest_sha256 mismatch"),
    (lambda _manifest, predictions, _suite: _append_prediction(predictions, "real-1"), "duplicate sample_id"),
    (lambda manifest, _predictions, _suite: _remove_manifest_row(manifest, "fake-1"), "sample_id set does not match manifest"),
    (lambda manifest, _predictions, _suite: _change_manifest_label(manifest, "fake-1", 0), "label mismatch"),
    (lambda _manifest, predictions, _suite: _change_prediction_weight(predictions, "fake-1", "b" * 64), "model weight SHA-256 mismatch"),
    (lambda _manifest, predictions, _suite: _change_prediction_score(predictions, "fake-1", float("inf")), "finite \\[0,1\\] score"),
])
def test_phase8_strict_validation_rejects_tampering(tmp_path: Path, mutation, message: str) -> None:
    suite = _strict_suite(tmp_path)
    suite_data = json.loads(suite.read_text(encoding="utf-8"))
    manifest = Path(suite_data["experiments"][0]["manifest"])
    predictions = Path(suite_data["experiments"][0]["predictions"])
    mutation(manifest, predictions, suite_data)
    if message in {"sample_id set does not match manifest", "label mismatch"}:
        _sync_prediction_manifest_hash(predictions, _sha256(manifest))
    if "expected_predictions_sha256" not in suite_data["experiments"][0] or suite_data["experiments"][0]["expected_predictions_sha256"] != "0" * 64:
        _refresh_hashes(suite_data, manifest, predictions, Path(suite_data["experiments"][0]["config"]))
    suite.write_text(json.dumps(suite_data), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        run_phase8(suite, tmp_path / "phase8-output")


def test_phase8_eer_uses_linear_interpolation_at_crossing() -> None:
    values: list[tuple[int, float]] = []
    values.extend((1, score) for score in (0.99, 0.98, 0.97, 0.96, 0.95, 0.94, 0.93))
    values.extend([(0, 0.92), (0, 0.91)])
    values.extend((1, score) for score in (0.30, 0.20, 0.10))
    values.extend((0, score) for score in (0.09, 0.08, 0.07))

    assert _eer(values) == 0.3


def _strict_suite(tmp_path: Path) -> Path:
    manifest = tmp_path / "manifest.jsonl"
    predictions = tmp_path / "predictions.jsonl"
    config = tmp_path / "config.json"
    manifest_rows = [
        {"sample_id": "real-1", "binary_label_fake": 0, "source_sha256": "1" * 64},
        {"sample_id": "fake-1", "binary_label_fake": 1, "source_sha256": "2" * 64},
    ]
    prediction_rows = [
        _prediction("real-1", 0, "1" * 64, 0.2, None),
        _prediction("fake-1", 1, "2" * 64, 0.8, None),
    ]
    manifest.write_text("".join(json.dumps(row) + "\n" for row in manifest_rows), encoding="utf-8")
    manifest_hash = _sha256(manifest)
    for row in prediction_rows:
        row["manifest_sha256"] = manifest_hash
    predictions.write_text("".join(json.dumps(row) + "\n" for row in prediction_rows), encoding="utf-8")
    config.write_text(json.dumps({"fixture": True}), encoding="utf-8")
    suite_data = {
        "schema_version": "phase8-suite-manifest-v1",
        "random_seed": "strict-seed",
        "experiments": [
            {
                "name": "visual-main",
                "axis": "visual",
                "kind": "prediction_jsonl",
                "predictions": str(predictions),
                "manifest": str(manifest),
                "config": str(config),
                "expected_model_weight_sha256": WEIGHT_SHA,
                "expected_total_rows": 2,
                "expected_label_counts": {"0": 1, "1": 1},
                "expected_status_counts": {"completed": 2},
                "decision_threshold": 0.5,
            },
            {"name": "audio-main", "axis": "audio", "kind": "not_applicable", "reason": "fixture"},
            {"name": "facts-main", "axis": "fact_check", "kind": "not_applicable", "reason": "fixture"},
            {"name": "fusion-main", "axis": "fusion", "kind": "not_applicable", "reason": "fixture"},
            {"name": "ablation-main", "axis": "ablation", "kind": "not_applicable", "reason": "fixture"},
            {"name": "robustness-main", "axis": "robustness", "kind": "not_applicable", "reason": "fixture"},
        ],
    }
    _refresh_hashes(suite_data, manifest, predictions, config)
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps(suite_data), encoding="utf-8")
    return suite


def _prediction(sample_id: str, label: int, source_sha: str, score: float, manifest_sha: str | None) -> dict:
    return {
        "sample_id": sample_id,
        "status": "completed",
        "label": label,
        "risk_score": score,
        "source_sha256": source_sha,
        "manifest_sha256": manifest_sha,
        "model_result": {"mock": False, "elapsed_seconds": 0.01, "model": {"weight_sha256": WEIGHT_SHA}},
    }


def _refresh_hashes(suite_data: dict, manifest: Path, predictions: Path, config: Path) -> None:
    experiment = suite_data["experiments"][0]
    experiment["expected_manifest_sha256"] = _sha256(manifest)
    experiment["expected_predictions_sha256"] = _sha256(predictions)
    experiment["expected_config_sha256"] = _sha256(config)


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _append_prediction(path: Path, sample_id: str) -> None:
    rows = _jsonl(path)
    rows.append(dict(rows[0], sample_id=sample_id))
    _write_jsonl(path, rows)


def _remove_manifest_row(path: Path, sample_id: str) -> None:
    _write_jsonl(path, [row for row in _jsonl(path) if row["sample_id"] != sample_id])


def _change_manifest_label(path: Path, sample_id: str, label: int) -> None:
    rows = _jsonl(path)
    for row in rows:
        if row["sample_id"] == sample_id:
            row["binary_label_fake"] = label
    _write_jsonl(path, rows)


def _change_prediction_weight(path: Path, sample_id: str, weight: str) -> None:
    rows = _jsonl(path)
    for row in rows:
        if row["sample_id"] == sample_id:
            row["model_result"]["model"]["weight_sha256"] = weight
    _write_jsonl(path, rows)


def _change_prediction_score(path: Path, sample_id: str, score: float) -> None:
    rows = _jsonl(path)
    for row in rows:
        if row["sample_id"] == sample_id:
            row["risk_score"] = score
    _write_jsonl(path, rows)


def _tamper_file(path: Path, mutator) -> None:
    rows = _jsonl(path)
    for row in rows:
        mutator(row)
    _write_jsonl(path, rows)


def _sync_prediction_manifest_hash(path: Path, manifest_hash: str) -> None:
    rows = _jsonl(path)
    for row in rows:
        row["manifest_sha256"] = manifest_hash
    _write_jsonl(path, rows)
