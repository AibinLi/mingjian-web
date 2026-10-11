import json

from evaluation.compute_metrics import compute_metrics


def test_metrics_are_recomputed_from_prediction_jsonl(tmp_path) -> None:
    predictions = tmp_path / "predictions.jsonl"
    rows = [
        {"status": "completed", "label": 0, "risk_score": 0.1},
        {"status": "completed", "label": 1, "risk_score": 0.9},
        {"status": "unavailable", "label": 1, "risk_score": None},
    ]
    predictions.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    result = compute_metrics(predictions)

    assert result["total_rows"] == 3
    assert result["scored_rows"] == 2
    assert result["skipped_rows"] == 1
    assert result["metrics"]["accuracy"] == 1.0
    assert result["metrics"]["roc_auc"] == 1.0


def test_metrics_decline_to_claim_values_without_completed_rows(tmp_path) -> None:
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(json.dumps({"status": "unavailable", "label": 0, "risk_score": None}) + "\n", encoding="utf-8")

    result = compute_metrics(predictions)

    assert result["metrics"] is None
    assert "no metrics" in result["reason"].lower()
