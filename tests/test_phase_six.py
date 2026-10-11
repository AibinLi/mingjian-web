from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.main import PhaseSixReporter
from backend.app.phase_six import CalibrationRegistry, QualityGateEvaluator, ReviewRuleEngine
from backend.app.visual_detector import WEIGHT_SHA256
from evaluation import calibrate_scores
from evaluation.build_phase6_calibration_manifest import _group_id, build_manifest
from evaluation.evaluate_frozen_calibrator import evaluate as evaluate_frozen


def valid_artifact() -> dict:
    record = {
        "schema_version": "score-calibrator-v1", "calibrator_version": "fixture-v2", "deployment_status": "active", "model_key": "visual.meso4.fake_score", "model_weight_sha256": WEIGHT_SHA256,
        "method": "platt_logistic", "parameters": {"intercept": 0.0, "slope": 1.0, "input_transform": "logit(clamp(raw_score, 1e-6, 1-1e-6))"}, "review_threshold": 0.5,
        "inputs": {"dataset_role": "calibration", "prediction_jsonl_sha256": "a" * 64, "manifest_sha256": "b" * 64, "official_test_list_sha256": "e" * 64, "model_key": "visual.meso4.fake_score", "model_weight_sha256": WEIGHT_SHA256, "official_test_overlap_count": 0},
        "validation": {"kind": "group_aware_deterministic_frozen_holdout", "dataset_role": "calibration", "official_test_overlap_count": 0, "group_overlap_count": 0, "fit_sample_ids_sha256": "c" * 64, "validation_sample_ids_sha256": "d" * 64},
        "final_evaluation": {"accepted": True},
        "metrics_before": {"brier_score": 0.2}, "metrics_after": {"brier_score": 0.1},
    }
    record["artifact_sha256"] = CalibrationRegistry._artifact_sha256(record)
    return record


def registry(directory: Path, record: dict | None = None) -> CalibrationRegistry:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "visual.json").write_text(json.dumps(record or valid_artifact()), encoding="utf-8")
    return CalibrationRegistry(directory)


def report(content_status: str = "no_evidence", *, content_mock: bool = False, provenance_mock: bool = True) -> dict:
    claims = [] if content_status == "no_evidence" else [{"status": content_status}]
    return {"axes": {"authenticity": {"name": "媒体真实性"}, "content": {"name": "内容可信度", "mock": content_mock, "claims": claims}, "provenance": {"name": "来源可靠性", "mock": provenance_mock}}}


def quality(*, has_audio: bool = True, vad: dict | None = None) -> dict:
    return QualityGateEvaluator().evaluate({"input_features": {"has_video": True, "has_audio": has_audio, "width": 640, "height": 480, "duration_seconds": 8}}, {"status": "completed", "tracks": [{"min_face_side_px": 90}]}, vad or {"status": "available", "quality": {"speech_seconds": 3}}, {}, {}, {})


def calibrated(registry_value: CalibrationRegistry, score: float | None) -> dict:
    return {"visual": registry_value.apply("visual.meso4.fake_score", score), "audio": registry_value.apply("audio.ast.fake_score", None), "sync": registry_value.apply("sync.haar_motion_anomaly", None)}


@pytest.mark.parametrize("mutation", [
    lambda value: value.update({"artifact_sha256": "wrong"}),
    lambda value: value["parameters"].update({"slope": float("inf")}),
    lambda value: value.update({"review_threshold": 1.1}),
    lambda value: value["inputs"].update({"dataset_role": "test"}),
    lambda value: value["validation"].update({"official_test_overlap_count": 1}),
    lambda value: value.update({"model_weight_sha256": "wrong"}),
    lambda value: value.update({"deployment_status": "rejected"}),
])
def test_registry_rejects_tampered_or_ineligible_artifacts(tmp_path: Path, mutation) -> None:
    artifact = valid_artifact()
    mutation(artifact)
    # All mutations except the explicit bad hash change the canonical payload.
    if artifact["artifact_sha256"] != "wrong":
        artifact["artifact_sha256"] = CalibrationRegistry._artifact_sha256(artifact)
    assert registry(tmp_path, artifact).apply("visual.meso4.fake_score", 0.9)["status"] == "unavailable"


def test_registry_rejects_model_key_mismatch(tmp_path: Path) -> None:
    artifact = valid_artifact()
    artifact["model_key"] = "audio.ast.fake_score"
    artifact["inputs"]["model_key"] = "audio.ast.fake_score"
    artifact["artifact_sha256"] = CalibrationRegistry._artifact_sha256(artifact)
    assert registry(tmp_path, artifact).describe("audio.ast.fake_score")["status"] == "unavailable"


def test_mock_content_never_creates_conflict_but_high_visual_remains_reviewable(tmp_path: Path) -> None:
    engine, active = ReviewRuleEngine(), registry(tmp_path)
    decision = engine.decide(report("conflicting", content_mock=True), quality(), calibrated(active, 0.9))
    assert decision["status"] == "review_recommended"
    assert decision["decision_trace"][0]["rule_id"] == "R-002-media-review-signal"
    no_high = engine.decide(report("conflicting", content_mock=True), quality(), calibrated(active, 0.1))
    assert no_high["status"] == "insufficient_evidence"
    assert no_high["decision_trace"][0]["rule_id"] != "R-005-content-conflict"


def test_uncalibrated_high_visual_can_only_recommend_review() -> None:
    value = report("conflicting", content_mock=True)
    value["visual_model"] = {"status": "completed", "video_risk": {"risk_score": 0.9}, "frame_risks": [{"risk_threshold": 0.5}]}
    value["audio_model"] = {"status": "not_applicable"}
    unavailable = {"visual": {"status": "unavailable"}, "audio": {"status": "not_applicable"}, "sync": {"status": "not_applicable"}}
    decision = ReviewRuleEngine().decide(value, quality(has_audio=False), unavailable)
    assert decision["status"] == "review_recommended"
    assert decision["automatic_action"] == "none"
    assert decision["decision_trace"][0]["rule_id"] == "R-002-media-review-signal"


def test_missing_calibrator_is_explicitly_uncalibrated(tmp_path: Path) -> None:
    result = CalibrationRegistry(tmp_path / "missing").apply("visual.meso4.fake_score", 0.9)
    assert result["calibrated_score"] is None
    assert result["uncalibrated"] is True
    assert result["reason"] == "No verified frozen-validation calibrator is configured."


def test_real_conflict_and_provenance_mock_are_kept_separate(tmp_path: Path) -> None:
    active = registry(tmp_path)
    real_conflict = ReviewRuleEngine().decide(report("conflicting", content_mock=False, provenance_mock=False), quality(), calibrated(active, 0.9))
    assert real_conflict["status"] == "unable_to_determine"
    provenance_only = ReviewRuleEngine().decide(report("supported", content_mock=False, provenance_mock=True), quality(), calibrated(active, 0.1))
    assert provenance_only["status"] == "insufficient_evidence"
    assert provenance_only["decision_trace"][0]["rule_id"] == "R-004-evidence-insufficient"


@pytest.mark.parametrize(("vad", "expected"), [
    ({"status": "unavailable", "quality": {}}, "unavailable"),
    ({"status": "available", "quality": {"speech_seconds": 0}}, "degraded"),
    ({"status": "available", "quality": {"speech_seconds": 0.4}}, "degraded"),
    ({"status": "available", "quality": {"speech_seconds": 2.0}}, "available"),
])
def test_audio_speech_gate_never_uses_total_media_duration(vad: dict, expected: str) -> None:
    states = {gate["code"]: gate["status"] for gate in quality(vad=vad)["gates"]}
    assert states["audio_duration"] == "available"
    assert states["audio_speech"] == expected
    missing = {gate["code"]: gate["status"] for gate in quality(has_audio=False)["gates"]}
    assert missing["audio_modality"] == "missing"
    assert missing["audio_speech"] == "not_applicable"


def _manifest_and_predictions(tmp_path: Path) -> tuple[Path, Path, Path]:
    official = tmp_path / "List_of_testing_videos.txt"
    official.write_text("1 YouTube-real/official.mp4\n", encoding="utf-8")
    official_hash = calibrate_scores._sha256_file(official)
    manifest = tmp_path / "manifest.jsonl"
    manifest_rows = []
    for label, values in ((0, ("r1", "r2", "r3", "r4")), (1, ("f1", "f2", "f3", "f4"))):
        for index, sample_id in enumerate(values):
            manifest_rows.append({"dataset_role": "calibration", "sample_id": sample_id, "relative_path": f"x/{sample_id}.mp4", "label": "fake" if label else "real", "binary_label_fake": label, "source_sha256": f"{sample_id:0<64}"[:64], "official_testing_list_sha256": official_hash, "official_test_overlap_count": 0, "group_id": f"celeb/id{sample_id[-1]}"})
    manifest.write_text("".join(json.dumps(row) + "\n" for row in manifest_rows), encoding="utf-8")
    manifest_hash = calibrate_scores._sha256_file(manifest)
    predictions = tmp_path / "predictions.jsonl"
    rows = [{"dataset_role": "calibration", "official_test_overlap_count": 0, "manifest_sha256": manifest_hash, "model_key": "visual.meso4.fake_score", "model_weight_sha256": WEIGHT_SHA256, "status": "completed", "risk_score": 0.2 + label * 0.6 + index * 0.01, "label": label, "sample_id": row["sample_id"], "source_sha256": row["source_sha256"]} for label in (0, 1) for index, row in enumerate([entry for entry in manifest_rows if entry["binary_label_fake"] == label])]
    predictions.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return manifest, predictions, official


def test_calibration_input_guards_reject_test_hash_and_duplicate_ids(tmp_path: Path, monkeypatch) -> None:
    manifest, predictions, official = _manifest_and_predictions(tmp_path)
    kwargs = (predictions, manifest, official, "visual.meso4.fake_score", WEIGHT_SHA256, "fixture-v2", "seed")
    artifact = calibrate_scores.calibrate(*kwargs)
    assert artifact["inputs"]["dataset_role"] == "calibration"
    assert artifact["validation"]["group_overlap_count"] == 0
    monkeypatch.setattr(calibrate_scores, "_sha256_file", lambda _path: calibrate_scores.PHASE3_OFFICIAL_TEST_PREDICTIONS_SHA256)
    with pytest.raises(ValueError, match="official-test"):
        calibrate_scores.calibrate(*kwargs)
    monkeypatch.undo()
    rows = predictions.read_text(encoding="utf-8").splitlines()
    predictions.write_text("\n".join(rows + [rows[0]]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        calibrate_scores.calibrate(*kwargs)


def test_calibration_recomputes_actual_official_path_overlap(tmp_path: Path) -> None:
    manifest, predictions, official = _manifest_and_predictions(tmp_path)
    rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
    official.write_text(f"1 {rows[0]['relative_path']}\n", encoding="utf-8")
    official_hash = calibrate_scores._sha256_file(official)
    for row in rows:
        row["official_testing_list_sha256"] = official_hash
    manifest.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    with pytest.raises(ValueError, match="actual official-test path"):
        calibrate_scores.calibrate(predictions, manifest, official, "visual.meso4.fake_score", WEIGHT_SHA256, "fixture-v2", "seed")


def test_global_visible_identity_split_has_zero_intersection(tmp_path: Path) -> None:
    manifest, predictions, official = _manifest_and_predictions(tmp_path)
    manifest_rows = calibrate_scores._read_manifest(manifest, calibrate_scores._sha256_file(official), {"YouTube-real/official.mp4"})
    rows = calibrate_scores._read_predictions(predictions, manifest_rows, calibrate_scores._sha256_file(manifest), "visual.meso4.fake_score", WEIGHT_SHA256)
    fit, validation = calibrate_scores._group_split(rows, "seed", 0.20)
    assert {row[3] for row in fit}.isdisjoint({row[3] for row in validation})
    assert {row[0] for row in fit} == {0, 1}
    assert {row[0] for row in validation} == {0, 1}


def test_manifest_builder_excludes_official_test_paths_and_records_zero_overlap(tmp_path: Path) -> None:
    root = tmp_path / "Celeb-DF-v2"
    for directory in ("Celeb-real", "Celeb-synthesis", "YouTube-real"):
        (root / directory).mkdir(parents=True)
    for name in ("id0_0000.mp4", "id1_0000.mp4", "id2_0000.mp4"):
        (root / "Celeb-real" / name).write_bytes(name.encode())
    for name in ("id0_id9_0000.mp4", "id1_id9_0000.mp4", "id2_id9_0000.mp4"):
        (root / "Celeb-synthesis" / name).write_bytes(name.encode())
    (root / "YouTube-real" / "00000.mp4").write_bytes(b"y")
    (root / "List_of_testing_videos.txt").write_text("1 YouTube-real/00000.mp4\n", encoding="utf-8")
    output = tmp_path / "manifest.jsonl"
    summary = build_manifest(root, output, 2, 2, "seed")
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert summary["official_test_overlap_count"] == 0
    assert all(row["dataset_role"] == "calibration" and row["official_test_overlap_count"] == 0 for row in rows)
    assert all(row["relative_path"] != "YouTube-real/00000.mp4" for row in rows)
    assert _group_id("Celeb-real/id0_0000.mp4") == _group_id("Celeb-synthesis/id0_id9_0000.mp4") == "celeb/id0"


def test_final_evaluation_marks_worse_candidate_rejected(tmp_path: Path) -> None:
    candidate = valid_artifact()
    candidate["deployment_status"] = "candidate"
    candidate.pop("final_evaluation")
    candidate["parameters"] = {"intercept": 20.0, "slope": 0.0, "input_transform": "logit"}
    candidate["artifact_sha256"] = CalibrationRegistry._artifact_sha256(candidate)
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    predictions = tmp_path / "official.jsonl"
    rows = [{"status": "completed", "label": label, "risk_score": score} for label, score in ((0, 0.1), (0, 0.2), (1, 0.8), (1, 0.9))]
    predictions.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    evaluation, finalized = evaluate_frozen(candidate_path, predictions)
    assert evaluation["accepted"] is False
    assert finalized["deployment_status"] == "rejected"
    assert registry(tmp_path / "registry", finalized).describe("visual.meso4.fake_score")["status"] == "unavailable"


def test_phase_six_report_persists_only_review_recommendation(tmp_path: Path) -> None:
    class Visual:
        def detect(self, *_args): return {"status": "completed", "mock": False, "limitations": [], "video_risk": {"risk_score": 0.9}, "frame_risks": [{"risk_threshold": 0.5}], "evidence": {"highest_risk_frame": {"frame_index": 1}, "suspicious_segments": []}}
    class Audio:
        def detect(self, *_args): return {"status": "not_applicable", "mock": False, "limitations": ["no audio"], "whole_audio_risk": None, "segment_risks": [], "evidence": {"highest_risk_segment": None, "suspicious_segments": []}}
    class Sync:
        def analyse(self, *_args): return {"status": "not_applicable", "mock": False, "limitations": ["no audio"], "risk_score": None, "anomalous_intervals": [], "evidence": {"best_offset_seconds": None, "best_correlation": None, "suspicious_intervals": []}}
        def describe(self): return {"name": "fixture"}
    folder = tmp_path / "derived" / "phase-six"; folder.mkdir(parents=True)
    for name, value in {"faces": {"status": "completed", "tracks": [{"min_face_side_px": 90}]}, "vad": {"status": "not_applicable", "quality": {}}, "asr": {"status": "unavailable", "segments": []}, "ocr": {"status": "unavailable", "items": []}, "frame_index": []}.items():
        (folder / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")
    preprocess = {"status": "completed", "input_features": {"has_video": True, "has_audio": False, "width": 640, "height": 480, "duration_seconds": 4}, "quality_gates": [], "artifacts": [{"name": name, "path": f"derived/phase-six/{name}.json"} for name in ("faces", "vad", "asr", "ocr", "frame_index")]}
    result = PhaseSixReporter(tmp_path, Visual(), Audio(), Sync(), calibration_registry=registry(tmp_path)).create({"id": "phase-six"}, preprocess)
    assert set(result["axes"]) == {"authenticity", "content", "provenance"}
    assert result["phase6"]["recommendation"]["automatic_action"] == "none"
    assert result["aggregate_assessment"]["routing_label"] == "需人工干预"
    assert result["aggregate_assessment"]["truth_label"] == "none"
    assert {item["name"] for item in preprocess["artifacts"]} >= {"internet_verification", "aggregate_assessment", "phase6_quality", "phase6_recommendation"}
