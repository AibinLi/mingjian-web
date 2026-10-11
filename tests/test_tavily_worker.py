from __future__ import annotations

import socket

from fastapi.testclient import TestClient

from backend.app import tavily_worker


def test_tavily_worker_health_reports_configuration_without_exposing_key(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_RETRIEVAL", "true")
    monkeypatch.setenv("TAVILY_API_KEY", "test-only-secret")

    response = TestClient(tavily_worker.app).get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "mingjian-tavily-worker",
        "retrieval_enabled": True,
        "tavily_configured": True,
    }
    assert "test-only-secret" not in response.text


def test_tavily_worker_reports_missing_key_without_external_request(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_RETRIEVAL", "true")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    response = TestClient(tavily_worker.app).post("/search", json={"query": "bounded query"})

    assert response.status_code == 200
    assert response.json()["status"] == "not_configured"
    assert response.json()["error_code"] == "tavily_api_key_missing"


def test_tavily_worker_accepts_the_api_evaluated_feature_flag(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_RETRIEVAL", "false")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    response = TestClient(tavily_worker.app).post(
        "/search",
        json={"query": "bounded query"},
        headers={"X-MingJian-Retrieval-Enabled": "true"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "not_configured"
    assert response.json()["error_code"] == "tavily_api_key_missing"


async def unavailable_dns() -> None:
    raise socket.gaierror("Temporary failure in name resolution")


def test_tavily_worker_dns_failure_is_a_stable_network_error(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_RETRIEVAL", "true")
    monkeypatch.setenv("TAVILY_API_KEY", "test-only")
    monkeypatch.setattr(tavily_worker, "_resolve_provider", unavailable_dns)

    response = TestClient(tavily_worker.app).post("/search", json={"query": "bounded query"})

    assert response.status_code == 200
    value = response.json()
    assert value["status"] == "network_error"
    assert value["results"] == []
    assert value["verdict"] == "联网核验不可用，请人工核查"
    assert "test-only" not in value["error_trace"]
