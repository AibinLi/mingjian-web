import json
import subprocess
import sys
from pathlib import Path

from PIL import Image


def test_evaluation_runner_writes_raw_prediction_jsonl(tmp_path) -> None:
    root = tmp_path / "dataset"
    frames = root / "frames"
    frames.mkdir(parents=True)
    Image.new("RGB", (320, 240), color=(40, 70, 110)).save(frames / "sample.png")
    manifest = root / "manifest.jsonl"
    manifest.write_text(json.dumps({
        "sample_id": "contract-sample",
        "label": 0,
        "frames": [{"path": "frames/sample.png", "timestamp_seconds": 0.0, "timestamp_quality": "exact"}],
    }) + "\n", encoding="utf-8")
    output = tmp_path / "predictions.jsonl"
    project = Path(__file__).resolve().parents[1]

    subprocess.run([
        sys.executable, "evaluation/run_visual_baseline.py",
        "--manifest", str(manifest),
        "--dataset-root", str(root),
        "--work-dir", str(root / "work"),
        "--output", str(output),
        "--device", "cpu",
        "--batch-size", "1",
    ], cwd=project, check=True)

    prediction = json.loads(output.read_text(encoding="utf-8"))
    assert prediction["schema_version"] == "visual-prediction-jsonl-v1"
    assert prediction["sample_id"] == "contract-sample"
    assert prediction["status"] == "completed"
    assert isinstance(prediction["risk_score"], float)
