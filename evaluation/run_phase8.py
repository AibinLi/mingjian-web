"""Phase 8 reproducible evaluation and error-analysis runner.

The runner is deliberately a reporting/evaluation harness, not a product
feature.  It never tunes thresholds from test outcomes.  Each experiment must
come from a frozen suite manifest as either:

* ``prediction_jsonl``: an existing raw prediction JSONL, optionally generated
  first by an explicit command.
* ``not_applicable``: an auditable reason that no authorised labelled manifest
  exists for that experiment.

Rows in raw prediction JSONL are expected to preserve at least ``sample_id``,
``status``, ``label`` when labels exist, ``risk_score`` when a scalar score
exists, ``quality_status``, ``failure_reason`` and ``runtime_seconds``.  Missing
fields do not get invented; the corresponding metrics are marked
not-applicable with an explanation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape


REQUIRED_EXPERIMENTS = {"visual", "audio", "fact_check", "fusion", "ablation", "robustness"}
EXPECTED_HASH_FIELDS = {
    "predictions": "expected_predictions_sha256",
    "manifest": "expected_manifest_sha256",
    "config": "expected_config_sha256",
}
DEFAULT_THRESHOLD = 0.5
LATENCY_TABLE_LABEL = "Meso4 detector detect() p95 elapsed time (s)"
SYSTEM_FAILURE_STATUSES = {
    "cancelled",
    "decode_failed",
    "ffmpeg_timeout",
    "ffmpeg_unavailable",
    "frame_output_exists",
    "source_hash_mismatch",
    "source_missing",
    "source_path_invalid",
    "timed_out",
}
UNABLE_STATUSES = {"degraded", "insufficient_evidence", "no_evidence", "not_applicable", "unable_to_determine", "unavailable"}
ENV_ALLOWLIST = {
    "CUDA_VISIBLE_DEVICES",
    "MINGJIAN_AUDIO_FAKE_LABEL",
    "MINGJIAN_AUDIO_MODEL_DIR",
    "MINGJIAN_CALIBRATOR_DIR",
    "MINGJIAN_OPENCV_DIR",
    "MINGJIAN_REAL_MEDIA_SMOKE",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-manifest", type=Path, required=True, help="Frozen Phase 8 suite manifest JSON.")
    parser.add_argument("--output-dir", type=Path, required=True, help="Fresh directory for Phase 8 artifacts.")
    parser.add_argument("--seed", default=None, help="Optional override only if it matches the suite manifest seed.")
    parser.add_argument("--force", action="store_true", help="Allow writing into an existing output directory.")
    args = parser.parse_args()

    summary = run_phase8(args.suite_manifest, args.output_dir, args.seed, force=args.force)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


def run_phase8(suite_manifest: Path, output_dir: Path, seed_override: str | None = None, *, force: bool = False) -> dict[str, Any]:
    started = time.monotonic()
    suite_manifest = suite_manifest.resolve()
    output_dir = output_dir.resolve()
    if not suite_manifest.is_file():
        raise ValueError("Suite manifest does not exist.")
    if output_dir.exists() and any(output_dir.iterdir()) and not force:
        raise ValueError("Output directory already contains files; use a fresh run directory or --force.")
    output_dir.mkdir(parents=True, exist_ok=True)
    suite = json.loads(suite_manifest.read_text(encoding="utf-8"))
    _validate_suite(suite, seed_override)
    raw_dir = output_dir / "raw_predictions"
    metrics_dir = output_dir / "metrics"
    plots_dir = output_dir / "plots"
    for directory in (raw_dir, metrics_dir, plots_dir):
        directory.mkdir(parents=True, exist_ok=True)

    commands: list[dict[str, Any]] = []
    metric_records: list[dict[str, Any]] = []
    failure_rows: list[dict[str, Any]] = []
    experiment_summaries: list[dict[str, Any]] = []

    for experiment in suite["experiments"]:
        name = _safe_name(experiment["name"])
        kind = experiment["kind"]
        experiment_started = time.monotonic()
        if kind == "not_applicable":
            reason = str(experiment["reason"])
            metrics = _not_applicable_metrics(experiment, reason)
            raw_path = raw_dir / f"{name}.jsonl"
            raw_path.write_text("", encoding="utf-8")
            command_record = {"experiment": name, "kind": kind, "status": "not_applicable", "reason": reason}
        else:
            command_record = _maybe_run_command(experiment, output_dir)
            predictions = _resolve_path(experiment["predictions"], suite_manifest.parent)
            if not predictions.is_file():
                raise ValueError(f"Experiment {name} predictions file is missing: {predictions}")
            input_validation = validate_prediction_inputs(experiment, suite_manifest.parent)
            raw_path = raw_dir / f"{name}.jsonl"
            _copy_text(predictions, raw_path)
            metrics = compute_experiment_metrics(raw_path, experiment)
            metrics["input_validation"] = input_validation
            failure_rows.extend(_failure_cases(raw_path, experiment))

        metrics_path = metrics_dir / f"{name}.metrics.json"
        metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        plot_path = plots_dir / f"{name}.roc.svg"
        points_path = plots_dir / f"{name}.roc_points.csv"
        _write_roc_outputs(metrics, plot_path, points_path)
        command_record["elapsed_seconds"] = round(time.monotonic() - experiment_started, 6)
        commands.append(command_record)
        metric_records.append(metrics)
        experiment_summaries.append({
            "name": name,
            "axis": experiment["axis"],
            "kind": kind,
            "input_validation": input_validation if kind != "not_applicable" else {"status": "not_applicable"},
            "input_artifacts": _input_artifacts(experiment, suite_manifest.parent),
            "raw_predictions_path": str(raw_path),
            "raw_predictions_sha256": _sha256_file(raw_path),
            "metrics_path": str(metrics_path),
            "metrics_sha256": _sha256_file(metrics_path),
            "roc_plot_path": str(plot_path),
            "roc_points_path": str(points_path),
        })

    failure_path = output_dir / "failure_cases.csv"
    _write_failure_cases(failure_path, failure_rows)
    table_csv = output_dir / "metrics_table.csv"
    table_md = output_dir / "metrics_table.md"
    _write_metric_tables(table_csv, table_md, metric_records)
    frozen_path = output_dir / "frozen_run.json"
    commands_path = output_dir / "commands.json"
    frozen = {
        "schema_version": "phase8-frozen-run-v1",
        "suite_manifest_path": str(suite_manifest),
        "suite_manifest_sha256": _sha256_file(suite_manifest),
        "random_seed": suite["random_seed"],
        "command": [sys.executable, *sys.argv],
        "cwd": str(Path.cwd().resolve()),
        "git": _git_state(),
        "environment": _environment_record(),
        "experiments": experiment_summaries,
        "outputs": {
            "commands": str(commands_path),
            "failure_cases": str(failure_path),
            "metrics_table_csv": str(table_csv),
            "metrics_table_md": str(table_md),
        },
        "guardrails": [
            "No threshold, calibrator, or parameter is selected from these test outcomes.",
            "Metrics are recomputed only from retained raw prediction JSONL.",
            "Not-applicable metrics are preserved with reasons instead of being filled by hand.",
        ],
    }
    frozen_path.write_text(json.dumps(frozen, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    commands_path.write_text(json.dumps({"schema_version": "phase8-command-log-v1", "commands": commands}, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = {
        "schema_version": "phase8-summary-v1",
        "status": "completed",
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "frozen_run_path": str(frozen_path),
        "frozen_run_sha256": _sha256_file(frozen_path),
        "commands_path": str(commands_path),
        "commands_sha256": _sha256_file(commands_path),
        "metrics_table_csv": str(table_csv),
        "metrics_table_md": str(table_md),
        "failure_cases_csv": str(failure_path),
        "experiments": experiment_summaries,
    }
    summary_path = output_dir / "phase8_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def compute_experiment_metrics(predictions: Path, experiment: dict[str, Any]) -> dict[str, Any]:
    rows = _read_jsonl(predictions)
    threshold = float(experiment.get("decision_threshold", DEFAULT_THRESHOLD))
    score_field = str(experiment.get("score_field", "risk_score"))
    status_counts = Counter(str(row.get("status", "missing_status")) for row in rows)
    scored = [_score_tuple(row, score_field) for row in rows]
    completed = [item for item in scored if item is not None]
    confusion = _confusion(completed, threshold)
    metric_values, reasons = _classification_metrics(completed, threshold, confusion)
    calibration, calibration_reasons = _calibration_metrics(completed)
    latency, latency_reasons = _latency_metrics(rows)
    reasons.update(calibration_reasons)
    reasons.update(latency_reasons)
    model_hashes = _model_hashes(rows)
    return {
        "schema_version": "phase8-experiment-metrics-v1",
        "experiment": experiment["name"],
        "axis": experiment["axis"],
        "kind": experiment["kind"],
        "threshold": threshold,
        "score_field": score_field,
        "prediction_jsonl_sha256": _sha256_file(predictions),
        "total_rows": len(rows),
        "scored_rows": len(completed),
        "skipped_rows": len(rows) - len(completed),
        "status_counts": dict(sorted(status_counts.items())),
        "metrics": metric_values,
        "calibration": calibration,
        "latency": latency,
        "end_to_end_latency": {
            "status": "not_applicable",
            "reason": "Raw predictions retain visual detector detect() elapsed time only; per-sample end-to-end media processing, queue, and Web timings were not recorded.",
        },
        "confusion_matrix": confusion,
        "not_applicable": reasons,
        "model_hashes": model_hashes,
        "limitations": [
            "The decision threshold is frozen in the suite manifest and is not selected from these results.",
            "Scores are interpreted only as the model-domain score recorded in raw prediction JSONL.",
        ],
        "roc_curve": _roc_curve(completed),
    }


def _classification_metrics(values: list[tuple[int, float, dict[str, Any]]], threshold: float, confusion: dict[str, int | None]) -> tuple[dict[str, Any], dict[str, str]]:
    reasons: dict[str, str] = {}
    if not values:
        reason = "No completed labelled rows with a finite score in [0, 1]."
        return {
            "accuracy": None, "precision": None, "recall": None, "f1": None, "roc_auc": None,
            "eer": None, "eer_method": "linear_interpolation_at_fpr_fnr_crossing", "false_positive_rate": None, "false_negative_rate": None,
        }, {key: reason for key in ("accuracy", "precision", "recall", "f1", "roc_auc", "eer", "false_positive_rate", "false_negative_rate")}
    tp, tn, fp, fn = (int(confusion[key]) for key in ("true_positive", "true_negative", "false_positive", "false_negative"))
    positives = tp + fn
    negatives = tn + fp
    precision = _divide(tp, tp + fp)
    recall = _divide(tp, positives)
    f1 = _divide(2 * precision * recall, precision + recall) if precision is not None and recall is not None else None
    auc = _roc_auc([(label, score) for label, score, _ in values])
    eer = _eer([(label, score) for label, score, _ in values])
    if positives == 0 or negatives == 0:
        reasons["roc_auc"] = "ROC-AUC requires at least one real and one fake completed scored sample."
        reasons["eer"] = "EER requires at least one real and one fake completed scored sample."
    return {
        "accuracy": _divide(tp + tn, len(values)),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "roc_auc": auc,
        "eer": eer,
        "eer_method": "linear_interpolation_at_fpr_fnr_crossing",
        "false_positive_rate": _divide(fp, negatives),
        "false_negative_rate": _divide(fn, positives),
    }, reasons


def _calibration_metrics(values: list[tuple[int, float, dict[str, Any]]]) -> tuple[dict[str, Any], dict[str, str]]:
    if not values:
        reason = "Calibration diagnostics require completed labelled rows with finite scores in [0, 1]."
        return {"brier_score": None, "log_loss": None, "ece_10_bin": None}, {"brier_score": reason, "log_loss": reason, "ece_10_bin": reason}
    pairs = [(label, max(1e-12, min(1 - 1e-12, score))) for label, score, _ in values]
    brier = sum((score - label) ** 2 for label, score in pairs) / len(pairs)
    log_loss = -sum(label * math.log(score) + (1 - label) * math.log(1 - score) for label, score in pairs) / len(pairs)
    return {
        "brier_score": round(brier, 6),
        "log_loss": round(log_loss, 6),
        "ece_10_bin": _ece(pairs, 10),
        "score_semantics": "Raw score calibration diagnostic only; this does not activate a calibrator.",
    }, {}


def _latency_metrics(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, str]]:
    values = [_runtime_seconds(row) for row in rows]
    values = [value for value in values if value is not None and value >= 0]
    if not values:
        reason = "No per-sample runtime_seconds values were retained in raw predictions."
        return {
            "latency_scope": "visual_detector_detect_only",
            "latency_source": "model_result.elapsed_seconds",
            "includes_ffmpeg": False,
            "includes_frame_extraction": False,
            "includes_queue_or_web": False,
            "mean_seconds": None,
            "p50_seconds": None,
            "p95_seconds": None,
            "max_seconds": None,
        }, {"latency": reason}
    ordered = sorted(values)
    return {
        "latency_scope": "visual_detector_detect_only",
        "latency_source": "model_result.elapsed_seconds",
        "includes_ffmpeg": False,
        "includes_frame_extraction": False,
        "includes_queue_or_web": False,
        "mean_seconds": round(sum(ordered) / len(ordered), 6),
        "p50_seconds": round(_percentile(ordered, 50), 6),
        "p95_seconds": round(_percentile(ordered, 95), 6),
        "max_seconds": round(max(ordered), 6),
        "sample_count": len(ordered),
    }, {}


def _failure_cases(predictions: Path, experiment: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _read_jsonl(predictions)
    threshold = float(experiment.get("decision_threshold", DEFAULT_THRESHOLD))
    score_field = str(experiment.get("score_field", "risk_score"))
    failures: list[dict[str, Any]] = []
    for row in rows:
        label = row.get("label")
        score = _number(row.get(score_field))
        status = str(row.get("status", "missing_status"))
        category = None
        predicted_label = None
        if status == "completed" and label in (0, 1) and score is not None:
            predicted_label = 1 if score >= threshold else 0
            if label == 0 and predicted_label == 1:
                category = "false_positive"
            elif label == 1 and predicted_label == 0:
                category = "false_negative"
        elif bool(row.get("system_failure")) or status in SYSTEM_FAILURE_STATUSES:
            category = "system_failure"
        elif status in UNABLE_STATUSES or score is None:
            category = "unable_to_determine"
        if category:
            failures.append({
                "experiment": experiment["name"],
                "axis": experiment["axis"],
                "sample_id": row.get("sample_id", ""),
                "category": category,
                "label": label if label in (0, 1) else "",
                "predicted_label": "" if predicted_label is None else predicted_label,
                "risk_score": "" if score is None else score,
                "threshold": threshold,
                "status": status,
                "quality_status": row.get("quality_status") or ((row.get("model_result") or {}).get("input_quality") or {}).get("status") or "",
                "failure_reason": row.get("failure_reason") or ((row.get("failure") or {}).get("reason")) or "",
                "runtime_seconds": row.get("runtime_seconds", _runtime_seconds(row) or ""),
                "relative_path": row.get("relative_path", ""),
            })
    return failures


def _validate_suite(suite: dict[str, Any], seed_override: str | None) -> None:
    if suite.get("schema_version") != "phase8-suite-manifest-v1":
        raise ValueError("Suite manifest must use schema_version=phase8-suite-manifest-v1.")
    if not isinstance(suite.get("random_seed"), str) or not suite["random_seed"]:
        raise ValueError("Suite manifest must freeze a non-empty random_seed.")
    if seed_override is not None and seed_override != suite["random_seed"]:
        raise ValueError("Seed override must match the frozen suite manifest seed; no ad-hoc reseeding is allowed.")
    experiments = suite.get("experiments")
    if not isinstance(experiments, list) or not experiments:
        raise ValueError("Suite manifest must contain experiments.")
    axes = set()
    names = set()
    for experiment in experiments:
        if not isinstance(experiment, dict):
            raise ValueError("Each experiment must be an object.")
        for key in ("name", "axis", "kind"):
            if not isinstance(experiment.get(key), str) or not experiment[key]:
                raise ValueError(f"Each experiment must define {key}.")
        if experiment["name"] in names:
            raise ValueError("Experiment names must be unique.")
        names.add(experiment["name"])
        axes.add(experiment["axis"])
        if experiment["kind"] == "prediction_jsonl":
            if not isinstance(experiment.get("predictions"), str) or not experiment["predictions"]:
                raise ValueError("prediction_jsonl experiments must declare a predictions path.")
            threshold = float(experiment.get("decision_threshold", DEFAULT_THRESHOLD))
            if not 0 <= threshold <= 1:
                raise ValueError("decision_threshold must be frozen in [0, 1].")
            for field in EXPECTED_HASH_FIELDS.values():
                if field in experiment and not _is_sha256(experiment[field]):
                    raise ValueError(f"{field} must be a lowercase SHA-256 hex string.")
            if "expected_model_weight_sha256" in experiment and not _is_sha256(experiment["expected_model_weight_sha256"]):
                raise ValueError("expected_model_weight_sha256 must be a lowercase SHA-256 hex string.")
            command = experiment.get("command")
            if command is not None and (not isinstance(command, list) or not all(isinstance(item, str) and item for item in command)):
                raise ValueError("command must be a list of non-empty strings.")
        elif experiment["kind"] == "not_applicable":
            if not isinstance(experiment.get("reason"), str) or not experiment["reason"]:
                raise ValueError("not_applicable experiments must record a reason.")
        else:
            raise ValueError(f"Unsupported experiment kind: {experiment['kind']}")
    missing = REQUIRED_EXPERIMENTS - axes
    if missing:
        raise ValueError(f"Suite manifest must explicitly cover Phase 8 experiments: {', '.join(sorted(missing))}.")


def validate_prediction_inputs(experiment: dict[str, Any], base: Path) -> dict[str, Any]:
    predictions = _resolve_path(experiment["predictions"], base)
    actual_hashes: dict[str, str] = {}
    for artifact_kind, expected_field in EXPECTED_HASH_FIELDS.items():
        path_value = experiment.get(artifact_kind)
        if not isinstance(path_value, str) or not path_value:
            if expected_field in experiment:
                raise ValueError(f"{experiment['name']} declares {expected_field} but omits {artifact_kind}.")
            continue
        path = _resolve_path(path_value, base)
        if not path.is_file():
            raise ValueError(f"{experiment['name']} {artifact_kind} file is missing: {path}")
        actual = _sha256_file(path)
        actual_hashes[f"{artifact_kind}_sha256"] = actual
        expected = experiment.get(expected_field)
        if expected is not None and actual != expected:
            raise ValueError(f"{experiment['name']} {artifact_kind} SHA-256 mismatch: expected {expected}, got {actual}.")

    rows = _read_jsonl(predictions)
    manifest_rows = _read_manifest_rows(_resolve_path(experiment["manifest"], base)) if isinstance(experiment.get("manifest"), str) else {}
    strict = bool(manifest_rows) or any(key.startswith("expected_") for key in experiment)
    sample_ids: set[str] = set()
    status_counts: Counter[str] = Counter()
    label_counts: Counter[str] = Counter()
    manifest_hash = actual_hashes.get("manifest_sha256")
    expected_weight = experiment.get("expected_model_weight_sha256")
    manifest_sample_ids = set(manifest_rows)
    observed_weights: set[str] = set()

    for index, row in enumerate(rows, start=1):
        sample_id = row.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id:
            raise ValueError(f"{experiment['name']} prediction line {index} has no string sample_id.")
        if sample_id in sample_ids:
            raise ValueError(f"{experiment['name']} has duplicate sample_id: {sample_id}")
        sample_ids.add(sample_id)
        if row.get("mock") is True or (isinstance(row.get("model_result"), dict) and row["model_result"].get("mock") is True):
            raise ValueError(f"{experiment['name']} has mock prediction: {sample_id}")
        status_counts[str(row.get("status", "missing_status"))] += 1
        if row.get("label") not in (0, 1):
            raise ValueError(f"{experiment['name']} prediction {sample_id} has invalid label.")
        label_counts[str(row["label"])] += 1
        score = _number(row.get(str(experiment.get("score_field", "risk_score"))))
        if strict and (row.get("status") != "completed" or score is None or not 0 <= score <= 1):
            raise ValueError(f"{experiment['name']} prediction {sample_id} is not a completed finite [0,1] score.")
        if manifest_hash is not None and row.get("manifest_sha256") != manifest_hash:
            raise ValueError(f"{experiment['name']} prediction {sample_id} manifest_sha256 mismatch.")
        weight = _prediction_weight_sha256(row)
        if weight is not None:
            observed_weights.add(weight)
        if expected_weight is not None and weight != expected_weight:
            raise ValueError(f"{experiment['name']} prediction {sample_id} model weight SHA-256 mismatch.")
        if manifest_rows and sample_id in manifest_rows:
            manifest_row = manifest_rows[sample_id]
            if row.get("source_sha256") != manifest_row["source_sha256"]:
                raise ValueError(f"{experiment['name']} prediction {sample_id} source_sha256 mismatch.")
            if row.get("label") != manifest_row["label"]:
                raise ValueError(f"{experiment['name']} prediction {sample_id} label mismatch.")

    if manifest_rows and sample_ids != manifest_sample_ids:
        missing = sorted(manifest_sample_ids - sample_ids)[:5]
        extra = sorted(sample_ids - manifest_sample_ids)[:5]
        raise ValueError(f"{experiment['name']} prediction sample_id set does not match manifest; missing={missing}, extra={extra}.")
    if len(observed_weights) > 1:
        raise ValueError(f"{experiment['name']} predictions contain multiple model weight SHA-256 values: {sorted(observed_weights)}")

    _assert_expected_value(experiment, "expected_total_rows", len(rows), experiment["name"])
    _assert_expected_value(experiment, "expected_label_counts", dict(sorted(label_counts.items())), experiment["name"])
    _assert_expected_value(experiment, "expected_status_counts", dict(sorted(status_counts.items())), experiment["name"])
    return {
        "status": "passed",
        "strict": strict,
        "actual_hashes": actual_hashes,
        "actual_model_weight_sha256s": sorted(observed_weights),
        "total_rows": len(rows),
        "label_counts": dict(sorted(label_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "sample_id_count": len(sample_ids),
        "manifest_sample_id_count": len(manifest_rows) if manifest_rows else None,
        "expected": {
            key: experiment[key]
            for key in (
                "expected_predictions_sha256",
                "expected_manifest_sha256",
                "expected_config_sha256",
                "expected_model_weight_sha256",
                "expected_total_rows",
                "expected_label_counts",
                "expected_status_counts",
            )
            if key in experiment
        },
    }


def _maybe_run_command(experiment: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    command = experiment.get("command")
    if command is None:
        return {"experiment": experiment["name"], "kind": "prediction_jsonl", "status": "using_frozen_prediction_jsonl", "command": None}
    started = time.monotonic()
    env = os.environ.copy()
    for key, value in (experiment.get("env") or {}).items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("Experiment env overrides must be string key/value pairs.")
        env[key] = value
    completed = subprocess.run(command, cwd=Path.cwd(), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False)
    record = {
        "experiment": experiment["name"],
        "kind": "prediction_jsonl",
        "status": "completed" if completed.returncode == 0 else "failed",
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-16384:],
        "stderr": completed.stderr[-16384:],
        "elapsed_seconds": round(time.monotonic() - started, 6),
    }
    command_log = output_dir / f"{_safe_name(experiment['name'])}.command.json"
    command_log.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"Experiment command failed for {experiment['name']}; see {command_log}.")
    return record


def _not_applicable_metrics(experiment: dict[str, Any], reason: str) -> dict[str, Any]:
    metric_reason = f"Not applicable: {reason}"
    return {
        "schema_version": "phase8-experiment-metrics-v1",
        "experiment": experiment["name"],
        "axis": experiment["axis"],
        "kind": experiment["kind"],
        "threshold": experiment.get("decision_threshold", DEFAULT_THRESHOLD),
        "score_field": experiment.get("score_field", "risk_score"),
        "prediction_jsonl_sha256": _sha256_empty(),
        "total_rows": 0,
        "scored_rows": 0,
        "skipped_rows": 0,
        "status_counts": {"not_applicable": 0},
        "metrics": {
            "accuracy": None, "precision": None, "recall": None, "f1": None, "roc_auc": None,
            "eer": None, "eer_method": "linear_interpolation_at_fpr_fnr_crossing", "false_positive_rate": None, "false_negative_rate": None,
        },
        "calibration": {"brier_score": None, "log_loss": None, "ece_10_bin": None},
        "latency": {
            "latency_scope": "visual_detector_detect_only",
            "latency_source": "model_result.elapsed_seconds",
            "includes_ffmpeg": False,
            "includes_frame_extraction": False,
            "includes_queue_or_web": False,
            "mean_seconds": None,
            "p50_seconds": None,
            "p95_seconds": None,
            "max_seconds": None,
        },
        "end_to_end_latency": {"status": "not_applicable", "reason": reason},
        "confusion_matrix": {"true_positive": None, "true_negative": None, "false_positive": None, "false_negative": None},
        "not_applicable": {
            "accuracy": metric_reason, "precision": metric_reason, "recall": metric_reason, "f1": metric_reason,
            "roc_auc": metric_reason, "eer": metric_reason, "false_positive_rate": metric_reason,
            "false_negative_rate": metric_reason, "brier_score": metric_reason, "log_loss": metric_reason,
            "ece_10_bin": metric_reason, "latency": metric_reason,
        },
        "model_hashes": {},
        "limitations": [reason],
        "roc_curve": [],
    }


def _score_tuple(row: dict[str, Any], score_field: str) -> tuple[int, float, dict[str, Any]] | None:
    score = _number(row.get(score_field))
    label = row.get("label")
    if row.get("status") == "completed" and label in (0, 1) and score is not None and 0 <= score <= 1:
        return int(label), score, row
    return None


def _confusion(values: list[tuple[int, float, dict[str, Any]]], threshold: float) -> dict[str, int | None]:
    if not values:
        return {"true_positive": None, "true_negative": None, "false_positive": None, "false_negative": None}
    tp = sum(1 for label, score, _ in values if label == 1 and score >= threshold)
    tn = sum(1 for label, score, _ in values if label == 0 and score < threshold)
    fp = sum(1 for label, score, _ in values if label == 0 and score >= threshold)
    fn = sum(1 for label, score, _ in values if label == 1 and score < threshold)
    return {"true_positive": tp, "true_negative": tn, "false_positive": fp, "false_negative": fn}


def _roc_auc(values: list[tuple[int, float]]) -> float | None:
    positives = sum(1 for label, _ in values if label == 1)
    negatives = len(values) - positives
    if not positives or not negatives:
        return None
    ordered = sorted(enumerate(values), key=lambda item: item[1][1])
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(ordered):
        end = cursor + 1
        while end < len(ordered) and ordered[end][1][1] == ordered[cursor][1][1]:
            end += 1
        average_rank = (cursor + 1 + end) / 2
        for original_index, _ in ordered[cursor:end]:
            ranks[original_index] = average_rank
        cursor = end
    rank_sum = sum(ranks[index] for index, (label, _) in enumerate(values) if label == 1)
    return round((rank_sum - positives * (positives + 1) / 2) / (positives * negatives), 6)


def _eer(values: list[tuple[int, float]]) -> float | None:
    points = _roc_curve([(label, score, {}) for label, score in values])
    if not points:
        return None
    diffs = [point["false_positive_rate"] - point["false_negative_rate"] for point in points]
    for point, diff in zip(points, diffs):
        if diff == 0:
            return round(point["false_positive_rate"], 6)
    for index in range(1, len(points)):
        previous, current = diffs[index - 1], diffs[index]
        if previous * current < 0:
            left, right = points[index - 1], points[index]
            ratio = abs(previous) / (abs(previous) + abs(current))
            fpr = left["false_positive_rate"] + ratio * (right["false_positive_rate"] - left["false_positive_rate"])
            fnr = left["false_negative_rate"] + ratio * (right["false_negative_rate"] - left["false_negative_rate"])
            return round((fpr + fnr) / 2, 6)
    best = min(points, key=lambda point: abs(point["false_positive_rate"] - point["false_negative_rate"]))
    return round((best["false_positive_rate"] + best["false_negative_rate"]) / 2, 6)


def _roc_curve(values: list[tuple[int, float, dict[str, Any]]]) -> list[dict[str, float]]:
    positives = sum(1 for label, _, _ in values if label == 1)
    negatives = len(values) - positives
    if not positives or not negatives:
        return []
    thresholds = [math.inf, *sorted({score for _, score, _ in values}, reverse=True), -math.inf]
    points: list[dict[str, float]] = []
    for threshold in thresholds:
        tp = sum(1 for label, score, _ in values if label == 1 and score >= threshold)
        fp = sum(1 for label, score, _ in values if label == 0 and score >= threshold)
        fn = positives - tp
        points.append({
            "threshold": threshold if math.isfinite(threshold) else (1.000001 if threshold > 0 else -0.000001),
            "true_positive_rate": round(tp / positives, 6),
            "false_positive_rate": round(fp / negatives, 6),
            "false_negative_rate": round(fn / positives, 6),
        })
    return points


def _ece(values: list[tuple[int, float]], bins: int) -> float:
    total = len(values)
    error = 0.0
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        bucket = [(label, score) for label, score in values if low <= score < high or (index == bins - 1 and score == 1.0)]
        if not bucket:
            continue
        confidence = sum(score for _, score in bucket) / len(bucket)
        accuracy = sum(label for label, _ in bucket) / len(bucket)
        error += len(bucket) / total * abs(confidence - accuracy)
    return round(error, 6)


def _write_failure_cases(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = ["experiment", "axis", "sample_id", "category", "label", "predicted_label", "risk_score", "threshold", "status", "quality_status", "failure_reason", "runtime_seconds", "relative_path"]
    rows = sorted(rows, key=lambda row: (str(row["category"]), str(row["experiment"]), str(row["sample_id"])))
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_metric_tables(csv_path: Path, markdown_path: Path, metrics: list[dict[str, Any]]) -> None:
    fields = ["experiment", "axis", "kind", "total_rows", "scored_rows", "f1", "roc_auc", "eer", "eer_method", "false_positive_rate", "false_negative_rate", "brier_score", "log_loss", LATENCY_TABLE_LABEL, "end_to_end_latency", "notes"]
    table_rows = []
    for item in metrics:
        row = {
            "experiment": item["experiment"],
            "axis": item["axis"],
            "kind": item["kind"],
            "total_rows": item["total_rows"],
            "scored_rows": item["scored_rows"],
            "f1": _display(item["metrics"].get("f1"), item["not_applicable"].get("f1")),
            "roc_auc": _display(item["metrics"].get("roc_auc"), item["not_applicable"].get("roc_auc")),
            "eer": _display(item["metrics"].get("eer"), item["not_applicable"].get("eer")),
            "eer_method": item["metrics"].get("eer_method", "linear_interpolation_at_fpr_fnr_crossing"),
            "false_positive_rate": _display(item["metrics"].get("false_positive_rate"), item["not_applicable"].get("false_positive_rate")),
            "false_negative_rate": _display(item["metrics"].get("false_negative_rate"), item["not_applicable"].get("false_negative_rate")),
            "brier_score": _display(item["calibration"].get("brier_score"), item["not_applicable"].get("brier_score")),
            "log_loss": _display(item["calibration"].get("log_loss"), item["not_applicable"].get("log_loss")),
            LATENCY_TABLE_LABEL: _display(item["latency"].get("p95_seconds"), item["not_applicable"].get("latency")),
            "end_to_end_latency": item.get("end_to_end_latency", {}).get("status", "not_applicable"),
            "notes": "; ".join(sorted(set(item["not_applicable"].values())))[:500],
        }
        table_rows.append(row)
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(table_rows)
    markdown = ["| " + " | ".join(fields) + " |", "| " + " | ".join("---" for _ in fields) + " |"]
    for row in table_rows:
        markdown.append("| " + " | ".join(str(row[field]).replace("|", "/") for field in fields) + " |")
    markdown_path.write_text("\n".join(markdown) + "\n", encoding="utf-8")


def _write_roc_outputs(metrics: dict[str, Any], svg_path: Path, points_path: Path) -> None:
    points = metrics.get("roc_curve") or []
    with points_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["threshold", "false_positive_rate", "true_positive_rate", "false_negative_rate"])
        writer.writeheader()
        writer.writerows(points)
    if not points:
        reason = metrics.get("not_applicable", {}).get("roc_auc", "ROC curve is not applicable.")
        svg_path.write_text(_empty_svg(metrics["experiment"], reason), encoding="utf-8")
        return
    width, height, margin = 520, 360, 48
    coords = []
    for point in points:
        x = margin + point["false_positive_rate"] * (width - 2 * margin)
        y = height - margin - point["true_positive_rate"] * (height - 2 * margin)
        coords.append(f"{x:.2f},{y:.2f}")
    polyline = " ".join(coords)
    title = escape(f"{metrics['experiment']} ROC")
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="white"/>
  <text x="{margin}" y="28" font-family="Arial, sans-serif" font-size="16">{title}</text>
  <line x1="{margin}" y1="{height-margin}" x2="{width-margin}" y2="{height-margin}" stroke="#333"/>
  <line x1="{margin}" y1="{height-margin}" x2="{margin}" y2="{margin}" stroke="#333"/>
  <line x1="{margin}" y1="{height-margin}" x2="{width-margin}" y2="{margin}" stroke="#bbb" stroke-dasharray="4 4"/>
  <polyline points="{polyline}" fill="none" stroke="#1769aa" stroke-width="2"/>
  <text x="{width//2 - 30}" y="{height-12}" font-family="Arial, sans-serif" font-size="12">FPR</text>
  <text x="10" y="{height//2}" font-family="Arial, sans-serif" font-size="12" transform="rotate(-90 10,{height//2})">TPR</text>
</svg>
'''
    svg_path.write_text(svg, encoding="utf-8")


def _empty_svg(title: str, reason: str) -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="520" height="180" viewBox="0 0 520 180">
  <rect width="100%" height="100%" fill="white"/>
  <text x="24" y="42" font-family="Arial, sans-serif" font-size="16">{escape(title)} ROC</text>
  <text x="24" y="82" font-family="Arial, sans-serif" font-size="13">{escape(reason[:180])}</text>
</svg>
'''


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path} line {line_number} is not an object.")
        rows.append(value)
    return rows


def _read_manifest_rows(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path} line {line_number} is not an object.")
        sample_id = value.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id or sample_id in rows:
            raise ValueError(f"{path} line {line_number} requires a unique sample_id.")
        if value.get("binary_label_fake") not in (0, 1) or not isinstance(value.get("source_sha256"), str):
            raise ValueError(f"{path} line {line_number} lacks binary_label_fake/source_sha256.")
        rows[sample_id] = {"label": value["binary_label_fake"], "source_sha256": value["source_sha256"]}
    if not rows:
        raise ValueError(f"{path} contains no manifest rows.")
    return rows


def _copy_text(source: Path, destination: Path) -> None:
    destination.write_bytes(source.read_bytes())


def _resolve_path(value: str, base: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _model_hashes(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    values: dict[str, set[str]] = {}
    for row in rows:
        for key in ("model_weight_sha256", "manifest_sha256"):
            if isinstance(row.get(key), str):
                values.setdefault(key, set()).add(row[key])
        model = row.get("model") if isinstance(row.get("model"), dict) else (row.get("model_result") or {}).get("model") if isinstance(row.get("model_result"), dict) else {}
        if isinstance(model, dict):
            if isinstance(model.get("weight_sha256"), str):
                values.setdefault("model.weight_sha256", set()).add(model["weight_sha256"])
            artifacts = model.get("frozen_artifacts")
            if isinstance(artifacts, dict):
                for artifact_name, artifact in artifacts.items():
                    if isinstance(artifact, dict) and isinstance(artifact.get("actual_sha256"), str):
                        values.setdefault(f"model.frozen_artifacts.{artifact_name}.actual_sha256", set()).add(artifact["actual_sha256"])
    return {key: sorted(item) for key, item in sorted(values.items())}


def _input_artifacts(experiment: dict[str, Any], base: Path) -> list[dict[str, str]]:
    artifacts = []
    for key in ("manifest", "config", "predictions"):
        value = experiment.get(key)
        if not isinstance(value, str) or not value:
            continue
        path = _resolve_path(value, base)
        record = {"kind": key, "path": str(path)}
        if path.is_file():
            record["sha256"] = _sha256_file(path)
        else:
            record["status"] = "missing"
        artifacts.append(record)
    return artifacts


def _environment_record() -> dict[str, Any]:
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "executable": sys.executable,
        "env_allowlist": {key: os.environ.get(key) for key in sorted(ENV_ALLOWLIST) if key in os.environ},
        "path_sha256": hashlib.sha256(os.environ.get("PATH", "").encode("utf-8")).hexdigest(),
        "redaction": "Only MingJian/CUDA allowlisted environment values are recorded; PATH is hashed to avoid leaking local paths.",
    }


def _git_state() -> dict[str, Any]:
    return {
        "commit": _git(["rev-parse", "HEAD"]),
        "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"]),
        "status_short": _git(["status", "--short"]),
    }


def _git(args: list[str]) -> str | None:
    try:
        completed = subprocess.run(["git", *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", check=False)
    except FileNotFoundError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _safe_name(value: str) -> str:
    output = "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in value.strip())
    if not output:
        raise ValueError("Experiment name produced an empty file stem.")
    return output


def _prediction_weight_sha256(row: dict[str, Any]) -> str | None:
    if isinstance(row.get("model_weight_sha256"), str):
        return row["model_weight_sha256"]
    model_result = row.get("model_result")
    if isinstance(model_result, dict):
        model = model_result.get("model")
        if isinstance(model, dict) and isinstance(model.get("weight_sha256"), str):
            return model["weight_sha256"]
    model = row.get("model")
    if isinstance(model, dict) and isinstance(model.get("weight_sha256"), str):
        return model["weight_sha256"]
    return None


def _assert_expected_value(experiment: dict[str, Any], key: str, actual: Any, name: str) -> None:
    if key in experiment and experiment[key] != actual:
        raise ValueError(f"{name} {key} mismatch: expected {experiment[key]}, got {actual}.")


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _runtime_seconds(row: dict[str, Any]) -> float | None:
    direct = _number(row.get("runtime_seconds"))
    if direct is not None:
        return direct
    model_result = row.get("model_result")
    if isinstance(model_result, dict):
        return _number(model_result.get("elapsed_seconds"))
    return None


def _divide(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _percentile(values: list[float], percentile: float) -> float:
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * percentile / 100
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return values[int(position)]
    return values[low] * (high - position) + values[high] * (position - low)


def _display(value: Any, reason: str | None) -> str:
    if value is None:
        return f"NA: {reason}" if reason else "NA"
    return str(value)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_empty() -> str:
    return hashlib.sha256(b"").hexdigest()


if __name__ == "__main__":
    main()
