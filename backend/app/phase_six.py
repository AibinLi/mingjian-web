"""Phase 6 calibration provenance, input-quality gates, and review-only rules.

This module deliberately keeps the three product axes separate.  It records a
calibrated *model-domain score* when a verified calibrator is available, but
never turns that score into a universal authenticity, truth, or source claim.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
from pathlib import Path
from typing import Any

from .visual_detector import WEIGHT_SHA256 as MESO4_WEIGHT_SHA256


CALIBRATOR_SCHEMA = "score-calibrator-v1"
QUALITY_SCHEMA = "phase6-quality-gates-v1"
DECISION_SCHEMA = "phase6-review-recommendation-v1"
RULESET_VERSION = "phase6-rules-v1"
MIN_FACE_SIDE_PX = 48
MIN_AUDIO_SECONDS = 1.5
MIN_WIDTH = 320
MIN_HEIGHT = 180
PHASE3_OFFICIAL_TEST_PREDICTIONS_SHA256 = "4e7e87b508d7e1a27d8ff56afe8f84b5685a703e9aaf60a9a61fdd3a36adb8eb"
HOLDOUT_KIND = "group_aware_deterministic_frozen_holdout"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


class CalibrationRegistry:
    """Loads immutable calibrator records; malformed records are never applied."""

    def __init__(self, directory: Path | None = None, *, expected_models: dict[str, str] | None = None) -> None:
        configured = os.environ.get("MINGJIAN_CALIBRATOR_DIR")
        self.directory = (directory or (Path(configured) if configured else None))
        self.expected_models = expected_models or {"visual.meso4.fake_score": MESO4_WEIGHT_SHA256}
        self._records = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.directory or not self.directory.is_dir():
            return {}
        records: dict[str, dict[str, Any]] = {}
        for path in sorted(self.directory.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if self._valid(record):
                records[record["model_key"]] = {**record, "artifact_path": str(path)}
        return records

    def _valid(self, record: Any) -> bool:
        if not isinstance(record, dict) or record.get("schema_version") != CALIBRATOR_SCHEMA:
            return False
        if record.get("deployment_status") != "active":
            return False
        if record.get("method") != "platt_logistic" or record.get("model_key") not in self.expected_models:
            return False
        parameters = record.get("parameters")
        inputs, validation = record.get("inputs"), record.get("validation")
        if not isinstance(parameters, dict) or any(_number(parameters.get(key)) is None for key in ("intercept", "slope")):
            return False
        threshold = _number(record.get("review_threshold"))
        if threshold is None or not 0 <= threshold <= 1:
            return False
        required_inputs = {"dataset_role", "prediction_jsonl_sha256", "manifest_sha256", "official_test_list_sha256", "model_key", "model_weight_sha256", "official_test_overlap_count"}
        required_validation = {"kind", "dataset_role", "official_test_overlap_count", "group_overlap_count", "fit_sample_ids_sha256", "validation_sample_ids_sha256"}
        if not isinstance(inputs, dict) or not isinstance(validation, dict) or not required_inputs.issubset(inputs) or not required_validation.issubset(validation):
            return False
        if inputs.get("dataset_role") != "calibration" or validation.get("dataset_role") != "calibration" or validation.get("kind") != HOLDOUT_KIND:
            return False
        if inputs.get("prediction_jsonl_sha256") == PHASE3_OFFICIAL_TEST_PREDICTIONS_SHA256 or inputs.get("official_test_overlap_count") != 0 or validation.get("official_test_overlap_count") != 0 or validation.get("group_overlap_count") != 0:
            return False
        if inputs.get("model_key") != record["model_key"] or inputs.get("model_weight_sha256") != record.get("model_weight_sha256") or record.get("model_weight_sha256") != self.expected_models[record["model_key"]]:
            return False
        if not all(isinstance(inputs.get(key), str) and inputs[key] for key in ("prediction_jsonl_sha256", "manifest_sha256", "model_weight_sha256")):
            return False
        final_evaluation = record.get("final_evaluation")
        if not isinstance(final_evaluation, dict) or final_evaluation.get("accepted") is not True:
            return False
        return isinstance(record.get("artifact_sha256"), str) and record["artifact_sha256"] == self._artifact_sha256(record)

    @staticmethod
    def _artifact_sha256(record: dict[str, Any]) -> str:
        clean = {key: value for key, value in record.items() if key != "artifact_sha256"}
        return hashlib.sha256(json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def describe(self, model_key: str) -> dict[str, Any]:
        record = self._records.get(model_key)
        if not record:
            return {"status": "unavailable", "model_key": model_key, "reason": "No verified frozen-validation calibrator is configured."}
        return {"status": "available", "model_key": model_key, "calibrator_version": record.get("calibrator_version"), "method": record["method"], "artifact_sha256": record.get("artifact_sha256"), "validation": record.get("validation"), "metrics_before": record.get("metrics_before"), "metrics_after": record.get("metrics_after")}

    def apply(self, model_key: str, raw_score: Any) -> dict[str, Any]:
        score = _number(raw_score)
        if score is None or not 0.0 <= score <= 1.0:
            return {"status": "not_applicable", "model_key": model_key, "raw_score": raw_score, "calibrated_score": None, "reason": "Raw score is absent or outside [0, 1]."}
        record = self._records.get(model_key)
        if not record:
            return {"status": "unavailable", "model_key": model_key, "raw_score": score, "calibrated_score": None, "uncalibrated": True, "reason": "No verified frozen-validation calibrator is configured."}
        slope, intercept = float(record["parameters"]["slope"]), float(record["parameters"]["intercept"])
        logit = math.log(max(1e-6, min(1 - 1e-6, score)) / (1 - max(1e-6, min(1 - 1e-6, score))))
        calibrated = 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, intercept + slope * logit))))
        return {"status": "available", "model_key": model_key, "raw_score": round(score, 6), "calibrated_score": round(calibrated, 6), "calibrator_version": record.get("calibrator_version"), "artifact_sha256": record.get("artifact_sha256"), "review_threshold": record.get("review_threshold", 0.5), "scope": "Frozen-validation estimate for this model/domain only; not a universal media-authenticity probability."}


class QualityGateEvaluator:
    """Produces availability/quality states only; no item is a risk score."""

    def evaluate(self, preprocess: dict[str, Any] | None, faces: dict[str, Any], vad: dict[str, Any], visual: dict[str, Any], audio: dict[str, Any], sync: dict[str, Any]) -> dict[str, Any]:
        features = (preprocess or {}).get("input_features", {}) if isinstance(preprocess, dict) else {}
        gates: list[dict[str, Any]] = []
        has_video, has_audio = features.get("has_video"), features.get("has_audio")
        gates.append(self._gate("visual_modality", "authenticity", "available" if has_video else "missing", "Visual modality is available." if has_video else "No video/image modality; this is not low-risk evidence."))
        gates.append(self._gate("audio_modality", "authenticity", "available" if has_audio else "missing", "Audio modality is available." if has_audio else "No audio modality; this is not low-risk evidence."))
        width, height = _number(features.get("width")), _number(features.get("height"))
        if has_video and width is not None and height is not None and (width < MIN_WIDTH or height < MIN_HEIGHT):
            gates.append(self._gate("resolution", "authenticity", "degraded", "Low-resolution input limits analysis; it is not low-risk evidence.", {"minimum_width": MIN_WIDTH, "minimum_height": MIN_HEIGHT}, {"width": width, "height": height}))
        elif has_video:
            gates.append(self._gate("resolution", "authenticity", "available", "Resolution is above the phase-6 minimum."))
        else:
            gates.append(self._gate("resolution", "authenticity", "not_applicable", "No visual modality."))
        gates.append(self._face_gate(faces, has_video))
        duration = _number(features.get("duration_seconds"))
        if has_audio and duration is None:
            gates.append(self._gate("audio_duration", "authenticity", "unavailable", "Audio duration is unavailable; this is not low-risk evidence."))
        elif has_audio and duration < MIN_AUDIO_SECONDS:
            gates.append(self._gate("audio_duration", "authenticity", "degraded", "Total audio is short; this is not low-risk evidence.", {"minimum_seconds": MIN_AUDIO_SECONDS}, {"duration_seconds": duration}))
        elif has_audio:
            gates.append(self._gate("audio_duration", "authenticity", "available", "Total audio duration meets the phase-6 minimum.", {"minimum_seconds": MIN_AUDIO_SECONDS}, {"duration_seconds": duration}))
        else:
            gates.append(self._gate("audio_duration", "authenticity", "not_applicable", "No audio modality."))
        speech_seconds = _number(((vad.get("quality") or {}) if isinstance(vad, dict) else {}).get("speech_seconds"))
        if not has_audio:
            gates.append(self._gate("audio_speech", "authenticity", "not_applicable", "No audio modality; absence is not low-risk evidence."))
        elif vad.get("status") not in {"available", "degraded", "completed"} or speech_seconds is None:
            gates.append(self._gate("audio_speech", "authenticity", "unavailable", "VAD has no auditable effective-speech duration; total media duration cannot substitute for speech evidence.", {"minimum_seconds": MIN_AUDIO_SECONDS}, {"speech_seconds": speech_seconds, "vad_status": vad.get("status")}))
        elif speech_seconds <= 0:
            gates.append(self._gate("audio_speech", "authenticity", "degraded", "VAD found no effective speech; silence or music is not low-risk evidence.", {"minimum_seconds": MIN_AUDIO_SECONDS}, {"speech_seconds": speech_seconds}))
        elif speech_seconds < MIN_AUDIO_SECONDS:
            gates.append(self._gate("audio_speech", "authenticity", "degraded", "Effective speech is too short for a stable review; this is not low-risk evidence.", {"minimum_seconds": MIN_AUDIO_SECONDS}, {"speech_seconds": speech_seconds}))
        else:
            gates.append(self._gate("audio_speech", "authenticity", "available", "Effective speech duration meets the phase-6 minimum.", {"minimum_seconds": MIN_AUDIO_SECONDS}, {"speech_seconds": speech_seconds}))
        explicit_ood = features.get("ood_status") or visual.get("ood_status") or audio.get("ood_status") or sync.get("ood_status")
        if explicit_ood in {"out_of_distribution", "ood"}:
            gates.append(self._gate("distribution", "authenticity", "out_of_distribution", "Input is explicitly outside a validated model domain; do not infer low risk."))
        else:
            gates.append(self._gate("distribution", "authenticity", "unknown", "No implemented OOD detector validates that this input matches the frozen calibration domain."))
        return {"schema_version": QUALITY_SCHEMA, "gates": gates, "summary": {"blocking_statuses": sorted({gate["status"] for gate in gates if gate["status"] in {"missing", "out_of_distribution", "unavailable"}}), "degraded": any(gate["status"] == "degraded" for gate in gates)}}

    @staticmethod
    def _gate(code: str, axis: str, state: str, reason: str, threshold: dict[str, Any] | None = None, observed: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"code": code, "axis": axis, "status": state, "reason": reason, "threshold": threshold or {}, "observed": observed or {}}

    def _face_gate(self, faces: dict[str, Any], has_video: Any) -> dict[str, Any]:
        if not has_video:
            return self._gate("face_size", "authenticity", "not_applicable", "No visual modality.")
        if faces.get("status") not in {"completed", "available"}:
            return self._gate("face_size", "authenticity", "unavailable", "Face size is not measured by an available detector; absence of a face result is not low-risk evidence.", {"minimum_side_px": MIN_FACE_SIDE_PX})
        sides: list[float] = []
        for track in faces.get("tracks", []):
            if not isinstance(track, dict):
                continue
            for key in ("min_face_side_px", "face_side_px"):
                value = _number(track.get(key))
                if value is not None:
                    sides.append(value)
            for box in track.get("boxes", []):
                if isinstance(box, dict):
                    width, height = _number(box.get("width")), _number(box.get("height"))
                    if width is not None and height is not None:
                        sides.append(min(width, height))
            for box in track.get("bboxes", []):
                if isinstance(box, list) and len(box) == 4:
                    coordinates = [_number(value) for value in box]
                    if all(value is not None for value in coordinates):
                        sides.append(min(float(coordinates[2]) - float(coordinates[0]), float(coordinates[3]) - float(coordinates[1])))
        if not sides:
            return self._gate("face_size", "authenticity", "unavailable", "Face detector did not emit auditable face dimensions; this is not low-risk evidence.", {"minimum_side_px": MIN_FACE_SIDE_PX})
        minimum = min(sides)
        return self._gate("face_size", "authenticity", "degraded" if minimum < MIN_FACE_SIDE_PX else "available", "Face is below the minimum review size; this is not low-risk evidence." if minimum < MIN_FACE_SIDE_PX else "Measured face size meets the phase-6 minimum.", {"minimum_side_px": MIN_FACE_SIDE_PX}, {"minimum_face_side_px": minimum})


class ReviewRuleEngine:
    """Auditable rules that recommend review only and never take enforcement action."""

    def decide(self, report: dict[str, Any], quality: dict[str, Any], calibration: dict[str, Any]) -> dict[str, Any]:
        trace: list[dict[str, Any]] = []
        content = report.get("axes", {}).get("content", {})
        provenance = report.get("axes", {}).get("provenance", {})
        visual = calibration.get("visual", {})
        audio = calibration.get("audio", {})
        sync = calibration.get("sync", {})
        blocked = {gate["status"] for gate in quality["gates"] if gate["status"] in {"missing", "out_of_distribution", "unavailable"}}
        content_is_mock = bool(content.get("mock"))
        provenance_is_mock = bool(provenance.get("mock"))
        # Mock claims are pipeline fixtures, not content-evidence signals.  In
        # particular, a mock "conflicting" state must never manufacture a
        # cross-axis conflict recommendation.
        content_states = set() if content_is_mock else {claim.get("status") for claim in content.get("claims", []) if isinstance(claim, dict)}
        high = [item for item in (visual, audio, sync) if item.get("status") == "available" and isinstance(item.get("calibrated_score"), (int, float)) and item["calibrated_score"] >= item.get("review_threshold", 0.5)]
        # A rejected/missing calibrator does not erase the original model
        # signal.  Raw configured thresholds may still recommend review, but
        # the trace explicitly marks them uncalibrated and never calls them a
        # probability or low-risk conclusion.
        visual_result, audio_result = report.get("visual_model", {}), report.get("audio_model", {})
        if visual.get("status") != "available" and visual_result.get("status") == "completed":
            raw_score = _number((visual_result.get("video_risk") or {}).get("risk_score"))
            threshold = _number((visual_result.get("frame_risks") or [{}])[0].get("risk_threshold"))
            if raw_score is not None and threshold is not None and raw_score >= threshold:
                high.append({"model_key": "visual.meso4.fake_score", "raw_score": raw_score, "uncalibrated": True})
        if audio.get("status") != "available" and audio_result.get("status") == "completed":
            raw_score = _number((audio_result.get("whole_audio_risk") or {}).get("risk_score"))
            threshold = _number((audio_result.get("segment_risks") or [{}])[0].get("risk_threshold"))
            if raw_score is not None and threshold is not None and raw_score >= threshold:
                high.append({"model_key": "audio.ast.fake_score", "raw_score": raw_score, "uncalibrated": True})
        if "conflicting" in content_states and high:
            trace.append({"rule_id": "R-006-strong-conflict", "matched": True, "inputs": {"content_states": sorted(content_states), "high_signals": [item["model_key"] for item in high]}})
            return self._result("unable_to_determine", "建议人工复核", "高风险媒体信号与内容证据内部冲突同时存在，系统不作合并断言。", trace, calibration)
        if "conflicting" in content_states:
            trace.append({"rule_id": "R-005-content-conflict", "matched": True, "inputs": {"content_states": sorted(content_states)}})
            return self._result("unable_to_determine", "建议人工复核", "内容证据存在支持与反对的冲突，需人工核查引用链。", trace, calibration)
        if "out_of_distribution" in blocked:
            trace.append({"rule_id": "R-003-quality-block", "matched": True, "inputs": {"quality_statuses": sorted(blocked)}})
            return self._result("insufficient_evidence", "建议补充可用模态或人工复核", "输入被标记为分布外；这不是低风险证据。", trace, calibration)
        if high:
            trace.append({"rule_id": "R-002-media-review-signal", "matched": True, "inputs": {"high_signals": high}})
            return self._result("review_recommended", "建议人工复核", "存在达到冻结校准域或已配置原始复核阈值的媒体信号；未校准信号不是概率，这也不是自动判定、封禁或违法结论。", trace, calibration)
        if content_is_mock or provenance_is_mock or "no_evidence" in content_states or blocked:
            trace.append({"rule_id": "R-004-evidence-insufficient", "matched": True, "inputs": {"content_mock": content_is_mock, "provenance_mock": provenance_is_mock, "content_states": sorted(content_states), "quality_statuses": sorted(blocked)}})
            return self._result("insufficient_evidence", "建议补充证据或人工复核", "内容或来源轴仍为 Mock、暂无证据或存在缺失模态，不能产生低风险建议。", trace, calibration)
        trace.append({"rule_id": "R-001-no-automatic-disposition", "matched": True, "inputs": {"calibration_statuses": {key: value.get("status") for key, value in calibration.items()}}})
        return self._result("insufficient_evidence", "建议保留三轴证据并按需人工复核", "规则不会把独立轴压缩成真/假，也不会自动处置内容。", trace, calibration)

    @staticmethod
    def _result(status: str, suggestion: str, rationale: str, trace: list[dict[str, Any]], calibration: dict[str, Any]) -> dict[str, Any]:
        return {"schema_version": DECISION_SCHEMA, "ruleset_version": RULESET_VERSION, "status": status, "suggestion": suggestion, "rationale": rationale, "automatic_action": "none", "legal_or_policy_determination": "none", "decision_trace": trace, "calibration_inputs": calibration, "limitations": ["This is a review recommendation only; it does not automatically block content or determine legality.", "Media authenticity, content credibility, and provenance remain separate axes and are never fused into a single truth label."]}
