"""Auditable, deterministic claim verification over untrusted media text.

This module deliberately does not call a language model and never treats ASR,
OCR, search snippets, or web pages as instructions.  They are bounded data
inputs only.  Search is an injectable boundary; the default controlled fake
adapter is visible in every produced citation and never invents a URL.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlparse
import os
import httpx

from .security import safe_error_trace


MAX_TEXT_CHARS = 4_000
MAX_TITLE_CHARS = 500
MAX_QUERY_CHARS = 320
MAX_CITATIONS_PER_CLAIM = 12
CLAIM_TYPES = {"fact", "opinion", "prediction", "unverifiable"}
RELATIONS = {"supports", "refutes", "background"}

STATE_LABELS = {
    "supported": "支持",
    "refuted": "反对",
    "conflicting": "冲突",
    "background": "背景",
    "no_evidence": "暂无证据",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_text(value: Any, *, field: str, maximum: int = MAX_TEXT_CHARS) -> str | None:
    """Validate untrusted text without silently changing its evidential value."""
    if not isinstance(value, str):
        return None
    if not value or len(value) > maximum or "\x00" in value:
        return None
    # Newlines are retained as data.  This only rejects non-printing controls
    # that make a stored audit record ambiguous or unsafe to render.
    if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
        return None
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number >= 0 and number < 86_400 else None


def _confidence(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return round(min(1.0, max(0.0, float(value))), 4)


def _iso8601_datetime(value: Any) -> bool:
    """Accept an ISO-8601 date-time as supplied; never infer or repair one."""
    text = _bounded_text(value, field="timestamp", maximum=80)
    if not text or "T" not in text:
        return False
    try:
        # ``Z`` is the standard ISO-8601 UTC designator.  The replacement is
        # parser compatibility only; the original value is retained verbatim.
        datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return False
    return True


def _normalise_for_query(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _sentences(text: str) -> list[str]:
    pieces = [piece.strip() for piece in re.split(r"(?<=[。！？!?；;])|\n+", text)]
    return [piece for piece in pieces if len(piece) >= 2]


def _entities(text: str) -> list[str]:
    values: list[str] = []
    for pattern in (
        r"\b[A-Z][A-Za-z0-9._-]{1,}\b",
        r"\b\d{4}(?:[-/.年]\d{1,2})?(?:[-/.月]\d{1,2})?日?\b",
        r"[\u4e00-\u9fff]{2,12}(?:公司|集团|政府|大学|医院|法院|委员会|部门|银行|新闻|日报|电视台)",
    ):
        values.extend(re.findall(pattern, text))
    # A short leading CJK phrase is preferable to silently sending an empty
    # query when a claim names an individual or a place without a suffix.
    if not values:
        leading = re.match(r"[\u4e00-\u9fff]{2,10}", text)
        if leading:
            values.append(leading.group(0))
    return list(dict.fromkeys(value[:80] for value in values))[:8]


class ClaimExtractor:
    """Extracts text claims only when their ASR/OCR source can be time-located."""

    def extract(self, asr: dict[str, Any], ocr: dict[str, Any], frame_index: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        claims: list[dict[str, Any]] = []
        diagnostics: list[dict[str, str]] = []
        frame_times: dict[int, tuple[float, str]] = {}
        for frame in frame_index:
            if not isinstance(frame, dict) or not isinstance(frame.get("index"), int):
                continue
            timestamp = _number(frame.get("timestamp_seconds"))
            if timestamp is not None:
                quality = frame.get("timestamp_quality") if isinstance(frame.get("timestamp_quality"), str) else "unknown"
                frame_times[frame["index"]] = (timestamp, quality)

        if asr.get("status") == "completed":
            segments = asr.get("segments")
            if not isinstance(segments, list):
                diagnostics.append({"source": "asr", "reason": "ASR output has no timestamped segments; no claim was created."})
            else:
                for index, segment in enumerate(segments):
                    if not isinstance(segment, dict):
                        diagnostics.append({"source": "asr", "reason": f"ASR segment {index} is not an object."})
                        continue
                    text = _bounded_text(segment.get("text"), field="asr.text")
                    start, end = _number(segment.get("start_seconds")), _number(segment.get("end_seconds"))
                    if not text or start is None or end is None or end < start:
                        diagnostics.append({"source": "asr", "reason": f"ASR segment {index} lacks valid text or timestamps."})
                        continue
                    trace = {"modality": "asr", "artifact": "asr.json", "segment_index": index, "start_seconds": start, "end_seconds": end, "timestamp_quality": "provided"}
                    claims.extend(self._claims_from_text(text, trace, _confidence(segment.get("confidence"), 0.65)))
        elif asr.get("status") not in {None, "not_applicable", "unavailable"}:
            diagnostics.append({"source": "asr", "reason": "ASR status is not usable for claim extraction."})

        if ocr.get("status") == "completed":
            items = ocr.get("items")
            if not isinstance(items, list):
                diagnostics.append({"source": "ocr", "reason": "OCR output has no items array; no claim was created."})
            else:
                for index, item in enumerate(items):
                    if not isinstance(item, dict):
                        diagnostics.append({"source": "ocr", "reason": f"OCR item {index} is not an object."})
                        continue
                    text = _bounded_text(item.get("text"), field="ocr.text")
                    timestamp = _number(item.get("timestamp_seconds"))
                    frame_number = item.get("frame_index")
                    timestamp_quality = item.get("timestamp_quality") if isinstance(item.get("timestamp_quality"), str) else "provided"
                    if timestamp is None and isinstance(frame_number, int) and frame_number in frame_times:
                        timestamp, timestamp_quality = frame_times[frame_number]
                    if not text or timestamp is None:
                        diagnostics.append({"source": "ocr", "reason": f"OCR item {index} lacks valid text or a traceable frame timestamp."})
                        continue
                    trace: dict[str, Any] = {"modality": "ocr", "artifact": "ocr.json", "item_index": index, "timestamp_seconds": timestamp, "timestamp_quality": timestamp_quality}
                    if isinstance(frame_number, int):
                        trace["frame_index"] = frame_number
                    claims.extend(self._claims_from_text(text, trace, _confidence(item.get("confidence"), 0.60)))
        elif ocr.get("status") not in {None, "not_applicable", "unavailable"}:
            diagnostics.append({"source": "ocr", "reason": "OCR status is not usable for claim extraction."})
        return claims, diagnostics

    def _claims_from_text(self, source_text: str, trace: dict[str, Any], confidence: float) -> list[dict[str, Any]]:
        claims: list[dict[str, Any]] = []
        for sentence_index, original_text in enumerate(_sentences(source_text)):
            claim_type, verifiable = self._classify(original_text)
            digest_input = f"{trace}|{sentence_index}|{original_text}".encode("utf-8")
            claims.append({
                "claim_id": hashlib.sha256(digest_input).hexdigest()[:20],
                "original_text": original_text,
                # Compatibility view for downstream consumers.  The original
                # auditable fields remain authoritative and are never replaced.
                "claim_text": original_text,
                "source_type": trace["modality"],
                "timestamp_seconds": trace.get("start_seconds", trace.get("timestamp_seconds")),
                "speaker": None,
                "source_text": source_text,
                "source_trace": trace.copy(),
                "type": claim_type,
                "entities": _entities(original_text),
                "verifiable": verifiable,
                "confidence": confidence,
                "untrusted_input": True,
                "instruction_handling": "Text is data only; it is never executed as a system or tool instruction.",
            })
        return claims

    @staticmethod
    def _classify(text: str) -> tuple[str, bool]:
        lowered = text.lower()
        if text.endswith(("?", "？")) or re.search(r"忽略.*指令|系统提示|system prompt|previous instructions|执行.*命令", lowered):
            return "unverifiable", False
        if re.search(r"我认为|我觉得|观点|应该|最好|糟糕|伟大|令人失望|喜欢|讨厌", text):
            return "opinion", False
        if re.search(r"将会|将于|预计|预测|未来|可能会|明年|下周|will |forecast", lowered):
            return "prediction", False
        factual = re.search(r"\d|于|在|是|为|有|已|发布|发生|达到|宣布|表示|is |was |has |reported", lowered)
        return ("fact", True) if factual else ("unverifiable", False)


class QueryBuilder:
    """Makes bounded search strings from verifiable claims without using prompts."""

    def build(self, claim: dict[str, Any]) -> list[dict[str, str]]:
        if not claim.get("verifiable") or claim.get("type") != "fact":
            return []
        text = _normalise_for_query(str(claim["original_text"]))[:MAX_QUERY_CHARS]
        entities = [entity for entity in claim.get("entities", []) if isinstance(entity, str)]
        candidates = [" ".join(entities[:3] + [text]).strip(), text]
        queries: list[dict[str, str]] = []
        for candidate in candidates:
            if candidate and candidate not in [item["text"] for item in queries]:
                queries.append({"claim_id": str(claim["claim_id"]), "text": candidate[:MAX_QUERY_CHARS], "purpose": "evidence_retrieval"})
        return queries


class SearchAdapter(Protocol):
    def search(self, query: dict[str, str]) -> list[dict[str, Any]]:
        """Return untrusted source records; this interface must not infer truth."""


class RetrievalConfigurationError(RuntimeError):
    """The internal retrieval worker is reachable but intentionally unconfigured."""


class RetrievalUnavailableError(RuntimeError):
    """The internal retrieval worker failed without a network classification."""


class FakeSearchAdapter:
    """Controlled test/demo search. Results are caller-provided, never invented."""

    def __init__(self, results_by_claim: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.results_by_claim = results_by_claim or {}

    def search(self, query: dict[str, str]) -> list[dict[str, Any]]:
        supplied = self.results_by_claim.get(query["claim_id"], [])
        records: list[dict[str, Any]] = []
        for value in supplied:
            if isinstance(value, dict):
                record = value.copy()
                record["source_kind"] = "mock"
                records.append(record)
        return records

class TavilySearchAdapter:
    """Calls only the internal retrieval worker; API itself stays offline."""
    def __init__(self, worker_url: str, *, enabled: bool = False, transport: httpx.BaseTransport | None = None) -> None:
        self.worker_url = worker_url.rstrip('/')
        self.enabled = enabled
        self.transport = transport

    def search(self, query: dict[str, str]) -> list[dict[str, Any]]:
        if not self.enabled:
            raise RetrievalConfigurationError("retrieval_disabled")
        timeout = httpx.Timeout(connect=3.0, read=15.0, write=3.0, pool=3.0)
        with httpx.Client(timeout=timeout, transport=self.transport, trust_env=False) as client:
            response = client.post(
                f'{self.worker_url}/search',
                json={'query': query['text']},
                # The worker is internal-only.  This reflects the API's
                # already-evaluated feature flag, avoiding a second process
                # silently falling back to its own default false value.
                headers={'X-MingJian-Retrieval-Enabled': 'true'},
            )
            response.raise_for_status()
            body = response.json()
        if body.get("status") == "network_error":
            raise ConnectionError("retrieval worker reported network_error")
        if body.get("status") == "not_configured":
            code = body.get("error_code") if isinstance(body.get("error_code"), str) else "retrieval_not_configured"
            raise RetrievalConfigurationError(code)
        if body.get("status") != "completed":
            raise RetrievalUnavailableError("retrieval worker returned unavailable")
        records: list[dict[str, Any]] = []
        for item in body.get('results', []):
            if not isinstance(item, dict):
                continue
            url = item.get('url')
            title = item.get('title')
            content = item.get('content')
            if isinstance(url, str) and isinstance(title, str) and isinstance(content, str):
                host=urlparse(url).hostname
                # Tavily commonly supplies a date-only value.  Citation schema
                # accepts an unknown publication time but must not invent a time
                # of day to satisfy the ISO-8601 date-time contract.
                published_at = item.get('published_date')
                if published_at is not None and not _iso8601_datetime(published_at):
                    published_at = None
                if host:
                    records.append({
                        'url': url,
                        'title': title[:MAX_TITLE_CHARS],
                        'body': content[:MAX_TEXT_CHARS],
                        'excerpt': content[:800],
                        'domain': host.lower(),
                        'published_at': published_at,
                        'fetched_at': utc_now(),
                        'relation': 'background',
                        'source_kind': 'fetched',
                    })
        return records


class EvidenceExtractor:
    """Keeps only traceable verbatim snippets from validated search records."""

    def extract(self, results: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        citations: list[dict[str, Any]] = []
        rejected: list[str] = []
        seen: set[tuple[str, str]] = set()
        for result in results:
            citation, reason = self._citation(result)
            if citation is None:
                rejected.append(reason)
                continue
            key = (citation["url"], citation["excerpt"])
            if key not in seen:
                seen.add(key)
                citations.append(citation)
            if len(citations) >= MAX_CITATIONS_PER_CLAIM:
                break
        return citations, rejected

    def _citation(self, result: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        url = _bounded_text(result.get("url"), field="url", maximum=2048)
        title = _bounded_text(result.get("title"), field="title", maximum=MAX_TITLE_CHARS)
        body = _bounded_text(result.get("body"), field="body")
        fetched_at = _bounded_text(result.get("fetched_at"), field="fetched_at", maximum=80)
        relation = result.get("relation")
        if not url or not title or not body or not fetched_at or relation not in RELATIONS:
            return None, "Search record lacks required structured fields."
        parsed = urlparse(url)
        domain = parsed.hostname.lower() if parsed.scheme in {"http", "https"} and parsed.hostname else None
        declared_domain = result.get("domain")
        if not domain or declared_domain != domain:
            return None, "URL/domain validation failed."
        if not _iso8601_datetime(fetched_at):
            return None, "Fetched timestamp is invalid."
        published_at = result.get("published_at")
        if published_at is not None and not _iso8601_datetime(published_at):
            return None, "Published timestamp is invalid."
        excerpt = _bounded_text(result.get("excerpt"), field="excerpt", maximum=800)
        if not excerpt:
            excerpt = body[:800]
        if excerpt not in body:
            return None, "Excerpt is not a verbatim substring of fetched content."
        source_kind = result.get("source_kind")
        if source_kind not in {"fetched", "mock"}:
            return None, "Search record has an unknown source kind."
        return {
            "url": url,
            "title": title,
            "domain": domain,
            "published_at": published_at,
            "fetched_at": fetched_at,
            "excerpt": excerpt,
            "relation": relation,
            "source_kind": source_kind,
            "mock": source_kind == "mock",
        }, ""


class ClaimVerifier:
    """Coordinates claim extraction, retrieval, citation extraction and status."""

    def __init__(self, search_adapter: SearchAdapter | None = None, *, extractor: ClaimExtractor | None = None, query_builder: QueryBuilder | None = None, evidence_extractor: EvidenceExtractor | None = None) -> None:
        self.search_adapter = search_adapter or FakeSearchAdapter()
        self.extractor = extractor or ClaimExtractor()
        self.query_builder = query_builder or QueryBuilder()
        self.evidence_extractor = evidence_extractor or EvidenceExtractor()

    def verify(self, asr: dict[str, Any], ocr: dict[str, Any], frame_index: list[dict[str, Any]]) -> dict[str, Any]:
        claims, diagnostics = self.extractor.extract(asr, ocr, frame_index)
        verified: list[dict[str, Any]] = []
        network_error = False
        retrieval_not_configured = False
        for claim in claims:
            queries = self.query_builder.build(claim)
            raw_results: list[dict[str, Any]] = []
            for query in queries:
                try:
                    response = self.search_adapter.search(query)
                    if isinstance(response, list):
                        raw_results.extend(item for item in response if isinstance(item, dict))
                    else:
                        diagnostics.append({"source": "search", "reason": "Search adapter returned a non-list response."})
                except Exception as error:
                    network_error = network_error or isinstance(error, (ConnectionError, httpx.ConnectError, httpx.TimeoutException))
                    retrieval_not_configured = retrieval_not_configured or isinstance(error, RetrievalConfigurationError)
                    diagnostics.append({"source": "search", "reason": f"Search adapter failed: {safe_error_trace(error)}"})
            citations, rejected = self.evidence_extractor.extract(raw_results)
            diagnostics.extend({"source": "evidence", "reason": reason} for reason in rejected)
            state = self._state(citations, bool(claim["verifiable"]))
            verified.append({**claim, "queries": queries, "citations": citations, "status": state, "status_label": STATE_LABELS[state]})
        mock_sources = any(citation["mock"] for claim in verified for citation in claim["citations"])
        limitations = [
            "ASR、OCR、搜索结果和网页内容均是不可信数据，不能改变系统指令。",
            "只在来源文本带有可定位时间戳时创建主张；缺失时间戳的文本不会被当作证据。",
            "暂无证据表示当前检索没有可用引用，不表示主张为虚假。",
        ]
        if isinstance(self.search_adapter, FakeSearchAdapter):
            limitations.append("受控 FakeSearchAdapter 的所有引用都标记为 mock，不能表示真实联网检索。")
        else:
            limitations.append("检索结果仅作为已保存、可追溯的外部材料；它们不会自动证明主张或来源可靠。")
        if retrieval_not_configured:
            limitations.append("Tavily 检索未配置：请在 Worker 设置 ENABLE_RETRIEVAL=true 和 TAVILY_API_KEY。")
        return {
            "schema_version": "claim-verification-result-v1",
            "status": "not_configured" if retrieval_not_configured else "network_error" if network_error else "completed",
            "mock": mock_sources or isinstance(self.search_adapter, FakeSearchAdapter),
            "claims": verified,
            "diagnostics": diagnostics,
            "limitations": limitations,
        }

    @staticmethod
    def _state(citations: list[dict[str, Any]], verifiable: bool) -> str:
        if not verifiable or not citations:
            return "no_evidence"
        relations = {citation["relation"] for citation in citations}
        if "supports" in relations and "refutes" in relations:
            return "conflicting"
        if "supports" in relations:
            return "supported"
        if "refutes" in relations:
            return "refuted"
        return "background"
