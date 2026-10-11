"""Bounded API-side client for the internal-only ASR worker."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx

from .security import safe_error_trace


DEFAULT_PIPELINE_TIMEOUT_SECONDS = 135.0
MAX_ATTEMPTS = 3


def _pipeline_timeout() -> float:
    try:
        value = float(os.environ.get("MINGJIAN_ASR_PIPELINE_TIMEOUT_SECONDS", str(DEFAULT_PIPELINE_TIMEOUT_SECONDS)))
    except ValueError:
        return DEFAULT_PIPELINE_TIMEOUT_SECONDS
    return value if 15.0 <= value <= 180.0 else DEFAULT_PIPELINE_TIMEOUT_SECONDS


def _empty_result(limitation: str, *, error_trace: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": "whisper-asr-result-v1",
        "mock": False,
        "status": "unavailable",
        "transcript": "",
        "language": "unknown",
        "segments": [],
        "confidence": None,
        "limitations": [limitation],
    }
    if error_trace:
        result["error_trace"] = error_trace
    return result


class RemoteAsrAdapter:
    def __init__(self, data_dir: Path, *, transport: httpx.BaseTransport | None = None) -> None:
        self.data_dir = data_dir.resolve()
        self.url = os.environ.get("MINGJIAN_ASR_WORKER_URL", "").rstrip("/")
        self.timeout_seconds = _pipeline_timeout()
        self.transport = transport

    def describe(self) -> dict[str, Any]:
        return {
            "name": "internal Whisper ASR worker",
            "configured": bool(self.url),
            "raw_media_sent_outside_internal_network": False,
            "timeout_seconds": self.timeout_seconds,
            "max_attempts_on_busy": MAX_ATTEMPTS,
            "system_proxy_bypassed": True,
        }

    def transcribe(self, audio_path: Path) -> dict[str, Any]:
        if not self.url:
            return _empty_result("Internal ASR Worker is not configured.")
        try:
            relative = audio_path.resolve().relative_to(self.data_dir).as_posix()
            if not relative.startswith("derived/") or not relative.endswith(".wav"):
                raise ValueError("ASR input is outside the derived WAV allow-list.")
            timeout = httpx.Timeout(connect=5.0, read=max(10.0, self.timeout_seconds - 2.0), write=5.0, pool=5.0)
            with httpx.Client(timeout=timeout, transport=self.transport, trust_env=False) as client:
                response = self._post_with_busy_retry(client, relative)
            if response.is_error:
                return self._http_error(response)
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("ASR Worker returned non-object JSON.")
            return result
        except Exception as error:
            return _empty_result("Internal ASR Worker is unavailable.", error_trace=safe_error_trace(error))

    def _post_with_busy_retry(self, client: httpx.Client, relative: str) -> httpx.Response:
        response: httpx.Response | None = None
        for attempt in range(MAX_ATTEMPTS):
            response = client.post(f"{self.url}/transcribe", json={"audio_path": relative})
            if response.status_code != 429 or attempt == MAX_ATTEMPTS - 1:
                return response
            try:
                retry_after = float(response.headers.get("Retry-After", "1"))
            except ValueError:
                retry_after = 1.0
            time.sleep(min(2.0, max(0.1, retry_after)))
        assert response is not None
        return response

    @staticmethod
    def _http_error(response: httpx.Response) -> dict[str, Any]:
        detail: Any = None
        try:
            body = response.json()
            detail = body.get("detail") if isinstance(body, dict) else body
        except ValueError:
            detail = response.text.strip()
        if not detail:
            detail = response.reason_phrase or "empty response body"
        trace = safe_error_trace(RuntimeError(f"HTTP {response.status_code}: {detail}"))
        return _empty_result(f"Internal ASR Worker returned HTTP {response.status_code}.", error_trace=trace)
