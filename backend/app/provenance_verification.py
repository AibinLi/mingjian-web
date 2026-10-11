"""Conservative source-traceability verification over controlled search records."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .claim_verification import FakeSearchAdapter, SearchAdapter


def _canonical_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname.lower().rstrip(".")
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        return None
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), host + port, path, parsed.query, ""))


class ProvenanceVerifier:
    """Scores only whether a user-declared public origin is externally traceable."""

    def __init__(self, search_adapter: SearchAdapter) -> None:
        self.search_adapter = search_adapter

    def verify(self, declared_url: str | None) -> dict[str, Any]:
        mock = isinstance(self.search_adapter, FakeSearchAdapter)
        base = {
            "schema_version": "provenance-verification-result-v1",
            "mock": mock,
            "declared_url": declared_url,
            "risk_score": None,
            "evidence": [],
            "score_semantics": "0-100 来源追溯复核风险指数；不是域名信誉概率或来源真实概率。",
            "limitations": [
                "只核验用户声明的公开原始链接是否能在受控检索结果中追溯；不下载平台视频。",
                "同域名或精确 URL 可见不证明发布者身份、首发关系、页面内容真实或传播链完整。",
                "没有原始公开链接或没有合格检索结果时不生成低风险分数。",
            ],
        }
        canonical = _canonical_url(declared_url) if isinstance(declared_url, str) else None
        if not canonical:
            return {**base, "status": "unavailable", "verdict": "未提供可核验的原始公开链接", "summary": "无法仅从本地媒体字节恢复出处与传播链。"}
        if mock:
            return {**base, "status": "unavailable", "verdict": "来源检索为 Mock", "summary": "Mock 检索不能用于来源可靠性评分。"}
        domain = urlsplit(canonical).hostname or ""
        query = {"claim_id": "provenance", "text": f'原始来源 {canonical} {domain}', "kind": "provenance"}
        try:
            records = self.search_adapter.search(query)
        except Exception as error:
            return {**base, "status": "unavailable", "verdict": "来源联网核验失败", "summary": f"受控检索未完成：{str(error)[:160]}"}
        evidence: list[dict[str, Any]] = []
        exact = False
        same_domain = False
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict) or record.get("source_kind") != "fetched":
                continue
            candidate = _canonical_url(record.get("url")) if isinstance(record.get("url"), str) else None
            if not candidate:
                continue
            candidate_domain = urlsplit(candidate).hostname or ""
            is_exact = candidate == canonical
            is_same_domain = candidate_domain == domain
            exact = exact or is_exact
            same_domain = same_domain or is_same_domain
            evidence.append({
                "url": candidate,
                "title": str(record.get("title", ""))[:500],
                "domain": candidate_domain,
                "excerpt": str(record.get("excerpt") or record.get("body") or "")[:800],
                "fetched_at": record.get("fetched_at"),
                "match": "exact_url" if is_exact else "same_domain" if is_same_domain else "other",
            })
            if len(evidence) >= 5:
                break
        if exact:
            return {**base, "status": "completed", "verdict": "声明来源可精确追溯", "risk_score": 20.0, "evidence": evidence, "summary": "受控检索中存在与声明原始链接一致的记录；仍需人工确认发布者身份与首发关系。"}
        if same_domain:
            return {**base, "status": "completed", "verdict": "声明来源可部分追溯", "risk_score": 50.0, "evidence": evidence, "summary": "检索到同域名材料但没有精确 URL 记录；来源链仍不完整。"}
        return {**base, "status": "insufficient_evidence", "verdict": "来源追溯证据不足", "evidence": evidence, "summary": "没有检索到声明 URL 或同域名的合格记录；这不表示来源虚假。"}


def provenance_axis(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "来源可靠性",
        "mock": bool(value.get("mock")),
        "status": value.get("status", "unavailable"),
        "conclusion": value.get("verdict", "无法判断"),
        "summary": value.get("summary", "未提供来源核验摘要。"),
        "risk_score": value.get("risk_score"),
        "evidence": value.get("evidence", []),
        "limitations": value.get("limitations", []),
    }
