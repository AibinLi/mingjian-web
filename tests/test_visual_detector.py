import json
from pathlib import Path

from PIL import Image

from backend.app.visual_detector import Meso4VisualDetector, WEIGHT_SHA256


def _preprocess_with_frame(root: Path) -> dict:
    derived = root / "derived" / "visual-test"
    frames = derived / "frames"
    frames.mkdir(parents=True)
    image_path = frames / "frame-000001.png"
    Image.new("RGB", (320, 240), color=(80, 120, 160)).save(image_path)
    index_path = derived / "frame_index.json"
    index_path.write_text(json.dumps([{
        "index": 1,
        "timestamp_seconds": 0.0,
        "timestamp_quality": "exact",
        "path": "derived/visual-test/frames/frame-000001.png",
    }]), encoding="utf-8")
    return {
        "status": "completed",
        "artifacts": [{"name": "frame_index", "path": "derived/visual-test/frame_index.json"}],
        "quality_gates": [],
    }


def test_meso4_returns_frame_track_video_risks_and_evidence(tmp_path) -> None:
    detector = Meso4VisualDetector(tmp_path, device="cpu", batch_size=1, timeout_seconds=10)
    result = detector.detect({"id": "visual-test"}, _preprocess_with_frame(tmp_path), lambda: False)

    assert result["schema_version"] == "visual-detector-result-v1"
    assert result["status"] == "completed"
    assert result["mock"] is False
    assert result["model"]["weight_sha256"] == WEIGHT_SHA256
    assert result["configuration"]["device_selected"] == "cpu"
    assert len(result["frame_risks"]) == 1
    assert result["track_risks"][0]["track_type"] == "sampled_visual_frame_sequence"
    assert result["video_risk"]["aggregation"] == "max_frame_risk"
    assert result["evidence"]["highest_risk_frame"]["frame_index"] == 1


def test_missing_weight_is_explicitly_unavailable_not_low_risk(tmp_path) -> None:
    detector = Meso4VisualDetector(tmp_path, weights_path=tmp_path / "missing.pth", device="cpu")
    result = detector.detect({"id": "visual-test"}, _preprocess_with_frame(tmp_path), lambda: False)

    assert result["status"] == "unavailable"
    assert result["video_risk"] is None
    assert result["frame_risks"] == []
    assert "低风险" in "".join(result["limitations"])
