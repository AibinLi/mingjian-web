from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backend.app.claim_verification import ClaimExtractor, ClaimVerifier, QueryBuilder
from backend.app.demo_replay import (
    REPLAY_SCHEMA,
    DemoReplayError,
    DemoReplayStore,
    TRUSTED_REPLAY_SHA256,
    claim_fingerprint,
)
from backend.app.main import PhaseSixReporter


SOURCE_SHA256 = "a" * 64


def timestamped_asr(text: str = "Example Corp reported 2026 results.") -> dict:
    return {
        "status": "completed",
        "segments": [
            {
                "text": text,
                "start_seconds": 1.0,
                "end_seconds": 2.0,
                "confidence": 0.9,
            }
        ],
    }


def source_record() -> dict:
    body = "Example Corp reported 2026 results in its official release."
    return {
        "url": "https://evidence.example/report",
        "title": "Example Corp report",
        "body": body,
        "excerpt": body,
        "domain": "evidence.example",
        "published_at": None,
        "fetched_at": "2026-07-30T00:00:00+00:00",
        "relation": "background",
        "source_kind": "fetched",
    }


def replay_inputs() -> tuple[list[dict], list[str]]:
    claims, diagnostics = ClaimExtractor().extract(
        timestamped_asr(),
        {"status": "unavailable", "items": []},
        [],
    )
    assert diagnostics == []
    queries = [
        query["text"]
        for claim in claims
        for query in QueryBuilder().build(claim)
    ]
    return claims, queries


def write_sample(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sample_id: str = "fixture-sample",
    source_sha256: str = SOURCE_SHA256,
) -> tuple[DemoReplayStore, list[dict], list[str], Path]:
    claims, queries = replay_inputs()
    sample = {
        "schema_version": REPLAY_SCHEMA,
        "sample_id": sample_id,
        "source_sha256": source_sha256,
        "claim_fingerprint": claim_fingerprint(claims),
        "results_by_query": {text: [source_record()] for text in queries},
        "llm_analysis": {
            "schema_version": "llm-report-analysis-v1",
            "status": "completed",
            "provider": "DeepSeek",
            "model": "fixture",
            "analysis": "Frozen review result.",
            "internet_verification": {
                "claim_results": [
                    {
                        "claim_index": 0,
                        "status": "conflicting",
                        "citation_indexes": [0],
                        "reason": "Frozen evidence is conflicting.",
                    }
                ],
                "summary": "Frozen conflicting evidence.",
            },
            "limitations": ["Fixture replay only."],
        },
    }
    path = tmp_path / sample_id / "replay.json"
    path.parent.mkdir(parents=True)
    raw = json.dumps(sample, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    path.write_bytes(raw)
    monkeypatch.setitem(TRUSTED_REPLAY_SHA256, sample_id, hashlib.sha256(raw).hexdigest())
    return DemoReplayStore(tmp_path), claims, queries, path


def assert_replay_error(code: str, action) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(DemoReplayError, match=f"^{code}$"):
        action()


def test_source_sha_mismatch_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, claims, queries, _ = write_sample(tmp_path, monkeypatch)
    assert_replay_error(
        "demo_replay_sample_not_found_for_source_sha256",
        lambda: store.bind("b" * 64, claims, queries),
    )


def test_replay_file_hash_mismatch_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, claims, queries, path = write_sample(tmp_path, monkeypatch)
    path.write_bytes(path.read_bytes() + b" ")
    assert_replay_error(
        "demo_replay_file_hash_mismatch",
        lambda: store.bind(SOURCE_SHA256, claims, queries),
    )


def test_claim_fingerprint_mismatch_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, claims, queries, _ = write_sample(tmp_path, monkeypatch)
    changed = [{**claim, "original_text": "Changed claim."} for claim in claims]
    assert_replay_error(
        "demo_replay_claim_fingerprint_mismatch",
        lambda: store.bind(SOURCE_SHA256, changed, queries),
    )


def test_query_set_mismatch_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, claims, queries, _ = write_sample(tmp_path, monkeypatch)
    assert_replay_error(
        "demo_replay_query_set_mismatch",
        lambda: store.bind(SOURCE_SHA256, claims, [*queries, "missing query"]),
    )


def test_correct_sample_binds_and_marks_frozen_llm_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, claims, queries, _ = write_sample(tmp_path, monkeypatch)
    session = store.bind(SOURCE_SHA256, claims, queries)

    assert session.sample_id == "fixture-sample"
    assert session.search_adapter.search({"text": queries[0]}) == [source_record()]
    analysis = session.report_analyser.analyse({"ignored": True})
    assert analysis["status"] == "completed"
    assert analysis["replayed"] is True
    assert analysis["live_llm_performed"] is False
    assert analysis["replay_sample_id"] == "fixture-sample"


def test_repository_sample_hash_is_allow_listed() -> None:
    path = Path("backend/demo_samples/iphone20-conflicting-20260730/replay.json")
    raw = path.read_bytes().replace(b"\r\n", b"\n")
    value = json.loads(raw.decode("utf-8"))

    assert hashlib.sha256(raw).hexdigest() == TRUSTED_REPLAY_SHA256[value["sample_id"]]
    assert value["source_sha256"] == "baa814883bda7b2608836765cbb1fee5545e1664534151535405db4bacbed713"
    assert value["claim_fingerprint"] == "0e285e61fc634279c4530df84cb2bfb49b252aae58df1d1199ba361e51f4f5a1"


def test_demo_refresh_e2e_uses_only_replay_and_recalculates_aggregate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, _, _ = write_sample(tmp_path / "samples", monkeypatch)
    folder = tmp_path / "derived" / "demo-e2e"
    folder.mkdir(parents=True)
    (folder / "asr.json").write_text(json.dumps(timestamped_asr()), encoding="utf-8")
    (folder / "ocr.json").write_text(json.dumps({"status": "unavailable", "items": []}), encoding="utf-8")
    (folder / "frame_index.json").write_text("[]", encoding="utf-8")
    preprocess = {
        "status": "completed",
        "artifacts": [
            {"name": "asr", "path": "derived/demo-e2e/asr.json"},
            {"name": "ocr", "path": "derived/demo-e2e/ocr.json"},
            {"name": "frame_index", "path": "derived/demo-e2e/frame_index.json"},
        ],
    }

    class ForbiddenLiveAdapter:
        def search(self, _query):  # type: ignore[no-untyped-def]
            raise AssertionError("live retrieval must not run in demo replay")

        def analyse(self, _packet):  # type: ignore[no-untyped-def]
            raise AssertionError("live LLM must not run in demo replay")

    reporter = PhaseSixReporter(
        tmp_path,
        object(),
        enrichment_claim_verifier=ClaimVerifier(ForbiddenLiveAdapter()),
        enrichment_report_analyser=ForbiddenLiveAdapter(),
        demo_replay_store=store,
    )
    report = {
        "visual_model": {"status": "completed", "video_risk": {"risk_score": 0.5}},
        "audio_model": {"status": "not_applicable", "whole_audio_risk": None},
        "audio_visual_sync": {"status": "not_applicable", "risk_score": None},
        "axes": {"provenance": {"status": "unavailable", "mock": False}},
    }

    refreshed = reporter.refresh_llm_analysis(
        {"id": "demo-e2e", "source": {"sha256": SOURCE_SHA256}},
        report,
        preprocess,
    )

    assert refreshed["llm_analysis"]["status"] == "completed"
    assert refreshed["llm_analysis"]["replayed"] is True
    assert refreshed["internet_verification"]["status"] == "completed"
    assert refreshed["internet_verification"]["risk_score"] == 70.0
    assert refreshed["internet_verification"]["live_retrieval_performed"] is False
    assert refreshed["aggregate_assessment"]["overall_score"] is not None
