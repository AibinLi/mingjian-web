from fastapi.testclient import TestClient

from backend.app.main import create_app


def test_health_reports_phase_seven_traceable_web_report_and_phase_six_model_stack(tmp_path) -> None:
    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "mingjian-web-api"
    assert payload["phase"] == "11-real-content-source-verification-in-progress"
    assert payload["visual_detector"]["name"] == "DeepfakeBench Meso4"
    assert payload["audio_detector"]["license"] == "MIT"
    assert payload["audio_visual_sync"]["mock"] is True
    assert payload["claim_verification"]["search_adapter"] == "FakeSearchAdapter"
    assert payload["claim_verification"]["refresh_search_adapter"] == "FakeSearchAdapter"
    assert payload["llm_report_analysis"] == {
        "refresh_adapter": "DisabledReportAnalysisAdapter",
        "refresh_only": True,
        "raw_media_sent": False,
    }
    assert payload["aggregate_assessment"]["schema_version"] == "aggregate-review-scorecard-v1"
    assert payload["aggregate_assessment"]["weights"] == {"authenticity": 0.4, "content": 0.25, "provenance": 0.1, "internet_verification": 0.25}
    assert payload["aggregate_assessment"]["automatic_truth_label"] is False
    assert payload["calibration"]["visual"]["status"] == "unavailable"
    assert payload["recommendation_ruleset"] == "phase6-rules-v1"
    assert payload["security"]["url_fetch"] == "disabled_by_default"
    assert payload["feature_flags"] == {"ENABLE_ASR": False, "ENABLE_OCR": False, "ENABLE_FACE": False, "ENABLE_SYNC": False, "ENABLE_RETRIEVAL": False, "ENABLE_LLM": False}


def test_sync_feature_flag_enables_real_adapter_without_claiming_dependency(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ENABLE_SYNC", "true")
    with TestClient(create_app(tmp_path, start_worker=False)) as client:
        payload = client.get("/health").json()
    assert payload["feature_flags"]["ENABLE_SYNC"] is True
    assert payload["audio_visual_sync"]["mock"] is False
    assert payload["audio_visual_sync"]["version"] == "haar-motion-avsync-v1"


def test_retrieval_feature_flag_is_used_only_by_the_refresh_adapter(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ENABLE_RETRIEVAL", "true")
    monkeypatch.setenv("MINGJIAN_RETRIEVAL_WORKER_URL", "http://retrieval.internal:8005")
    with TestClient(create_app(tmp_path, start_worker=False)) as client:
        payload = client.get("/health").json()
    assert payload["feature_flags"]["ENABLE_RETRIEVAL"] is True
    assert payload["claim_verification"]["search_adapter"] == "FakeSearchAdapter"
    assert payload["claim_verification"]["refresh_search_adapter"] == "TavilySearchAdapter"
    assert payload["claim_verification"]["retrieval_worker_url"] == "http://retrieval.internal:8005"


def test_llm_feature_flag_is_used_only_by_the_refresh_adapter(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ENABLE_LLM", "true")
    monkeypatch.setenv("MINGJIAN_LLM_ANALYSIS_URL", "http://deepseek.internal:8006")

    with TestClient(create_app(tmp_path, start_worker=False)) as client:
        payload = client.get("/health").json()

    assert payload["feature_flags"]["ENABLE_LLM"] is True
    assert payload["llm_report_analysis"]["refresh_adapter"] == "DeepSeekReportAnalysisAdapter"
