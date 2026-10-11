from __future__ import annotations

from backend.app.aggregate_assessment import (
    AggregateAssessmentBuilder,
    InternetVerificationBuilder,
    ScorecardConfig,
)


def citation(url: str) -> dict:
    return {
        "url": url,
        "title": "Fixture evidence",
        "domain": "evidence.example",
        "published_at": None,
        "fetched_at": "2026-07-19T00:00:00+00:00",
        "excerpt": "The saved evidence directly addresses the claim.",
        "relation": "background",
        "source_kind": "fetched",
        "mock": False,
    }


def test_internet_verification_uses_only_validated_real_citation_indexes() -> None:
    verification = {
        "mock": False,
        "claims": [
            {"claim_id": "c1", "original_text": "The event occurred.", "citations": [citation("https://evidence.example/one")]},
            {"claim_id": "c2", "original_text": "A second event occurred.", "citations": []},
        ],
    }
    llm = {
        "status": "completed",
        "provider": "DeepSeek",
        "model": "fixture",
        "generated_at": "2026-07-19T00:00:00+00:00",
        "internet_verification": {
            "claim_results": [
                {"claim_index": 0, "status": "refuted", "citation_indexes": [0], "reason": "The citation contradicts the claim."},
                {"claim_index": 1, "status": "supported", "citation_indexes": [9], "reason": "Invalid citation index."},
                {"claim_index": 99, "status": "supported", "citation_indexes": [0]},
            ],
            "summary": "One claim is contradicted and one lacks evidence.",
        },
    }

    result = InternetVerificationBuilder().build(verification, llm)

    assert result["status"] == "completed"
    assert result["verdict_code"] == "refuted"
    assert result["risk_score"] == 90.0
    assert result["claim_results"][0]["citations"][0]["url"] == "https://evidence.example/one"
    assert result["claim_results"][1]["status"] == "insufficient_evidence"
    assert result["automatic_action"] == "none"


def test_mock_or_missing_network_evidence_never_becomes_a_low_score() -> None:
    result = InternetVerificationBuilder().build(
        {"mock": True, "claims": [{"claim_id": "fixture", "citations": [citation("https://evidence.example/mock")]}]},
        {"status": "completed", "provider": "fixture", "internet_verification": {"claim_results": [{"claim_index": 0, "status": "supported", "citation_indexes": [0]}]}},
    )
    assert result["status"] == "insufficient_evidence"
    assert result["risk_score"] is None


def scorecard_builder() -> AggregateAssessmentBuilder:
    return AggregateAssessmentBuilder(ScorecardConfig(
        weights={"authenticity": 0.40, "content": 0.25, "provenance": 0.10, "internet_verification": 0.25},
        low_max=35.0,
        high_min=65.0,
        minimum_coverage=0.65,
    ))


def test_environment_configuration_rejects_zero_weights_and_overlapping_bands(monkeypatch) -> None:
    for name in ("AUTHENTICITY", "CONTENT", "PROVENANCE", "INTERNET"):
        monkeypatch.setenv(f"MINGJIAN_SCORE_WEIGHT_{name}", "0")
    monkeypatch.setenv("MINGJIAN_SCORE_LOW_MAX", "80")
    monkeypatch.setenv("MINGJIAN_SCORE_HIGH_MIN", "20")

    config = ScorecardConfig.from_env()

    assert config.weights == {"authenticity": 0.4, "content": 0.25, "provenance": 0.1, "internet_verification": 0.25}
    assert (config.low_max, config.high_min) == (35.0, 65.0)


def complete_report(authenticity: float, content_status: str, internet_score: float) -> dict:
    return {
        "visual_model": {"status": "completed", "video_risk": {"risk_score": authenticity}},
        "audio_model": {"status": "not_applicable", "whole_audio_risk": None},
        "audio_visual_sync": {"status": "not_applicable", "risk_score": None},
        "claim_verification": {"mock": False, "claims": [{"claim_id": "c1", "status": content_status}]},
        "axes": {"provenance": {"mock": False, "risk_score": 60.0}},
        "internet_verification": {"status": "completed", "mock": False, "risk_score": internet_score, "verdict_code": "refuted" if internet_score > 50 else "supported"},
    }


def test_scorecard_uses_one_formula_and_configured_cross_dimension_weights() -> None:
    result = scorecard_builder().build(complete_report(0.80, "refuted", 90.0))

    assert result["components"]["authenticity"]["score"] == 80.0
    assert result["components"]["content"]["score"] == 90.0
    assert result["components"]["provenance"]["score"] == 60.0
    assert result["components"]["internet_verification"]["score"] == 90.0
    assert result["overall_score"] == 83.0
    assert result["routing_band"] == "high_risk_review"
    assert result["routing_label"] == "伪造风险高（需人工复核）"
    assert result["truth_label"] == "none"
    assert result["configured_coverage"] == 1.0
    assert result["applicable_coverage"] == 1.0
    assert result["automatic_action"] == "none"


def test_missing_internet_verification_forces_human_intervention() -> None:
    report = complete_report(0.10, "supported", 10.0)
    report["internet_verification"] = {"status": "insufficient_evidence", "mock": False, "risk_score": None}

    result = scorecard_builder().build(report)

    assert result["components"]["internet_verification"]["score"] is None
    assert result["overall_score"] is None
    assert result["partial_score"] is not None
    assert result["routing_band"] == "manual_review_required"
    assert result["routing_label"] == "需人工干预"
    assert result["configured_coverage"] == 0.75
    assert result["applicable_coverage"] < 1.0


def test_extreme_authenticity_risk_escalates_without_filling_missing_axes() -> None:
    report = complete_report(0.96, "supported", 10.0)
    report["claim_verification"] = {"mock": True, "claims": []}
    report["axes"]["provenance"] = {"mock": True, "status": "unavailable", "risk_score": None}
    report["internet_verification"] = {"status": "network_error", "mock": False, "risk_score": None}
    result = scorecard_builder().build(report)
    assert result["configured_coverage"] == 0.4
    assert result["partial_score"] == 96.0
    assert result["routing_band"] == "high_risk_review"
    assert result["automatic_action"] == "none"
    assert result["truth_label"] == "none"


def test_network_error_is_excluded_from_partial_score_and_coverage() -> None:
    report = complete_report(0.80, "supported", 90.0)
    report["internet_verification"] = {
        "status": "network_error",
        "mock": False,
        "risk_score": 99.0,
        "verdict": "联网核验不可用，请人工核查",
    }

    result = scorecard_builder().build(report)

    assert result["components"]["internet_verification"]["status"] == "unavailable"
    assert result["components"]["internet_verification"]["score"] is None
    assert result["configured_coverage"] == 0.75
    assert result["partial_score"] == 54.0
    assert result["overall_score"] is None
