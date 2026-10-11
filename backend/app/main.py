"""Bounded local media jobs with independent visual and audio evidence."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import socket
import struct
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .preprocess import MediaPreprocessor, PreprocessError, validate_upload_header
from .audio_detector import AstAudioDetector, AudioDetector, AudioResult, RISK_THRESHOLD as AUDIO_RISK_THRESHOLD
from .audio_visual_sync import AudioVisualSyncAdapter, HaarMotionAudioVisualSyncAdapter, MockAudioVisualSyncAdapter, SyncResult
from .face_detection import OpenCvYuNetFaceAdapter
from .aggregate_assessment import AggregateAssessmentBuilder, InternetVerificationBuilder
from .claim_verification import ClaimVerifier, FakeSearchAdapter, TavilySearchAdapter
from .demo_replay import DemoReplayError, DemoReplayStore
from .provenance_verification import ProvenanceVerifier, provenance_axis
from .remote_asr import RemoteAsrAdapter
from .tesseract_ocr import TesseractOcrAdapter
from .llm_analysis import (
    DeepSeekReportAnalysisAdapter,
    DisabledReportAnalysisAdapter,
    ReportAnalysisAdapter,
    build_evidence_packet,
)
from .phase_six import CalibrationRegistry, QualityGateEvaluator, ReviewRuleEngine
from .visual_detector import Meso4VisualDetector, ModelResult, VisualDetector
from .security import configure_redacting_logging, redact_sensitive, validate_public_http_url
from .local_boundary import LocalRequestBoundaryMiddleware


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_MULTIPART_OVERHEAD_BYTES = 64 * 1024
MAX_UPLOAD_BODY_BYTES = MAX_UPLOAD_BYTES + MAX_MULTIPART_OVERHEAD_BYTES
MAX_ACTIVE_JOBS = 8
FINAL_STATES = {"completed", "failed", "cancelled"}
DEFAULT_RETENTION_SECONDS = 15 * 60
DEFAULT_JOB_TIMEOUT_SECONDS = 120.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredSource:
    source_type: str
    object_key: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    url: str | None = None
    declared_source_url: str | None = None
    media_extension: str | None = None
    inferred_mime: str | None = None


class UrlJobRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class JobRepository:
    """SQLite persistence boundary; replaceable by a repository implementation later."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.objects_dir = data_dir / "objects"
        self.database_path = data_dir / "jobs.sqlite3"
        self._lock = threading.Lock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self._initialise()

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialise(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY, status TEXT NOT NULL,
                  source_type TEXT NOT NULL, object_key TEXT, sha256 TEXT,
                  size_bytes INTEGER, source_url TEXT,
                  declared_source_url TEXT,
                  media_extension TEXT, inferred_mime TEXT,
                  report_json TEXT, results_json TEXT, error_code TEXT, error_message TEXT,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  started_at TEXT, completed_at TEXT
                )
                """
            )
            self._add_column_if_missing(connection, "media_extension", "TEXT")
            self._add_column_if_missing(connection, "inferred_mime", "TEXT")
            self._add_column_if_missing(connection, "results_json", "TEXT")
            self._add_column_if_missing(connection, "declared_source_url", "TEXT")
            # An interrupted process cannot leave a job permanently running.
            connection.execute(
                "UPDATE jobs SET status = 'queued', started_at = NULL, updated_at = ? "
                "WHERE status = 'running'",
                (now(),),
            )

    @staticmethod
    def _add_column_if_missing(connection: sqlite3.Connection, name: str, definition: str) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
        if name not in columns:
            connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")

    def create(self, source: StoredSource) -> dict[str, Any]:
        job_id = str(uuid.uuid4())
        timestamp = now()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO jobs (id, status, source_type, object_key, sha256,
                   size_bytes, source_url, declared_source_url, media_extension, inferred_mime, created_at, updated_at)
                   VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    source.source_type,
                    source.object_key,
                    source.sha256,
                    source.size_bytes,
                    source.url,
                    source.declared_source_url,
                    source.media_extension,
                    source.inferred_mime,
                    timestamp,
                    timestamp,
                ),
            )
        return self.get(job_id)  # type: ignore[return-value]

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return row_to_job(row) if row else None

    def list(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [row_to_job(row) for row in rows]

    def active_count(self) -> int:
        with self._connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')").fetchone()[0])

    def claim_next(self) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            timestamp = now()
            updated = connection.execute(
                "UPDATE jobs SET status = 'running', started_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (timestamp, timestamp, row["id"]),
            ).rowcount
            if not updated:
                return None
            claimed = connection.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        return row_to_job(claimed) if claimed else None

    def complete(self, job_id: str, report: dict[str, Any], results: dict[str, Any] | None = None) -> bool:
        timestamp = now()
        with self._connection() as connection:
            updated = connection.execute(
                "UPDATE jobs SET status = 'completed', report_json = ?, results_json = ?, completed_at = ?, "
                "updated_at = ?, error_code = NULL, error_message = NULL "
                "WHERE id = ? AND status = 'running'",
                (json.dumps(report, ensure_ascii=False), json.dumps(results, ensure_ascii=False) if results else None, timestamp, timestamp, job_id),
            ).rowcount
        return bool(updated)

    def refresh_completed_report(self, job_id: str, report: dict[str, Any], results: dict[str, Any]) -> bool:
        """Replace a completed report after an explicit, bounded review refresh."""
        with self._connection() as connection:
            updated = connection.execute(
                "UPDATE jobs SET report_json = ?, results_json = ?, updated_at = ? WHERE id = ? AND status = 'completed'",
                (json.dumps(report, ensure_ascii=False), json.dumps(results, ensure_ascii=False), now(), job_id),
            ).rowcount
        return bool(updated)

    def fail(self, job_id: str, code: str, message: str, results: dict[str, Any] | None = None) -> bool:
        timestamp = now()
        with self._connection() as connection:
            updated = connection.execute(
                "UPDATE jobs SET status = 'failed', error_code = ?, error_message = ?, results_json = ?, completed_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'running'",
                (code, message, json.dumps(results, ensure_ascii=False) if results else None, timestamp, timestamp, job_id),
            ).rowcount
        return bool(updated)

    def record_cancelled_results(self, job_id: str, results: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE jobs SET results_json = ?, updated_at = ? WHERE id = ? AND status = 'cancelled'",
                (json.dumps(results, ensure_ascii=False), now(), job_id),
            )

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            exists = connection.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not exists:
                return None
            connection.execute(
                "UPDATE jobs SET status = 'cancelled', updated_at = ?, completed_at = ? "
                "WHERE id = ? AND status IN ('queued', 'running')",
                (now(), now(), job_id),
            )
        return self.get(job_id)

    def retry(self, job_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                return None
            if row["status"] not in {"failed", "cancelled"}:
                raise ValueError("Only failed or cancelled jobs can be retried.")
            connection.execute(
                """UPDATE jobs SET status = 'queued', report_json = NULL, results_json = NULL, error_code = NULL,
                   error_message = NULL, started_at = NULL, completed_at = NULL, updated_at = ?
                   WHERE id = ?""",
                (now(), job_id),
            )
        return self.get(job_id)

    def delete(self, job_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute("SELECT object_key FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                return False
            # A running job must be cancelled before its metadata is removed.
            if connection.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0] == "running":
                raise ValueError("A running job must be cancelled before deletion.")
            object_key = row["object_key"]
            connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            self._delete_job_files(connection, job_id, object_key)
        return True

    def purge_expired(self, retention_seconds: int, *, reference_time: float | None = None) -> int:
        """Actively delete final jobs and all unreferenced user-media bytes.

        Queue/running jobs are deliberately retained so a live task cannot lose
        its input.  This is invoked at service start and periodically by the
        worker, rather than relying on a manual delete endpoint alone.
        """
        if retention_seconds < 60 or retention_seconds > 86_400:
            raise ValueError("retention_seconds must be between 60 seconds and 24 hours.")
        effective_time = time.time() if reference_time is None else reference_time
        cutoff = datetime.fromtimestamp(effective_time - retention_seconds, timezone.utc).isoformat()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, object_key FROM jobs WHERE status IN ('completed', 'failed', 'cancelled') "
                "AND completed_at IS NOT NULL AND completed_at < ?",
                (cutoff,),
            ).fetchall()
            for row in rows:
                connection.execute("DELETE FROM jobs WHERE id = ?", (row["id"],))
                self._delete_job_files(connection, row["id"], row["object_key"])
        return len(rows)

    def _delete_job_files(self, connection: sqlite3.Connection, job_id: str, object_key: str | None) -> None:
        if object_key:
            references = connection.execute("SELECT COUNT(*) FROM jobs WHERE object_key = ?", (object_key,)).fetchone()[0]
            if not references:
                path = (self.data_dir / object_key).resolve()
                try:
                    path.relative_to(self.data_dir.resolve())
                except ValueError:
                    path = self.data_dir / "_invalid_object_key"
                if path.is_file():
                    path.unlink()
        derived = (self.data_dir / "derived" / job_id).resolve()
        try:
            derived.relative_to((self.data_dir / "derived").resolve())
        except ValueError:
            return
        if derived.is_dir():
            import shutil
            shutil.rmtree(derived)

    def store_upload(self, upload: UploadFile, *, declared_source_url: str | None = None) -> StoredSource:
        suffix = Path(upload.filename or "").suffix.lower()
        content_type = (upload.content_type or "").lower()

        # The supplied filename is used only to identify a declared extension;
        # it never becomes a filesystem component or object key.
        fd, temporary_name = tempfile.mkstemp(prefix="upload-", suffix=".part", dir=self.data_dir)
        digest = hashlib.sha256()
        size = 0
        header = bytearray()
        try:
            with os.fdopen(fd, "wb") as temporary_file:
                while chunk := upload.file.read(64 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, f"File exceeds the {MAX_UPLOAD_BYTES} byte limit.")
                    digest.update(chunk)
                    temporary_file.write(chunk)
                    if len(header) < 4096:
                        header.extend(chunk[:4096 - len(header)])
            if size == 0:
                raise HTTPException(422, "Empty files are not accepted.")
            try:
                media_magic, inferred_mime = validate_upload_header(suffix, content_type, bytes(header))
            except PreprocessError as error:
                raise HTTPException(415, str(error)) from error
            sha256 = digest.hexdigest()
            object_key = f"objects/{sha256[:2]}/{sha256}"
            destination = self.data_dir / object_key
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                os.unlink(temporary_name)
            else:
                os.replace(temporary_name, destination)
            return StoredSource("file", object_key, sha256, size, declared_source_url=declared_source_url, media_extension=suffix, inferred_mime=inferred_mime)
        except Exception:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
            raise
        finally:
            upload.file.close()


class MockReporter:
    """Explicitly non-analytical report producer behind a replaceable worker boundary."""

    def __init__(self, failures_remaining: int = 0) -> None:
        self.failures_remaining = failures_remaining

    def create(self, job: dict[str, Any], preprocess: dict[str, Any] | None = None, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("Configured mock worker failure for test coverage.")
        axes = {
            "authenticity": "媒体真实性",
            "content": "内容可信度",
            "provenance": "来源可靠性",
        }
        return {
            "mock": True,
            "disclaimer": "阶段 2 Mock 报告：未运行深伪检测、内容核验或来源检索模型。",
            "generated_at": now(),
            "preprocess": preprocess or {"status": "not_run"},
            "axes": {
                name: {
                    "name": label,
                    "mock": True,
                    "conclusion": "无法判断",
                    "summary": "Mock 占位结果，不代表真实分析或风险结论。",
                    "evidence": [],
                    "limitations": "阶段 2 Mock 路径未接入任何深伪检测、内容核验或来源检索模型。",
                }
                for name, label in axes.items()
            },
        }


class PhaseFourReporter:
    """Builds a three-axis report with independent visual and audio evidence."""

    def __init__(self, data_dir: Path, detector: VisualDetector, audio_detector: AudioDetector | None = None, sync_adapter: AudioVisualSyncAdapter | None = None) -> None:
        self.data_dir = data_dir.resolve()
        self.detector = detector
        self.audio_detector = audio_detector or AstAudioDetector(self.data_dir)
        self.sync_adapter = sync_adapter or HaarMotionAudioVisualSyncAdapter(self.data_dir)

    def create(self, job: dict[str, Any], preprocess: dict[str, Any] | None = None, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        cancelled = cancelled or (lambda: False)
        visual = self.detector.detect(job, preprocess, cancelled)
        audio = self.audio_detector.detect(job, preprocess, cancelled)
        vad = self._artifact_json(preprocess, "vad")
        sync = self.sync_adapter.analyse(job, preprocess, vad, cancelled)
        if preprocess is not None:
            self._persist_visual_result(job, preprocess, visual)
            self._persist_result(job, preprocess, "audio_detection", audio)
            self._persist_result(job, preprocess, "audio_visual_sync", sync)
        authenticity = self._authenticity_axis(visual, audio, sync)
        return {
            "mock": False,
            "partial_mock_axes": ["content", "provenance"],
            "disclaimer": "阶段 4 独立记录 Meso4 视觉信号、AST 音频反欺骗信号与单脸下半区运动/音频能量同步筛查信号；所有分数均未经本项目数据校准，不是真实性概率。同步筛查条件不足会明确不可用，且不单独作出音画一致或不一致的事实断言；内容可信度与来源可靠性仍为 Mock 占位结果。",
            "generated_at": now(),
            "preprocess": preprocess or {"status": "not_run"},
            "visual_model": visual,
            "audio_model": audio,
            "audio_visual_sync": sync,
            "axes": {
                "authenticity": authenticity,
                "content": self._mock_axis("内容可信度", "未接入事实核查或证据检索模型。"),
                "provenance": self._mock_axis("来源可靠性", "未接入来源溯源或传播链模型。"),
            },
        }

    def _persist_visual_result(self, job: dict[str, Any], preprocess: dict[str, Any], visual: ModelResult) -> None:
        self._persist_result(job, preprocess, "visual_detection", visual)

    def _persist_result(self, job: dict[str, Any], preprocess: dict[str, Any], name: str, value: dict[str, Any]) -> None:
        output = self.data_dir / "derived" / job["id"] / f"{name}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        relative = output.resolve().relative_to(self.data_dir).as_posix()
        artifacts = preprocess.setdefault("artifacts", [])
        artifacts[:] = [item for item in artifacts if item.get("name") != name]
        artifacts.append({"name": name, "path": relative, "size_bytes": output.stat().st_size})

    def _artifact_json(self, preprocess: dict[str, Any] | None, name: str) -> dict[str, Any]:
        artifact = next((item for item in (preprocess or {}).get("artifacts", []) if item.get("name") == name), None)
        if not artifact or not isinstance(artifact.get("path"), str):
            return {"status": "unavailable", "segments": []}
        try:
            path = (self.data_dir / artifact["path"]).resolve()
            path.relative_to(self.data_dir)
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"status": "unavailable", "segments": []}
        except (OSError, ValueError, json.JSONDecodeError):
            return {"status": "unavailable", "segments": []}

    @staticmethod
    def _authenticity_axis(visual: ModelResult, audio: AudioResult, sync: SyncResult) -> dict[str, Any]:
        visual_completed = visual["status"] == "completed" and visual["video_risk"]
        audio_completed = audio["status"] == "completed" and audio["whole_audio_risk"]
        if not visual_completed and not audio_completed:
            return {
                "name": "媒体真实性",
                "mock": False,
                "status": "unavailable" if audio["status"] != "not_applicable" else visual["status"],
                "conclusion": "无法判断",
                "summary": "视觉与音频基线均未完成可用风险推理；无音频、不适用、不可用、取消或超时不表示低风险。",
                "evidence": [],
                "limitations": [*visual["limitations"], *audio["limitations"], *sync["limitations"]],
            }
        evidence: list[dict[str, Any]] = []
        summaries: list[str] = []
        high_risk = False
        if visual_completed:
            visual_risk = float(visual["video_risk"]["risk_score"])
            visual_threshold = float(visual["frame_risks"][0]["risk_threshold"])
            high_risk = high_risk or visual_risk >= visual_threshold
            summaries.append(f"Meso4 视频聚合分数 {visual_risk:.6f}（阈值 {visual_threshold:.2f}，未经校准）")
            evidence.extend([visual["evidence"]["highest_risk_frame"], *visual["evidence"]["suspicious_segments"]])
        if audio_completed:
            audio_risk = float(audio["whole_audio_risk"]["risk_score"])
            high_risk = high_risk or audio_risk >= AUDIO_RISK_THRESHOLD
            summaries.append(f"AST 整段音频聚合分数 {audio_risk:.6f}（阈值 {AUDIO_RISK_THRESHOLD:.2f}，未经校准）")
            evidence.extend([audio["evidence"]["highest_risk_segment"], *audio["evidence"]["suspicious_segments"]])
        conclusion = "存在需复核的合成或篡改风险" if high_risk else "未观察到已完成模态中达到配置阈值的风险信号"
        return {
            "name": "媒体真实性",
            "mock": False,
            "status": "completed",
            "conclusion": conclusion,
            "summary": "；".join(summaries) + "。模态信号独立展示，不合成为真实性概率。",
            "evidence": evidence,
            "limitations": [*visual["limitations"], *audio["limitations"], *sync["limitations"]],
        }

    @staticmethod
    def _mock_axis(name: str, limitation: str) -> dict[str, Any]:
        return {
            "name": name,
            "mock": True,
            "conclusion": "无法判断",
            "summary": "Mock 占位结果，不代表真实分析或风险结论。",
            "evidence": [],
            "limitations": limitation,
        }


class PhaseFiveReporter(PhaseFourReporter):
    """Adds claim verification and an optional, separate LLM review aid."""

    def __init__(self, data_dir: Path, detector: VisualDetector, audio_detector: AudioDetector | None = None, sync_adapter: AudioVisualSyncAdapter | None = None, claim_verifier: ClaimVerifier | None = None, report_analyser: ReportAnalysisAdapter | None = None, provenance_verifier: ProvenanceVerifier | None = None, enrichment_claim_verifier: ClaimVerifier | None = None, enrichment_report_analyser: ReportAnalysisAdapter | None = None, demo_replay_store: DemoReplayStore | None = None) -> None:
        super().__init__(data_dir, detector, audio_detector, sync_adapter)
        self.claim_verifier = claim_verifier or ClaimVerifier(FakeSearchAdapter())
        self.report_analyser = report_analyser or DisabledReportAnalysisAdapter()
        self.enrichment_claim_verifier = enrichment_claim_verifier or self.claim_verifier
        self.enrichment_report_analyser = enrichment_report_analyser or self.report_analyser
        self.demo_replay_store = demo_replay_store
        self.internet_verification_builder = InternetVerificationBuilder()
        self.provenance_verifier = provenance_verifier or ProvenanceVerifier(self.claim_verifier.search_adapter)

    def create(self, job: dict[str, Any], preprocess: dict[str, Any] | None = None, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        report = super().create(job, preprocess, cancelled)
        asr = self._artifact_json(preprocess, "asr")
        ocr = self._artifact_json(preprocess, "ocr")
        frame_index_value = self._artifact_json_value(preprocess, "frame_index")
        frame_index = frame_index_value if isinstance(frame_index_value, list) else []
        verification = self.claim_verifier.verify(asr, ocr, frame_index)
        source = job.get("source") if isinstance(job.get("source"), dict) else {}
        provenance = self.provenance_verifier.verify(source.get("declared_source_url") or source.get("url"))
        evidence_packet = build_evidence_packet(job, report, asr, ocr, verification, provenance)
        llm_analysis = self.report_analyser.analyse(evidence_packet)
        internet_verification = self.internet_verification_builder.build(verification, llm_analysis)
        if preprocess is not None:
            self._persist_result(job, preprocess, "claim_verification", verification)
            self._persist_result(job, preprocess, "provenance_verification", provenance)
            self._persist_result(job, preprocess, "deepseek_analysis", llm_analysis)
            self._persist_result(job, preprocess, "internet_verification", internet_verification)
        report["partial_mock_axes"] = [name for name, mocked in (("content", verification["mock"]), ("provenance", provenance["mock"])) if mocked]
        retrieval_disclaimer = (
            "本次使用受控 FakeSearchAdapter，未访问网络，引用均明确标记为 mock。"
            if verification["mock"]
            else "本次使用已配置的受限检索 Worker；保存的查询和引用可在主张卡中审计，但不会自动证明主张或来源可靠。"
        )
        report["disclaimer"] = f"阶段 5 在媒体真实性轴之外，独立执行可审计的主张提取、查询生成、检索、引用抽取与状态判断。{retrieval_disclaimer} 暂无证据不等于虚假；来源可靠性仍独立处理。"
        report["claim_verification"] = verification
        report["provenance_verification"] = provenance
        report["llm_analysis"] = llm_analysis
        report["internet_verification"] = internet_verification
        report["axes"]["content"] = self._content_axis(verification)
        report["axes"]["provenance"] = provenance_axis(provenance)
        return report

    def refresh_llm_analysis(self, job: dict[str, Any], report: dict[str, Any], preprocess: dict[str, Any]) -> dict[str, Any]:
        """Regenerate only the LLM review aid for an already completed report."""
        asr = self._artifact_json(preprocess, "asr")
        ocr = self._artifact_json(preprocess, "ocr")
        frame_index_value = self._artifact_json_value(preprocess, "frame_index")
        frame_index = frame_index_value if isinstance(frame_index_value, list) else []
        active_verifier = self.enrichment_claim_verifier
        active_analyser = self.enrichment_report_analyser
        replay_session = None
        if self.demo_replay_store is not None:
            claims, _ = active_verifier.extractor.extract(asr, ocr, frame_index)
            query_texts = [
                query["text"]
                for claim in claims
                for query in active_verifier.query_builder.build(claim)
            ]
            source = job.get("source") if isinstance(job.get("source"), dict) else {}
            replay_session = self.demo_replay_store.bind(source.get("sha256"), claims, query_texts)
            active_verifier = ClaimVerifier(
                replay_session.search_adapter,
                extractor=active_verifier.extractor,
                query_builder=active_verifier.query_builder,
                evidence_extractor=active_verifier.evidence_extractor,
            )
            active_analyser = replay_session.report_analyser
        verification = active_verifier.verify(asr, ocr, frame_index)
        report["claim_verification"] = verification
        report["axes"]["content"] = self._content_axis(verification)
        source = job.get("source") if isinstance(job.get("source"), dict) else {}
        provenance = ProvenanceVerifier(active_verifier.search_adapter).verify(source.get("declared_source_url") or source.get("url"))
        evidence_packet = build_evidence_packet(job, report, asr, ocr, verification, provenance)
        llm_analysis = active_analyser.analyse(evidence_packet)
        internet_verification = self.internet_verification_builder.build(verification, llm_analysis)
        if replay_session is not None:
            internet_verification["replayed"] = True
            internet_verification["live_retrieval_performed"] = False
            internet_verification["replay_sample_id"] = replay_session.sample_id
        self._persist_result(job, preprocess, "claim_verification", verification)
        self._persist_result(job, preprocess, "provenance_verification", provenance)
        self._persist_result(job, preprocess, "deepseek_analysis", llm_analysis)
        self._persist_result(job, preprocess, "internet_verification", internet_verification)
        report["provenance_verification"] = provenance
        report["axes"]["provenance"] = provenance_axis(provenance)
        report["llm_analysis"] = llm_analysis
        report["internet_verification"] = internet_verification
        report["preprocess"] = preprocess
        return report

    def _artifact_json_value(self, preprocess: dict[str, Any] | None, name: str) -> Any:
        artifact = next((item for item in (preprocess or {}).get("artifacts", []) if item.get("name") == name), None)
        if not artifact or not isinstance(artifact.get("path"), str):
            return None
        try:
            path = (self.data_dir / artifact["path"]).resolve()
            path.relative_to(self.data_dir)
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _content_axis(verification: dict[str, Any]) -> dict[str, Any]:
        claims = verification["claims"]
        citations = [citation for claim in claims for citation in claim["citations"]]
        statuses = {claim["status_label"] for claim in claims}
        conclusion = "无法判断" if not claims else "；".join(sorted(statuses))
        summary = "未从可定位的 ASR/OCR 文本中提取主张；这不表示内容低风险或真实。" if not claims else f"已独立处理 {len(claims)} 条主张，所有结论均保留原文、时间戳、查询与引用链。"
        return {
            "name": "内容可信度",
            "mock": verification["mock"],
            "status": verification["status"],
            "conclusion": conclusion,
            "summary": summary,
            "evidence": citations,
            "claims": claims,
            "limitations": verification["limitations"],
        }


class PhaseSixReporter(PhaseFiveReporter):
    """Adds calibration provenance, quality states, and non-enforcement review rules."""

    def __init__(self, data_dir: Path, detector: VisualDetector, audio_detector: AudioDetector | None = None, sync_adapter: AudioVisualSyncAdapter | None = None, claim_verifier: ClaimVerifier | None = None, calibration_registry: CalibrationRegistry | None = None, report_analyser: ReportAnalysisAdapter | None = None, provenance_verifier: ProvenanceVerifier | None = None, enrichment_claim_verifier: ClaimVerifier | None = None, enrichment_report_analyser: ReportAnalysisAdapter | None = None, demo_replay_store: DemoReplayStore | None = None) -> None:
        super().__init__(data_dir, detector, audio_detector, sync_adapter, claim_verifier, report_analyser, provenance_verifier, enrichment_claim_verifier, enrichment_report_analyser, demo_replay_store)
        self.calibration_registry = calibration_registry or CalibrationRegistry()
        self.quality_evaluator = QualityGateEvaluator()
        self.rule_engine = ReviewRuleEngine()
        self.aggregate_builder = AggregateAssessmentBuilder()

    def create(self, job: dict[str, Any], preprocess: dict[str, Any] | None = None, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        report = super().create(job, preprocess, cancelled)
        visual, audio, sync = report["visual_model"], report["audio_model"], report["audio_visual_sync"]
        visual_score = (visual.get("video_risk") or {}).get("risk_score")
        audio_score = (audio.get("whole_audio_risk") or {}).get("risk_score")
        calibration = {
            "visual": self.calibration_registry.apply("visual.meso4.fake_score", visual_score),
            "audio": self.calibration_registry.apply("audio.ast.fake_score", audio_score),
            "sync": self.calibration_registry.apply("sync.haar_motion_anomaly", sync.get("risk_score")),
        }
        if visual.get("video_risk") is not None:
            visual["video_risk"]["calibration"] = calibration["visual"]
        if audio.get("whole_audio_risk") is not None:
            audio["whole_audio_risk"]["calibration"] = calibration["audio"]
        if sync.get("risk_score") is not None:
            sync["calibration"] = calibration["sync"]
        faces = self._artifact_json(preprocess, "faces")
        vad = self._artifact_json(preprocess, "vad")
        quality = self.quality_evaluator.evaluate(preprocess, faces, vad, visual, audio, sync)
        decision = self.rule_engine.decide(report, quality, calibration)
        aggregate_assessment = self.aggregate_builder.build(report)
        if preprocess is not None:
            self._persist_result(job, preprocess, "phase6_quality", quality)
            self._persist_result(job, preprocess, "phase6_recommendation", decision)
            self._persist_result(job, preprocess, "aggregate_assessment", aggregate_assessment)
        report["phase6"] = {"calibration": calibration, "quality": quality, "recommendation": decision}
        report["aggregate_assessment"] = aggregate_assessment
        report["disclaimer"] = "阶段 6 仅在独立三轴之外保存冻结校准工件、输入质量状态、联网属实性核验与可审计复核建议。统一公式分数是政策复核风险指数，不是真实性概率；缺失、不可用、分布外或 Mock 轴不会被解释为低风险。建议不会自动封禁、认定违法或输出单一真/假定论。"
        return report

    def refresh_llm_analysis(self, job: dict[str, Any], report: dict[str, Any], preprocess: dict[str, Any]) -> dict[str, Any]:
        report = super().refresh_llm_analysis(job, report, preprocess)
        aggregate_assessment = self.aggregate_builder.build(report)
        self._persist_result(job, preprocess, "aggregate_assessment", aggregate_assessment)
        report["aggregate_assessment"] = aggregate_assessment
        report["preprocess"] = preprocess
        return report


# Compatibility name for consumers which imported the phase-3 reporter.  The
# constructor accepts the old two arguments and gains optional phase-4 adapters.
PhaseThreeReporter = PhaseFourReporter


class JobWorker:
    """Small recoverable local worker; callers depend on this interface, not a queue product."""

    def __init__(
        self,
        repository: JobRepository,
        reporter: Any,
        preprocessor: MediaPreprocessor,
        delay_seconds: float = 0.2,
        *,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
        job_timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS,
    ) -> None:
        self.repository = repository
        self.reporter = reporter
        self.preprocessor = preprocessor
        self.delay_seconds = delay_seconds
        self.retention_seconds = retention_seconds
        self.job_timeout_seconds = job_timeout_seconds
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="mingjian-media-authenticity-worker", daemon=True)
        self._thread.start()

    def notify(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        next_cleanup = 0.0
        while not self._stop.is_set():
            if time.monotonic() >= next_cleanup:
                try:
                    deleted = self.repository.purge_expired(self.retention_seconds)
                    if deleted:
                        logger.info("retention cleanup deleted %s expired jobs", deleted)
                except Exception as error:
                    logger.warning("retention cleanup failed: %s", redact_sensitive(error))
                next_cleanup = time.monotonic() + 30.0
            job = self.repository.claim_next()
            if not job:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            if self._stop.wait(self.delay_seconds):
                return
            deadline = time.monotonic() + self.job_timeout_seconds
            def stopped() -> bool:
                return self._stop.is_set() or time.monotonic() >= deadline or (self.repository.get(job["id"]) or {}).get("status") == "cancelled"
            try:
                results = self.preprocessor.process(job, stopped)
                report = self.reporter.create(job, results, stopped)
                if time.monotonic() >= deadline:
                    self.repository.fail(job["id"], "job_timeout", "The job exceeded its total time limit.", results)
                    continue
                if not self.repository.complete(job["id"], report, results):
                    self.repository.record_cancelled_results(job["id"], results)
            except PreprocessError as error:
                if time.monotonic() >= deadline:
                    self.repository.fail(job["id"], "job_timeout", "The job exceeded its total time limit.", error.result)
                    continue
                if error.code == "preprocess_cancelled":
                    self.repository.record_cancelled_results(job["id"], error.result or {})
                else:
                    if not self.repository.fail(job["id"], error.code, str(error), error.result):
                        self.repository.record_cancelled_results(job["id"], error.result or {})
            except Exception as error:  # persisted failure is visible and retryable
                self.repository.fail(job["id"], "mock_worker_error", redact_sensitive(error))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    source: dict[str, Any] = {"type": row["source_type"]}
    if row["source_type"] == "file":
        source.update({"object_key": row["object_key"], "sha256": row["sha256"], "size_bytes": row["size_bytes"]})
        if row["declared_source_url"]:
            source["declared_source_url"] = row["declared_source_url"]
        if row["media_extension"]:
            source["media_extension"] = row["media_extension"]
        if row["inferred_mime"]:
            source["inferred_mime"] = row["inferred_mime"]
    else:
        source["url"] = row["source_url"]
    job = {
        "id": row["id"],
        "status": row["status"],
        "source": source,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "error": None,
    }
    if row["error_code"]:
        job["error"] = {"code": row["error_code"], "message": row["error_message"]}
    if row["results_json"]:
        job["results"] = json.loads(row["results_json"])
    return job


def validate_url(value: str) -> str:
    try:
        # URL jobs are offline references. This normalizes hostnames and rejects
        # unsafe local targets without resolving or fetching any network media.
        return validate_public_http_url(value)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


def _bounded_env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "false").strip().lower() in {"1", "true", "yes", "on"}


def _container_gateway_ips() -> set[str]:
    """Return only the container's explicit default-route gateway(s).

    The Compose port is bound to 127.0.0.1 on the host. Docker Desktop may
    nevertheless present that host request inside the container as the exact
    bridge gateway. We trust only those exact addresses in container mode, not
    arbitrary RFC1918 clients.
    """
    in_container = os.environ.get("MINGJIAN_CONTAINER_MODE") == "1" or Path("/.dockerenv").exists()
    if not in_container:
        return set()
    configured = {item.strip() for item in os.environ.get("MINGJIAN_TRUSTED_GATEWAY_IPS", "").split(",") if item.strip()}
    # Compose supplies this exact gateway address. Do not also trust a route
    # gateway when an explicit allow-list exists.
    if configured:
        return configured
    route_path = Path("/proc/net/route")
    try:
        for line in route_path.read_text(encoding="ascii").splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 3 and fields[1] == "00000000":
                configured.add(socket.inet_ntoa(struct.pack("<L", int(fields[2], 16))))
    except (OSError, ValueError, struct.error):
        pass
    return configured


def create_app(
    data_dir: Path | None = None,
    *,
    start_worker: bool = True,
    reporter: MockReporter | None = None,
    preprocessor: MediaPreprocessor | None = None,
    detector: VisualDetector | None = None,
    audio_detector: AudioDetector | None = None,
    sync_adapter: AudioVisualSyncAdapter | None = None,
    claim_verifier: ClaimVerifier | None = None,
    calibration_registry: CalibrationRegistry | None = None,
    test_mode: bool | None = None,
    trusted_gateway_ips: set[str] | None = None,
) -> FastAPI:
    """Construct the API with injectable persistence and worker collaborators for tests."""
    configure_redacting_logging()
    repository = JobRepository(data_dir or Path(__file__).resolve().parents[1] / "data")
    active_detector = detector or Meso4VisualDetector(repository.data_dir)
    active_audio_detector = audio_detector or AstAudioDetector(repository.data_dir)
    flags = {name: _env_enabled(name) for name in ("ENABLE_ASR", "ENABLE_OCR", "ENABLE_FACE", "ENABLE_SYNC", "ENABLE_RETRIEVAL", "ENABLE_LLM")}
    demo_mode = _env_enabled("DEMO_MODE")
    base_verifier = claim_verifier or ClaimVerifier(FakeSearchAdapter())
    # The API has no provider keys.  It may only call the internal worker during
    # the explicit refresh route; retain the old name as a migration fallback.
    retrieval_worker_url = (
        os.environ.get("MINGJIAN_RETRIEVAL_WORKER_URL")
        or os.environ.get("MINGJIAN_RETRIEVAL_URL")
        or "http://127.0.0.1:8005"
    )
    enrichment_verifier = (
        ClaimVerifier(TavilySearchAdapter(retrieval_worker_url, enabled=flags["ENABLE_RETRIEVAL"]))
        if flags["ENABLE_RETRIEVAL"]
        else base_verifier
    )
    enrichment_report_analyser: ReportAnalysisAdapter = (
        DeepSeekReportAnalysisAdapter(os.environ["MINGJIAN_LLM_ANALYSIS_URL"])
        if flags["ENABLE_LLM"] and os.environ.get("MINGJIAN_LLM_ANALYSIS_URL")
        else DisabledReportAnalysisAdapter()
    )
    active_sync = sync_adapter or (HaarMotionAudioVisualSyncAdapter(repository.data_dir) if flags["ENABLE_SYNC"] else MockAudioVisualSyncAdapter())
    demo_replay_store = DemoReplayStore(Path(__file__).resolve().parents[1] / "demo_samples") if demo_mode else None
    active_reporter = reporter or PhaseSixReporter(repository.data_dir, active_detector, active_audio_detector, active_sync, base_verifier, calibration_registry, DisabledReportAnalysisAdapter(), enrichment_claim_verifier=enrichment_verifier, enrichment_report_analyser=enrichment_report_analyser, demo_replay_store=demo_replay_store)
    if preprocessor is not None:
        active_preprocessor = preprocessor
    else:
        asr_adapter = RemoteAsrAdapter(repository.data_dir) if flags["ENABLE_ASR"] and os.environ.get("MINGJIAN_ASR_WORKER_URL") else None
        ocr_adapter = TesseractOcrAdapter(repository.data_dir) if flags["ENABLE_OCR"] else None
        face_adapter = OpenCvYuNetFaceAdapter(repository.data_dir) if flags["ENABLE_FACE"] else None
        active_preprocessor = MediaPreprocessor(repository.data_dir, face=face_adapter, asr=asr_adapter, ocr=ocr_adapter)
    retention_seconds = _bounded_env_int("MINGJIAN_RETENTION_SECONDS", DEFAULT_RETENTION_SECONDS, 60, 86_400)
    job_timeout_seconds = _bounded_env_float("MINGJIAN_JOB_TIMEOUT_SECONDS", DEFAULT_JOB_TIMEOUT_SECONDS, 10.0, 600.0)
    worker = JobWorker(repository, active_reporter, active_preprocessor, retention_seconds=retention_seconds, job_timeout_seconds=job_timeout_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        repository.purge_expired(retention_seconds)
        if start_worker:
            worker.start()
            worker.notify()
        yield
        worker.stop()

    app = FastAPI(
        title="MingJian Web API",
        version="0.9.0",
        description="Phase-9 hardened local web API over the phase-6 analysis stack and independent three-axis evidence.",
        lifespan=lifespan,
    )
    app.state.repository = repository
    app.state.worker = worker
    app.state.visual_detector = active_detector
    app.state.audio_detector = active_audio_detector
    app.state.preprocessor = active_preprocessor
    app.state.retention_seconds = retention_seconds
    app.state.job_timeout_seconds = job_timeout_seconds
    testing = ("PYTEST_CURRENT_TEST" in os.environ) if test_mode is None else test_mode
    allowed_hosts = ["127.0.0.1", "localhost", "[::1]"]
    if testing:
        allowed_hosts.append("testserver")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts, www_redirect=False)
    app.add_middleware(
        LocalRequestBoundaryMiddleware,
        max_upload_body_bytes=MAX_UPLOAD_BODY_BYTES,
        max_concurrent_uploads=2,
        max_state_requests_per_minute=30,
        test_mode=testing,
        trusted_gateway_ips=trusted_gateway_ips if trusted_gateway_ips is not None else _container_gateway_ips(),
    )
    @app.middleware("http")
    async def reject_oversized_multipart(request, call_next):
        if request.url.path == "/api/v1/jobs/upload":
            length = request.headers.get("content-length")
            if length:
                try:
                    if int(length) > MAX_UPLOAD_BYTES + MAX_MULTIPART_OVERHEAD_BYTES:
                        return JSONResponse(status_code=413, content={"detail": f"Upload exceeds the {MAX_UPLOAD_BYTES} byte limit."})
                except ValueError:
                    return JSONResponse(status_code=400, content={"detail": "Upload Content-Length is invalid."})
        return await call_next(request)
    cors_origins = [item.strip() for item in os.environ.get("MINGJIAN_CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173").split(",") if item.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )

    @app.get("/health", tags=["system"])
    def health() -> dict[str, Any]:
        sync_description = active_reporter.sync_adapter.describe() if isinstance(active_reporter, PhaseFourReporter) else {"injected": True}
        return {
            "status": "ok",
            "service": "mingjian-web-api",
            "phase": "11-real-content-source-verification-in-progress",
            "visual_detector": active_detector.describe() if isinstance(active_detector, Meso4VisualDetector) else {"injected": True},
            "audio_detector": active_audio_detector.describe() if isinstance(active_audio_detector, AstAudioDetector) else {"injected": True},
            "audio_visual_sync": sync_description,
            "claim_verification": {
                "search_adapter": type(active_reporter.claim_verifier.search_adapter).__name__,
                "refresh_search_adapter": type(active_reporter.enrichment_claim_verifier.search_adapter).__name__,
                "retrieval_worker_url": retrieval_worker_url if flags["ENABLE_RETRIEVAL"] else None,
                "untrusted_text_policy": "data_only_tool_allowlist",
            } if isinstance(active_reporter, PhaseFiveReporter) else {"injected": True},
            "llm_report_analysis": {
                "refresh_adapter": type(active_reporter.enrichment_report_analyser).__name__,
                "refresh_only": True,
                "raw_media_sent": False,
            } if isinstance(active_reporter, PhaseFiveReporter) else {"injected": True},
            "asr": active_preprocessor.asr.describe() if hasattr(active_preprocessor.asr, "describe") else {"adapter": type(active_preprocessor.asr).__name__},
            "ocr": active_preprocessor.ocr.describe() if hasattr(active_preprocessor.ocr, "describe") else {"adapter": type(active_preprocessor.ocr).__name__},
            "aggregate_assessment": active_reporter.aggregate_builder.describe() if isinstance(active_reporter, PhaseSixReporter) else {"injected": True},
            "calibration": {"visual": active_reporter.calibration_registry.describe("visual.meso4.fake_score"), "audio": active_reporter.calibration_registry.describe("audio.ast.fake_score"), "sync": active_reporter.calibration_registry.describe("sync.haar_motion_anomaly")} if isinstance(active_reporter, PhaseSixReporter) else {"injected": True},
            "recommendation_ruleset": "phase6-rules-v1" if isinstance(active_reporter, PhaseSixReporter) else "injected",
            "security": {"upload_max_bytes": MAX_UPLOAD_BYTES, "url_fetch": "disabled_by_default", "retention_seconds": retention_seconds, "job_timeout_seconds": job_timeout_seconds},
            "feature_flags": flags,
        }

    @app.post("/api/v1/jobs", status_code=status.HTTP_201_CREATED, tags=["jobs"])
    def create_url_job(payload: UrlJobRequest) -> dict[str, Any]:
        if repository.active_count() >= MAX_ACTIVE_JOBS:
            raise HTTPException(429, "Too many active jobs; wait for an existing task to finish.")
        job = repository.create(StoredSource("url", url=validate_url(payload.url)))
        worker.notify()
        return job

    @app.post("/api/v1/jobs/upload", status_code=status.HTTP_201_CREATED, tags=["jobs"])
    def create_upload_job(
        file: UploadFile = File(...),
        source_url: Optional[str] = Form(default=None, max_length=2048),
    ) -> dict[str, Any]:
        if repository.active_count() >= MAX_ACTIVE_JOBS:
            raise HTTPException(429, "Too many active jobs; wait for an existing task to finish.")
        declared_source_url = validate_url(source_url) if source_url and source_url.strip() else None
        job = repository.create(repository.store_upload(file, declared_source_url=declared_source_url))
        worker.notify()
        return job

    @app.get("/api/v1/jobs", tags=["jobs"])
    def list_jobs() -> dict[str, list[dict[str, Any]]]:
        return {"items": repository.list()}

    @app.get("/api/v1/jobs/{job_id}", tags=["jobs"])
    def get_job(job_id: str) -> dict[str, Any]:
        return require_job(repository, job_id)

    @app.get("/api/v1/jobs/{job_id}/report", tags=["jobs"])
    def get_report(job_id: str) -> dict[str, Any]:
        job = require_job(repository, job_id)
        if job["status"] != "completed":
            raise HTTPException(409, "A report is available only after the job completes.")
        with repository._connection() as connection:
            row = connection.execute("SELECT report_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return json.loads(row["report_json"])

    @app.post("/api/v1/jobs/{job_id}/refresh-llm", tags=["jobs"])
    def refresh_llm_analysis(job_id: str) -> dict[str, Any]:
        """Explicitly regenerate the optional LLM review without reprocessing media."""
        job = require_job(repository, job_id)
        if job["status"] != "completed":
            raise HTTPException(409, "LLM analysis can be refreshed only after the job completes.")
        if not isinstance(active_reporter, PhaseFiveReporter):
            raise HTTPException(409, "This report producer has no LLM analysis boundary.")
        logger.info(
            "refresh-llm requested (llm_enabled=%s, llm_adapter=%s, retrieval_enabled=%s).",
            flags["ENABLE_LLM"],
            type(active_reporter.enrichment_report_analyser).__name__,
            flags["ENABLE_RETRIEVAL"],
        )
        preprocess = job.get("results")
        if not isinstance(preprocess, dict):
            raise HTTPException(409, "The saved preprocessing result is unavailable.")
        with repository._connection() as connection:
            row = connection.execute("SELECT report_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row or not isinstance(row["report_json"], str):
            raise HTTPException(409, "The saved report is unavailable.")
        try:
            report = json.loads(row["report_json"])
        except json.JSONDecodeError as error:
            raise HTTPException(409, "The saved report cannot be read.") from error
        try:
            refreshed = active_reporter.refresh_llm_analysis(job, report, preprocess)
        except DemoReplayError as error:
            raise HTTPException(409, f"Demo replay failed: {error}") from error
        if not repository.refresh_completed_report(job_id, refreshed, preprocess):
            raise HTTPException(409, "The report state changed before it could be refreshed.")
        return refreshed

    @app.get("/api/v1/jobs/{job_id}/artifacts/{artifact_name}", tags=["jobs"])
    def get_report_artifact(job_id: str, artifact_name: str) -> Any:
        """Return an allow-listed JSON audit artifact referenced by this job.

        The report UI needs ASR/OCR records to explain their differences.  It
        deliberately cannot address arbitrary files or stderr logs by path.
        """
        job = require_job(repository, job_id)
        allowed = {"metadata", "shot_list", "frame_index", "faces", "vad", "asr", "ocr", "visual_detection", "audio_detection", "audio_visual_sync", "claim_verification", "provenance_verification", "deepseek_analysis", "internet_verification", "aggregate_assessment", "phase6_quality", "phase6_recommendation"}
        if artifact_name not in allowed:
            raise HTTPException(404, "This artifact is not available through the report API.")
        artifact = next((item for item in (job.get("results") or {}).get("artifacts", []) if item.get("name") == artifact_name), None)
        if not artifact or not isinstance(artifact.get("path"), str):
            raise HTTPException(404, "The requested report artifact is unavailable.")
        path = _safe_report_artifact_path(repository.data_dir, artifact["path"])
        if path.suffix.lower() != ".json" or not path.is_file():
            raise HTTPException(404, "The requested report artifact is unavailable.")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HTTPException(409, "The report artifact cannot be read.") from error

    @app.get("/api/v1/jobs/{job_id}/media", tags=["jobs"])
    def get_normalized_media(job_id: str) -> FileResponse:
        """Serve only the bounded normalized media derivative for local review."""
        job = require_job(repository, job_id)
        artifact = next((item for item in (job.get("results") or {}).get("artifacts", []) if item.get("name") == "normalized_media"), None)
        if not artifact or not isinstance(artifact.get("path"), str):
            raise HTTPException(404, "No normalized media is available for this job.")
        path = _safe_report_artifact_path(repository.data_dir, artifact["path"])
        if not path.is_file():
            raise HTTPException(404, "The normalized media derivative is unavailable.")
        media_type = {".mp4": "video/mp4", ".png": "image/png", ".wav": "audio/wav"}.get(path.suffix.lower(), "application/octet-stream")
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/jobs/{job_id}/frames/{frame_index}", tags=["jobs"])
    def get_keyframe(job_id: str, frame_index: int) -> FileResponse:
        """Serve one report-referenced keyframe, never a caller-supplied path."""
        job = require_job(repository, job_id)
        index_artifact = next((item for item in (job.get("results") or {}).get("artifacts", []) if item.get("name") == "frame_index"), None)
        if not index_artifact or not isinstance(index_artifact.get("path"), str):
            raise HTTPException(404, "No keyframe index is available for this job.")
        index_path = _safe_report_artifact_path(repository.data_dir, index_artifact["path"])
        try:
            frames = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HTTPException(409, "The keyframe index cannot be read.") from error
        frame = next((item for item in frames if isinstance(item, dict) and item.get("index") == frame_index), None) if isinstance(frames, list) else None
        if not frame or not isinstance(frame.get("path"), str):
            raise HTTPException(404, "The requested keyframe is unavailable.")
        path = _safe_report_artifact_path(repository.data_dir, frame["path"])
        if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"} or not path.is_file():
            raise HTTPException(404, "The requested keyframe is unavailable.")
        media_type = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[path.suffix.lower()]
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/jobs/{job_id}/cancel", tags=["jobs"])
    def cancel_job(job_id: str) -> dict[str, Any]:
        job = repository.cancel(job_id)
        if not job:
            raise HTTPException(404, "Job not found.")
        return job

    @app.post("/api/v1/jobs/{job_id}/retry", tags=["jobs"])
    def retry_job(job_id: str) -> dict[str, Any]:
        try:
            job = repository.retry(job_id)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        if not job:
            raise HTTPException(404, "Job not found.")
        worker.notify()
        return job

    @app.delete("/api/v1/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["jobs"])
    def delete_job(job_id: str) -> None:
        try:
            deleted = repository.delete(job_id)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        if not deleted:
            raise HTTPException(404, "Job not found.")

    frontend_dir = Path(__file__).resolve().parents[2] / "frontend"
    if frontend_dir.is_dir():
        # The Compose image serves the reviewed static UI and API from one
        # origin; no development CORS exposure is needed in that deployment.
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
    return app


def require_job(repository: JobRepository, job_id: str) -> dict[str, Any]:
    job = repository.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    return job


def _safe_report_artifact_path(data_dir: Path, relative_path: str) -> Path:
    """Resolve a persisted relative artifact path without allowing traversal."""
    path = (data_dir / relative_path).resolve()
    try:
        path.relative_to(data_dir.resolve())
    except ValueError as error:
        raise HTTPException(404, "The requested report artifact is unavailable.") from error
    return path


app = create_app()
