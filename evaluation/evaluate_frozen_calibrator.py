"""Evaluate a frozen calibrator without fitting or changing its parameters."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

EPSILON = 1e-6


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--evaluation-output", type=Path, required=True)
    parser.add_argument("--finalized-calibrator", type=Path, required=True)
    args = parser.parse_args()
    evaluation, finalized = evaluate(args.candidate, args.predictions)
    for path, value in ((args.evaluation_output, evaluation), (args.finalized_calibrator, finalized)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(evaluation, ensure_ascii=False, indent=2, sort_keys=True))


def evaluate(candidate_path: Path, predictions_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    if candidate.get("artifact_sha256") != _artifact_sha256(candidate):
        raise ValueError("Candidate artifact SHA-256 is invalid.")
    if candidate.get("deployment_status") != "candidate":
        raise ValueError("Only a frozen candidate may enter final evaluation.")
    intercept = _finite(candidate.get("parameters", {}).get("intercept"))
    slope = _finite(candidate.get("parameters", {}).get("slope"))
    if intercept is None or slope is None:
        raise ValueError("Candidate parameters are invalid.")
    values: list[tuple[int, float]] = []
    for line_number, line in enumerate(predictions_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        score = _finite(row.get("risk_score"))
        if row.get("status") != "completed" or row.get("label") not in (0, 1) or score is None or not 0 <= score <= 1:
            raise ValueError(f"Final-evaluation prediction line {line_number} is not a completed labelled score.")
        values.append((int(row["label"]), score))
    before = _metrics(values)
    after = _metrics([(label, _sigmoid(intercept + slope * _logit(score))) for label, score in values])
    accepted = not (after["brier_score"] > before["brier_score"] and after["log_loss"] > before["log_loss"])
    evaluation = {"schema_version": "phase6-calibrator-final-evaluation-v1", "mode": "evaluation_only_no_fitting_no_threshold_selection", "predictions_sha256": _sha256_file(predictions_path), "candidate_artifact_sha256": candidate["artifact_sha256"], "sample_count": len(values), "metrics_before": before, "metrics_after": after, "acceptance_rule": "Reject when Brier score and log loss are both worse after applying the frozen candidate.", "accepted": accepted, "deployment_status": "active" if accepted else "rejected", "parameters_frozen": True}
    evaluation["evaluation_sha256"] = hashlib.sha256(json.dumps(evaluation, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    finalized = {key: value for key, value in candidate.items() if key != "artifact_sha256"}
    finalized["deployment_status"] = evaluation["deployment_status"]
    finalized["final_evaluation"] = evaluation
    finalized["artifact_sha256"] = _artifact_sha256(finalized)
    return evaluation, finalized


def _metrics(values: list[tuple[int, float]]) -> dict[str, float | None]:
    brier = sum((score - label) ** 2 for label, score in values) / len(values)
    log_loss = -sum(label * math.log(max(EPSILON, score)) + (1 - label) * math.log(max(EPSILON, 1 - score)) for label, score in values) / len(values)
    positives, negatives = sum(label for label, _ in values), sum(1 - label for label, _ in values)
    auc = None
    if positives and negatives:
        ordered, ranks, cursor = sorted(enumerate(values), key=lambda item: item[1][1]), [0.0] * len(values), 0
        while cursor < len(ordered):
            end = cursor + 1
            while end < len(ordered) and ordered[end][1][1] == ordered[cursor][1][1]:
                end += 1
            for original, _ in ordered[cursor:end]:
                ranks[original] = (cursor + 1 + end) / 2
            cursor = end
        rank_sum = sum(ranks[index] for index, (label, _) in enumerate(values) if label == 1)
        auc = round((rank_sum - positives * (positives + 1) / 2) / (positives * negatives), 6)
    return {"brier_score": round(brier, 6), "log_loss": round(log_loss, 6), "roc_auc": auc}


def _logit(score: float) -> float:
    value = max(EPSILON, min(1 - EPSILON, score))
    return math.log(value / (1 - value))


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, value))))


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _artifact_sha256(record: dict[str, Any]) -> str:
    clean = {key: value for key, value in record.items() if key != "artifact_sha256"}
    return hashlib.sha256(json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    main()
