"""Integrity-checked replay adapters for the explicit demo enrichment step."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPLAY_SCHEMA = "demo-replay-sample-v1"

# A replay file is trusted only when both its sample id and exact byte hash are
# allow-listed here.  The value is filled from the reviewed repository sample.
TRUSTED_REPLAY_SHA256 = {
    "iphone20-conflicting-20260730": "a30f88acaefd88780363754038bfb0b5db9d11ee5e80c1df876cdefb98dc1c62",
}


class DemoReplayError(RuntimeError):
    """A demo replay could not be selected or failed an integrity check."""


def claim_fingerprint(claims: list[dict[str, Any]]) -> str:
    value = [
        {
            "claim_id": claim.get("claim_id"),
            "original_text": claim.get("original_text"),
            "source_trace": claim.get("source_trace"),
        }
        for claim in claims
        if isinstance(claim, dict)
    ]
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ReplaySearchAdapter:
    """Return frozen fetched records for an already validated query set."""

    def __init__(self, results_by_query: dict[str, list[dict[str, Any]]]) -> None:
        self.results_by_query = copy.deepcopy(results_by_query)

    def search(self, query: dict[str, str]) -> list[dict[str, Any]]:
        text = query.get("text")
        if not isinstance(text, str) or text not in self.results_by_query:
            raise DemoReplayError("demo_replay_query_not_found")
        return copy.deepcopy(self.results_by_query[text])


class ReplayReportAnalysisAdapter:
    """Return the frozen provider response without contacting a live worker."""

    def __init__(self, result: dict[str, Any], sample_id: str) -> None:
        self.result = copy.deepcopy(result)
        self.sample_id = sample_id

    def analyse(self, _packet: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(self.result)
        result["replayed"] = True
        result["live_llm_performed"] = False
        result["replay_sample_id"] = self.sample_id
        result["replayed_at"] = datetime.now(timezone.utc).isoformat()
        return result


@dataclass(frozen=True)
class DemoReplaySession:
    sample_id: str
    search_adapter: ReplaySearchAdapter
    report_analyser: ReplayReportAnalysisAdapter


class DemoReplayStore:
    """Load one reviewed sample by media SHA and fail closed on any mismatch."""

    def __init__(self, samples_dir: Path) -> None:
        self.samples_dir = samples_dir.resolve()

    def bind(
        self,
        source_sha256: Any,
        claims: list[dict[str, Any]],
        query_texts: list[str],
    ) -> DemoReplaySession:
        if not isinstance(source_sha256, str) or len(source_sha256) != 64:
            raise DemoReplayError("demo_replay_source_sha256_missing")

        matched: tuple[str, dict[str, Any]] | None = None
        for path in sorted(self.samples_dir.glob("*/replay.json")):
            sample_id = path.parent.name
            expected_file_hash = TRUSTED_REPLAY_SHA256.get(sample_id)
            if not expected_file_hash:
                continue
            try:
                raw = path.read_bytes()
            except OSError as error:
                raise DemoReplayError("demo_replay_file_unreadable") from error
            # Git may materialise text files with CRLF on Windows.  Hash the
            # repository-normalised bytes so the reviewed sample is portable.
            actual_file_hash = hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()
            if actual_file_hash != expected_file_hash:
                raise DemoReplayError("demo_replay_file_hash_mismatch")
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise DemoReplayError("demo_replay_file_invalid") from error
            if isinstance(value, dict) and value.get("source_sha256") == source_sha256:
                matched = sample_id, value
                break

        if matched is None:
            raise DemoReplayError("demo_replay_sample_not_found_for_source_sha256")
        sample_id, sample = matched
        if sample.get("schema_version") != REPLAY_SCHEMA:
            raise DemoReplayError("demo_replay_schema_unsupported")
        if sample.get("sample_id") != sample_id:
            raise DemoReplayError("demo_replay_sample_id_mismatch")
        if sample.get("claim_fingerprint") != claim_fingerprint(claims):
            raise DemoReplayError("demo_replay_claim_fingerprint_mismatch")

        results_by_query = sample.get("results_by_query")
        llm_analysis = sample.get("llm_analysis")
        if not isinstance(results_by_query, dict) or not isinstance(llm_analysis, dict):
            raise DemoReplayError("demo_replay_payload_invalid")
        if any(text not in results_by_query for text in query_texts):
            raise DemoReplayError("demo_replay_query_set_mismatch")
        if any(not isinstance(records, list) for records in results_by_query.values()):
            raise DemoReplayError("demo_replay_search_records_invalid")
        if llm_analysis.get("status") != "completed":
            raise DemoReplayError("demo_replay_llm_result_not_completed")

        return DemoReplaySession(
            sample_id=sample_id,
            search_adapter=ReplaySearchAdapter(results_by_query),
            report_analyser=ReplayReportAnalysisAdapter(llm_analysis, sample_id),
        )
