from __future__ import annotations

import subprocess
from pathlib import Path

from backend.app.claim_verification import FakeSearchAdapter
from backend.app.face_detection import MODEL_SOURCE_URL, OpenCvYuNetFaceAdapter
from backend.app import preprocess
from backend.app.provenance_verification import ProvenanceVerifier, provenance_axis
from backend.app.tesseract_ocr import MAX_OCR_FRAMES, TesseractOcrAdapter, ocr_timeout_policy


TSV = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t91\t北京\n5\t1\t1\t1\t1\t2\t10\t0\t10\t10\t83\t发布\n"


def test_tesseract_ocr_keeps_timestamped_text_and_bounds_frames(tmp_path: Path) -> None:
    frames = []
    for index in range(1, 21):
        path = tmp_path / "derived" / "job" / "frames" / f"frame-{index:06d}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
        frames.append({"index": index, "path": path.relative_to(tmp_path).as_posix(), "timestamp_seconds": float(index), "timestamp_quality": "exact"})

    def runner(command, **kwargs):  # type: ignore[no-untyped-def]
        assert command[0] == "fixture-tesseract"
        assert kwargs["timeout"] == 30.0
        assert kwargs["env"]["OMP_THREAD_LIMIT"] == "1"
        return subprocess.CompletedProcess(command, 0, TSV, "")

    result = TesseractOcrAdapter(tmp_path, executable="fixture-tesseract", runner=runner)
    result.describe = lambda: {"name": "fixture", "available": True, "executable": "fixture-tesseract"}  # type: ignore[method-assign]
    value = result.extract(frames)

    assert value["status"] == "completed"
    assert value["mock"] is False
    assert value["frames_processed"] == MAX_OCR_FRAMES
    assert len(value["items"]) == MAX_OCR_FRAMES
    assert value["items"][0]["text"] == "北京 发布"
    assert value["items"][0]["confidence"] == 0.87
    assert value["items"][0]["bbox"] == [0, 0, 20, 10]
    assert isinstance(value["items"][0]["timestamp_seconds"], float)


def test_ocr_timeout_policy_uses_selected_frame_count(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for name in (
        "MINGJIAN_OCR_FRAME_TIMEOUT_SECONDS",
        "MINGJIAN_OCR_TOTAL_BASE_SECONDS",
        "MINGJIAN_OCR_TOTAL_PER_FRAME_SECONDS",
        "MINGJIAN_OCR_OUTER_MARGIN_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    assert ocr_timeout_policy(1) == {"selected_frames": 1, "per_frame": 30.0, "total": 50.0, "outer": 110.0}
    assert ocr_timeout_policy(5) == {"selected_frames": 5, "per_frame": 30.0, "total": 130.0, "outer": 190.0}
    assert ocr_timeout_policy(12) == {"selected_frames": 12, "per_frame": 30.0, "total": 270.0, "outer": 330.0}
    assert ocr_timeout_policy(150) == ocr_timeout_policy(12)


def test_ocr_timeout_policy_invalid_environment_falls_back(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    names_and_values = (
        ("MINGJIAN_OCR_FRAME_TIMEOUT_SECONDS", "invalid"),
        ("MINGJIAN_OCR_FRAME_TIMEOUT_SECONDS", "nan"),
        ("MINGJIAN_OCR_FRAME_TIMEOUT_SECONDS", "Infinity"),
        ("MINGJIAN_OCR_TOTAL_BASE_SECONDS", "0"),
        ("MINGJIAN_OCR_TOTAL_PER_FRAME_SECONDS", "-1"),
        ("MINGJIAN_OCR_OUTER_MARGIN_SECONDS", "9999"),
    )
    for name, value in names_and_values:
        monkeypatch.setenv(name, value)
        assert ocr_timeout_policy(5) == {"selected_frames": 5, "per_frame": 30.0, "total": 130.0, "outer": 190.0}
        monkeypatch.delenv(name)


def test_ocr_timeout_policy_environment_override_keeps_hard_caps(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MINGJIAN_OCR_FRAME_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("MINGJIAN_OCR_TOTAL_BASE_SECONDS", "200")
    monkeypatch.setenv("MINGJIAN_OCR_TOTAL_PER_FRAME_SECONDS", "25")
    monkeypatch.setenv("MINGJIAN_OCR_OUTER_MARGIN_SECONDS", "120")
    policy = ocr_timeout_policy(12)
    assert policy == {"selected_frames": 12, "per_frame": 45.0, "total": 300.0, "outer": 360.0}
    assert policy["outer"] > policy["total"]


def test_run_ocr_passes_dynamic_outer_timeout(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured = {}

    def run_optional(adapter, method_name, args, timeout_seconds):  # type: ignore[no-untyped-def]
        captured.update({"adapter": adapter, "method_name": method_name, "args": args, "timeout_seconds": timeout_seconds})
        return {"status": "completed"}

    frames = [{"index": index} for index in range(5)]
    adapter = object()
    monkeypatch.setattr(preprocess, "_run_optional_module", run_optional)
    assert preprocess._run_ocr(adapter, frames) == {"status": "completed"}
    assert captured == {"adapter": adapter, "method_name": "extract", "args": (frames,), "timeout_seconds": 190.0}


def test_tesseract_rejects_frame_paths_outside_derived_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"fixture")
    adapter = TesseractOcrAdapter(tmp_path, executable="fixture")
    adapter.describe = lambda: {"name": "fixture", "available": True, "executable": "fixture"}  # type: ignore[method-assign]
    value = adapter.extract([{"index": 1, "path": "outside.jpg", "timestamp_seconds": 0.0}])
    assert value["status"] == "unavailable"
    assert value["items"] == []


def test_yunet_never_downloads_and_missing_artifact_is_unavailable(tmp_path: Path) -> None:
    adapter = OpenCvYuNetFaceAdapter(tmp_path, model_path=tmp_path / "missing.onnx", opencv_dir=tmp_path / "missing-cv2")
    value = adapter.detect_and_track([])
    assert MODEL_SOURCE_URL.startswith("https://github.com/opencv/opencv_zoo/")
    assert value["status"] == "unavailable"
    assert value["tracks"] == []
    assert value["model"]["remote_downloads_disabled"] is True


def test_yunet_detection_shape_and_iou_contract() -> None:
    detection = OpenCvYuNetFaceAdapter._detection([10, 20, 30, 40, 15, 30, 30, 30, 22, 40, 16, 50, 28, 50, 0.91], 100, 100)
    assert detection == {"bbox": [10.0, 20.0, 40.0, 60.0], "landmarks": [[15.0, 30.0], [30.0, 30.0], [22.0, 40.0], [16.0, 50.0], [28.0, 50.0]], "confidence": 0.91}
    assert OpenCvYuNetFaceAdapter._iou([0, 0, 10, 10], [5, 5, 15, 15]) == 25 / 175


class Search:
    def __init__(self, records):
        self.records = records

    def search(self, query):  # type: ignore[no-untyped-def]
        assert query["kind"] == "provenance"
        return self.records


def record(url: str) -> dict:
    return {"url": url, "title": "source", "domain": "example.org", "excerpt": "source excerpt", "fetched_at": "2026-07-19T00:00:00+00:00", "source_kind": "fetched"}


def test_provenance_exact_and_same_domain_results_are_numeric_but_not_probabilities() -> None:
    exact = ProvenanceVerifier(Search([record("https://example.org/original")])).verify("https://example.org/original")
    partial = ProvenanceVerifier(Search([record("https://example.org/another")])).verify("https://example.org/original")

    assert exact["status"] == "completed" and exact["risk_score"] == 20.0
    assert exact["evidence"][0]["match"] == "exact_url"
    assert partial["status"] == "completed" and partial["risk_score"] == 50.0
    assert partial["evidence"][0]["match"] == "same_domain"
    assert provenance_axis(exact)["risk_score"] == 20.0


def test_provenance_missing_url_or_mock_search_never_gets_a_score() -> None:
    missing = ProvenanceVerifier(Search([])).verify(None)
    mock = ProvenanceVerifier(FakeSearchAdapter()).verify("https://example.org/original")
    assert missing["status"] == "unavailable" and missing["risk_score"] is None
    assert mock["mock"] is True and mock["risk_score"] is None
