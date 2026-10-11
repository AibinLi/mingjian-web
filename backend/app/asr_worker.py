"""Single-concurrency, internal-only Whisper ASR worker."""
from __future__ import annotations

import multiprocessing
import os
import queue as queue_module
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .whisper_asr import WhisperAsrAdapter


DATA_DIR = Path(os.environ.get("MINGJIAN_DATA_DIR", "/app/backend/data")).resolve()
LOCK = threading.BoundedSemaphore(1)


def _timeout_from_env() -> float:
    try:
        value = float(os.environ.get("MINGJIAN_ASR_TIMEOUT_SECONDS", "120"))
    except ValueError:
        return 120.0
    return value if 10.0 <= value <= 125.0 else 120.0


TIMEOUT_SECONDS = _timeout_from_env()
app = FastAPI(title="MingJian ASR Worker")
DESCRIPTOR = WhisperAsrAdapter().describe()


def _unavailable(reason: str, *, error_trace: str | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": "whisper-asr-result-v1",
        "mock": False,
        "status": "unavailable",
        "transcript": "",
        "language": "unknown",
        "segments": [],
        "confidence": None,
        "model": DESCRIPTOR,
        "limitations": [reason],
    }
    if error_trace:
        value["error_trace"] = error_trace
    return value


class Request(BaseModel):
    audio_path: str


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "asr": DESCRIPTOR,
        "concurrency": 1,
        "hard_timeout_seconds": TIMEOUT_SECONDS,
        "subprocess_per_request": True,
    }


def _transcribe_child(audio_path: str, result_queue: Any) -> None:
    result_queue.put(WhisperAsrAdapter().transcribe(Path(audio_path)))


@app.post("/transcribe")
def transcribe(request: Request) -> dict[str, Any]:
    path = (DATA_DIR / request.audio_path).resolve()
    try:
        path.relative_to(DATA_DIR / "derived")
    except ValueError as error:
        raise HTTPException(400, "ASR path is outside the derived allow-list.") from error
    if path.suffix.lower() != ".wav" or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise HTTPException(400, "ASR input is unavailable or invalid.")
    if not LOCK.acquire(blocking=False):
        raise HTTPException(429, "ASR worker is busy; retry later.", headers={"Retry-After": "1"})
    try:
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue(maxsize=1)
        process = context.Process(target=_transcribe_child, args=(str(path), result_queue), daemon=True)
        process.start()
        process.join(timeout=TIMEOUT_SECONDS)
        if process.is_alive():
            process.terminate()
            process.join(timeout=3)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
            return _unavailable(
                f"Whisper ASR exceeded its {TIMEOUT_SECONDS:.0f}-second hard timeout.",
                error_trace=f"TimeoutError: ASR module exceeded {TIMEOUT_SECONDS:.0f} seconds",
            )
        if process.exitcode != 0:
            return _unavailable(
                "Whisper ASR subprocess exited unexpectedly.",
                error_trace=f"ChildProcessError: exitcode={process.exitcode}",
            )
        try:
            result = result_queue.get(timeout=1)
        except queue_module.Empty:
            return _unavailable("Whisper ASR subprocess returned no result.")
        return result if isinstance(result, dict) else _unavailable("Whisper ASR subprocess returned an invalid result.")
    finally:
        LOCK.release()
