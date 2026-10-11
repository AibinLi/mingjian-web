"""Recompute basic binary metrics only from saved visual prediction JSONL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from collections import Counter


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--output", type=Path, help="Optional JSON metrics output path.")
    args = parser.parse_args()
    metrics = compute_metrics(args.predictions, args.threshold)
    payload = json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)


def compute_metrics(predictions: Path, threshold: float = 0.5) -> dict[str, Any]:
    completed: list[tuple[int, float]] = []
    total = 0
    status_counts: Counter[str] = Counter()
    for line_number, line in enumerate(predictions.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        total += 1
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Prediction line {line_number} is invalid JSON.") from error
        status_counts[str(row.get("status", "missing_status"))] += 1
        if row.get("status") == "completed" and row.get("label") in (0, 1) and isinstance(row.get("risk_score"), (int, float)):
            completed.append((int(row["label"]), float(row["risk_score"])))
    if not completed:
        return {"schema_version": "visual-metrics-v1", "threshold": threshold, "total_rows": total, "scored_rows": 0, "status_counts": dict(sorted(status_counts.items())), "metrics": None, "reason": "No completed labelled predictions; no metrics are claimed."}
    tp = sum(1 for label, score in completed if label == 1 and score >= threshold)
    tn = sum(1 for label, score in completed if label == 0 and score < threshold)
    fp = sum(1 for label, score in completed if label == 0 and score >= threshold)
    fn = sum(1 for label, score in completed if label == 1 and score < threshold)
    precision = _divide(tp, tp + fp)
    recall = _divide(tp, tp + fn)
    f1 = _divide(2 * precision * recall, precision + recall) if precision is not None and recall is not None else None
    return {
        "schema_version": "visual-metrics-v1",
        "threshold": threshold,
        "total_rows": total,
        "scored_rows": len(completed),
        "skipped_rows": total - len(completed),
        "status_counts": dict(sorted(status_counts.items())),
        "metrics": {
            "accuracy": _divide(tp + tn, len(completed)),
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "roc_auc": _roc_auc(completed),
            "confusion_matrix": {"true_positive": tp, "true_negative": tn, "false_positive": fp, "false_negative": fn},
        },
    }


def _divide(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


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
    positive_rank_sum = sum(ranks[index] for index, (label, _) in enumerate(values) if label == 1)
    return round((positive_rank_sum - positives * (positives + 1) / 2) / (positives * negatives), 6)


if __name__ == "__main__":
    main()
