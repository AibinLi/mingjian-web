from __future__ import annotations

import socket

from fastapi.testclient import TestClient

from backend.app import deepseek_worker
from backend.app.llm_analysis import DeepSeekReportAnalysisAdapter, MAX_PACKET_CHARS


async def unavailable_dns(_url: str) -> None:
    raise socket.gaierror("Temporary failure in name resolution")


def test_deepseek_dns_failure_has_stable_network_error(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    monkeypatch.setattr(deepseek_worker, "_resolve_provider", unavailable_dns)
    response = TestClient(deepseek_worker.app).post("/analyse", json={"text": "{}"})
    assert response.status_code == 200
    value = response.json()
    assert value["status"] == "network_error"
    assert value["verdict"] == "联网核验不可用，请人工核查"
    assert "test-only" not in value["error_trace"]


def test_deepseek_analyze_alias_and_missing_key_are_explicit(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    response = TestClient(deepseek_worker.app).post("/analyze", json={"text": "{}"})

    assert response.status_code == 200
    assert response.json()["status"] == "not_configured"
    assert response.json()["error_code"] == "deepseek_api_key_missing"


def test_large_claim_and_citation_packet_is_compacted_before_worker_dispatch() -> None:
    packet = {
        "claim_verification": {
            "claims": [
                {
                    "text": "可验证主张" * 500,
                    "citations": [
                        {
                            "title": "证据标题" * 100,
                            "url": f"https://evidence.example/{index}/{citation}",
                            "domain": "evidence.example",
                            "excerpt": "引用原文" * 800,
                        }
                        for citation in range(5)
                    ],
                }
                for index in range(16)
            ],
        },
        "asr": {"segments": [{"text": "转录内容" * 600} for _ in range(12)]},
        "ocr": {"items": [{"text": "屏幕文字" * 600} for _ in range(12)]},
    }

    payload, compacted = DeepSeekReportAnalysisAdapter._bounded_payload(packet)

    assert compacted is True
    assert payload is not None
    assert len(payload) <= MAX_PACKET_CHARS


def test_tavily_dns_failure_returns_no_results_and_no_score_signal(monkeypatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "test-only")
    monkeypatch.setattr(deepseek_worker, "_resolve_provider", unavailable_dns)
    response = TestClient(deepseek_worker.app).post("/search", json={"query": "bounded query"})
    assert response.status_code == 200
    value = response.json()
    assert value["status"] == "network_error"
    assert value["results"] == []
    assert value["verdict"] == "联网核验不可用，请人工核查"
