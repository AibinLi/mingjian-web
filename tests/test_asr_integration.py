from __future__ import annotations

import json

import httpx

from backend.app import asr_worker, preprocess, remote_asr
from backend.app.remote_asr import RemoteAsrAdapter


def audio_fixture(tmp_path):  # type: ignore[no-untyped-def]
    path = tmp_path / "derived" / "job-id" / "audio.wav"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"fixture")
    return path


def test_remote_asr_sends_shared_relative_path_and_retries_busy(monkeypatch, tmp_path) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "0.1"}, json={"detail": "busy"})
        return httpx.Response(200, json={
            "status": "completed",
            "mock": False,
            "transcript": "fixture transcript",
            "language": "en",
            "segments": [{"text": "fixture transcript", "start_seconds": 0.0, "end_seconds": 1.0, "confidence": None}],
            "confidence": None,
        })

    monkeypatch.setenv("MINGJIAN_ASR_WORKER_URL", "http://asr.internal:8003")
    monkeypatch.setattr(remote_asr.time, "sleep", lambda _seconds: None)
    adapter = RemoteAsrAdapter(tmp_path, transport=httpx.MockTransport(handler))

    result = adapter.transcribe(audio_fixture(tmp_path))

    assert result["status"] == "completed"
    assert len(calls) == 3
    assert calls == [{"audio_path": "derived/job-id/audio.wav"}] * 3
    assert adapter.describe()["system_proxy_bypassed"] is True


def test_remote_asr_http_error_never_collapses_to_an_empty_limitation(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MINGJIAN_ASR_WORKER_URL", "http://asr.internal:8003")
    transport = httpx.MockTransport(lambda _request: httpx.Response(503, content=b""))

    result = RemoteAsrAdapter(tmp_path, transport=transport).transcribe(audio_fixture(tmp_path))

    assert result["status"] == "unavailable"
    assert result["limitations"] == ["Internal ASR Worker returned HTTP 503."]
    assert "HTTP 503" in result["error_trace"]


def test_asr_pipeline_budget_exceeds_worker_budget(monkeypatch) -> None:
    monkeypatch.setenv("MINGJIAN_ASR_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("MINGJIAN_ASR_PIPELINE_TIMEOUT_SECONDS", "70")

    assert asr_worker._timeout_from_env() == 60.0
    assert remote_asr._pipeline_timeout() == 70.0
    assert preprocess._asr_timeout_from_env() == 70.0
