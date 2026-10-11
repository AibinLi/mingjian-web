"""Auditable internet verification and weighted review-risk scorecard.

The values in this module are policy review indices, not calibrated
probabilities and not automatic true/false determinations.  Missing or Mock
evidence remains unavailable and can force the routing result to human review.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any


INTERNET_SCHEMA = "internet-verification-result-v1"
SCORECARD_SCHEMA = "aggregate-review-scorecard-v1"
STATUS_LABELS = {
    "supported": "现有联网证据支持",
    "refuted": "现有联网证据反对",
    "conflicting": "联网证据相互冲突",
    "insufficient_evidence": "联网证据不足",
    "unavailable": "联网核验不可用",
    "network_error": "联网核验不可用，请人工核查",
}
STATUS_RISK = {"supported": 0.10, "refuted": 0.90, "conflicting": 0.70}
CONTENT_RISK = {"supported": 0.10, "refuted": 0.90, "conflicting": 0.70, "background": 0.45, "no_evidence": 0.80}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _bounded_text(value: Any, maximum: int = 500) -> str | None:
    return value[:maximum] if isinstance(value, str) and value.strip() else None


class InternetVerificationBuilder:
    """Validate an LLM's citation-index decisions against persisted evidence."""

    def build(self, verification: dict[str, Any], llm_analysis: dict[str, Any]) -> dict[str, Any]:
        base = {
            "schema_version": INTERNET_SCHEMA,
            "provider": llm_analysis.get("provider", "none"),
            "model": llm_analysis.get("model"),
            "generated_at": llm_analysis.get("generated_at"),
            "mock": bool(verification.get("mock")),
            "score_semantics": "0-100 联网反证风险指数；是固定政策映射，不是内容为假的概率。",
            "automatic_action": "none",
            "limitations": [
                "只允许模型根据已保存的联网引用分类，不允许使用模型记忆补造事实、引用或来源。",
                "联网证据支持或反对的是可定位主张，不等同于媒体文件真实或来源可靠。",
                "没有真实引用、模型输出不可用或索引校验失败时不会生成低风险分数。",
            ],
        }
        claims = verification.get("claims") if isinstance(verification.get("claims"), list) else []
        if verification.get("status") == "network_error" or llm_analysis.get("status") == "network_error":
            return {**base, "status": "network_error", "verdict": STATUS_LABELS["network_error"], "risk_score": None, "claim_results": [], "summary": "DNS、连接、代理或网络超时导致联网核验未完成；该维度不参与加权。"}
        if verification.get("mock"):
            return {**base, "status": "insufficient_evidence", "verdict": STATUS_LABELS["insufficient_evidence"], "risk_score": None, "claim_results": [], "summary": "当前主张或引用为 Mock，不能用于联网属实性判断。"}
        if llm_analysis.get("status") != "completed":
            return {**base, "status": "unavailable", "verdict": STATUS_LABELS["unavailable"], "risk_score": None, "claim_results": [], "summary": "联网大模型分析未完成，保留原始主张和引用供人工核验。"}
        raw = llm_analysis.get("internet_verification")
        raw_results = raw.get("claim_results") if isinstance(raw, dict) and isinstance(raw.get("claim_results"), list) else []
        validated: list[dict[str, Any]] = []
        seen: set[int] = set()
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            claim_index = item.get("claim_index")
            status = item.get("status")
            if not isinstance(claim_index, int) or claim_index in seen or not 0 <= claim_index < len(claims):
                continue
            if status not in {*STATUS_RISK, "insufficient_evidence"}:
                continue
            claim = claims[claim_index]
            if not isinstance(claim, dict):
                continue
            citations = claim.get("citations") if isinstance(claim.get("citations"), list) else []
            selected: list[dict[str, Any]] = []
            for citation_index in item.get("citation_indexes", []):
                if isinstance(citation_index, int) and 0 <= citation_index < len(citations):
                    citation = citations[citation_index]
                    if isinstance(citation, dict) and not citation.get("mock"):
                        selected.append(citation)
            if status != "insufficient_evidence" and not selected:
                status = "insufficient_evidence"
            seen.add(claim_index)
            validated.append({
                "claim_id": claim.get("claim_id"),
                "claim_index": claim_index,
                "original_text": claim.get("original_text"),
                "status": status,
                "status_label": STATUS_LABELS[status],
                "reason": _bounded_text(item.get("reason"), 600),
                "citations": selected[:5],
            })
        scored = [item for item in validated if item["status"] in STATUS_RISK]
        states = {item["status"] for item in scored}
        if "refuted" in states and "supported" in states or "conflicting" in states:
            overall_status = "conflicting"
        elif "refuted" in states:
            overall_status = "refuted"
        elif "supported" in states:
            overall_status = "supported"
        else:
            overall_status = "insufficient_evidence"
        risk_score = round(100 * sum(STATUS_RISK[item["status"]] for item in scored) / len(scored), 2) if scored else None
        return {
            **base,
            "status": "completed" if scored else "insufficient_evidence",
            "verdict": STATUS_LABELS[overall_status],
            "verdict_code": overall_status,
            "risk_score": risk_score,
            "claim_results": validated,
            "summary": _bounded_text(raw.get("summary"), 800) if isinstance(raw, dict) else None,
        }


@dataclass(frozen=True)
class ScorecardConfig:
    weights: dict[str, float]
    low_max: float = 35.0
    high_min: float = 65.0
    minimum_coverage: float = 0.65

    @classmethod
    def from_env(cls) -> "ScorecardConfig":
        defaults = {"authenticity": 0.40, "content": 0.25, "provenance": 0.10, "internet_verification": 0.25}
        env_names = {
            "authenticity": "MINGJIAN_SCORE_WEIGHT_AUTHENTICITY",
            "content": "MINGJIAN_SCORE_WEIGHT_CONTENT",
            "provenance": "MINGJIAN_SCORE_WEIGHT_PROVENANCE",
            "internet_verification": "MINGJIAN_SCORE_WEIGHT_INTERNET",
        }
        values: dict[str, float] = {}
        for key, default in defaults.items():
            try:
                value = float(os.environ.get(env_names[key], default))
            except ValueError:
                value = default
            values[key] = value if math.isfinite(value) and value >= 0 else default
        total = sum(values.values())
        if total <= 0:
            values = defaults
            total = sum(values.values())
        weights = {key: value / total for key, value in values.items()}
        low_max = _bounded_env_float("MINGJIAN_SCORE_LOW_MAX", 35.0, 0.0, 100.0)
        high_min = _bounded_env_float("MINGJIAN_SCORE_HIGH_MIN", 65.0, 0.0, 100.0)
        if low_max >= high_min:
            low_max, high_min = 35.0, 65.0
        return cls(
            weights=weights,
            low_max=low_max,
            high_min=high_min,
            minimum_coverage=_bounded_env_float("MINGJIAN_SCORE_MIN_COVERAGE", 0.65, 0.0, 1.0),
        )


def _bounded_env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        return default
    return value if math.isfinite(value) and minimum <= value <= maximum else default


class AggregateAssessmentBuilder:
    """Build four comparable policy indices and a review-routing score."""

    def __init__(self, config: ScorecardConfig | None = None) -> None:
        self.config = config or ScorecardConfig.from_env()

    def describe(self) -> dict[str, Any]:
        return {"schema_version": SCORECARD_SCHEMA, "weights": self.config.weights, "low_max": self.config.low_max, "high_min": self.config.high_min, "minimum_coverage": self.config.minimum_coverage, "automatic_truth_label": False}

    def build(self, report: dict[str, Any]) -> dict[str, Any]:
        components = {
            "authenticity": self._component("媒体真实性", self._authenticity_values(report)),
            "content": self._component("内容可信度", self._content_values(report)),
            "provenance": self._component("来源可靠性", self._provenance_values(report)),
            "internet_verification": self._component("联网搜索属实性", self._internet_values(report)),
        }
        available = [key for key, value in components.items() if value["status"] == "available"]
        applicable = self._applicable_dimensions(report)
        covered_weight = sum(self.config.weights[key] for key in available)
        total_configured_weight = sum(self.config.weights.values())
        configured_coverage = round(covered_weight / total_configured_weight, 4) if total_configured_weight > 0 else 0.0
        applicable_weight = sum(self.config.weights[key] for key in applicable)
        applicable_covered_weight = sum(self.config.weights[key] for key in available if key in applicable)
        applicable_coverage = round(applicable_covered_weight / applicable_weight, 4) if applicable_weight > 0 else 0.0
        partial_score = round(sum(components[key]["score"] * self.config.weights[key] for key in available) / covered_weight, 2) if covered_weight else None
        complete = len(available) == len(components)
        applicable_complete = bool(applicable) and all(key in available for key in applicable)
        overall = partial_score if applicable_complete else None
        authenticity_score = components["authenticity"]["score"]
        other_available_scores = [components[key]["score"] for key in available if key != "authenticity"]
        high_authenticity_without_counterevidence = authenticity_score is not None and authenticity_score > 95.0 and not any(score <= self.config.low_max for score in other_available_scores)
        if high_authenticity_without_counterevidence:
            band = "high_risk_review"
            label = "高危需审"
            reason = "媒体真实性复核风险指数高于 95，且其他可用维度没有强低风险反证；仅升级人工复核优先级。"
        elif configured_coverage < self.config.minimum_coverage:
            band = "manual_review_required"
            label = "需人工干预"
            reason = "政策配置维度的有效权重覆盖不足；局部参考分不能冒充完整证据，必须人工补证据。"
        elif not applicable_complete:
            band = "manual_review_required"
            label = "需人工干预"
            reason = "当前输入适用的维度尚未全部可用；缺失或失败不会补零。"
        elif overall is not None and overall >= self.config.high_min:
            band = "high_risk_review"
            label = "伪造风险高（需人工复核）"
            reason = "综合复核风险指数进入高风险区；这不是自动伪造定论。"
        elif overall is not None and overall <= self.config.low_max:
            band = "low_forgery_risk_not_proof"
            label = "伪造风险低（不等于真实证明）"
            reason = "综合复核风险指数进入低风险区，但缺少证据仍不能被解释为真实证明。"
        else:
            band = "manual_review_required"
            label = "需人工干预"
            reason = "综合复核风险指数位于中间区间，需要人工结合四个维度的原始证据判断。"
        for key, component in components.items():
            component["configured_weight"] = round(self.config.weights[key], 4)
        return {
            "schema_version": SCORECARD_SCHEMA,
            "status": "completed",
            "score_kind": "review_risk_index",
            "score_semantics": "0-100 固定政策复核风险指数；不是伪造概率、真实性概率或事实正确率。",
            "component_formula": "component_score = 100 * sum(normalized_evidence_value * evidence_weight) / sum(evidence_weight)",
            "overall_formula": "overall_score = sum(component_score * configured_weight) / sum(available_configured_weight)",
            "components": components,
            "coverage": configured_coverage,
            "configured_coverage": configured_coverage,
            "applicable_coverage": applicable_coverage,
            "applicable_dimensions": sorted(applicable),
            "available_weight": round(covered_weight, 4),
            "applicable_weight": round(applicable_weight, 4),
            "minimum_coverage": self.config.minimum_coverage,
            "overall_score": overall,
            "partial_score": partial_score,
            "complete": complete,
            "applicable_complete": applicable_complete,
            "bands": {"low": [0.0, self.config.low_max], "manual": [self.config.low_max, self.config.high_min], "high": [self.config.high_min, 100.0]},
            "routing_band": band,
            "routing_label": label,
            "routing_reason": reason,
            "automatic_action": "none",
            "truth_label": "none",
            "limitations": [
                "四个数字使用同一加权平均公式，但输入是不同模型/证据状态的政策映射，不能作为经过校准的跨模态概率。",
                "Mock、暂无证据、不可用和不适用不会填充为 0 分或低风险；对应维度保持无法评分。",
                "媒体真实性、内容可信度和来源可靠性的原始状态继续独立保存，本报告只用于人工复核排序。",
            ],
        }

    @staticmethod
    def _applicable_dimensions(report: dict[str, Any]) -> set[str]:
        applicable: set[str] = set()
        visual = report.get("visual_model") if isinstance(report.get("visual_model"), dict) else {}
        audio = report.get("audio_model") if isinstance(report.get("audio_model"), dict) else {}
        sync = report.get("audio_visual_sync") if isinstance(report.get("audio_visual_sync"), dict) else {}
        if any(value.get("status") not in {None, "not_applicable"} for value in (visual, audio, sync)):
            applicable.add("authenticity")
        verification = report.get("claim_verification") if isinstance(report.get("claim_verification"), dict) else {}
        claims = verification.get("claims") if isinstance(verification.get("claims"), list) else []
        if claims:
            applicable.add("content")
            applicable.add("internet_verification")
        provenance = report.get("provenance_verification") if isinstance(report.get("provenance_verification"), dict) else {}
        axis = (report.get("axes") or {}).get("provenance") if isinstance(report.get("axes"), dict) else {}
        if provenance.get("declared_url") or provenance.get("evidence") or isinstance(axis, dict) and (_number(axis.get("risk_score")) is not None or axis.get("status") not in {None, "unavailable", "not_applicable"}):
            applicable.add("provenance")
        return applicable

    @staticmethod
    def _component(name: str, values: list[dict[str, Any]]) -> dict[str, Any]:
        usable = [item for item in values if _number(item.get("value")) is not None and _number(item.get("weight")) is not None and float(item["weight"]) > 0]
        total_weight = sum(float(item["weight"]) for item in usable)
        score = round(100 * sum(min(1.0, max(0.0, float(item["value"]))) * float(item["weight"]) for item in usable) / total_weight, 2) if total_weight else None
        return {"name": name, "status": "available" if score is not None else "unavailable", "score": score, "evidence_count": len(usable), "inputs": usable}

    @staticmethod
    def _authenticity_values(report: dict[str, Any]) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        visual = report.get("visual_model") if isinstance(report.get("visual_model"), dict) else {}
        audio = report.get("audio_model") if isinstance(report.get("audio_model"), dict) else {}
        sync = report.get("audio_visual_sync") if isinstance(report.get("audio_visual_sync"), dict) else {}
        candidates = [
            ("visual.meso4", visual.get("status"), (visual.get("video_risk") or {}).get("risk_score")),
            ("audio.ast", audio.get("status"), (audio.get("whole_audio_risk") or {}).get("risk_score")),
            ("sync.haar_motion", sync.get("status"), sync.get("risk_score")),
        ]
        for source, status, value in candidates:
            if status == "completed" and _number(value) is not None:
                values.append({"source": source, "value": float(value), "weight": 1.0, "calibrated": False})
        return values

    @staticmethod
    def _content_values(report: dict[str, Any]) -> list[dict[str, Any]]:
        verification = report.get("claim_verification") if isinstance(report.get("claim_verification"), dict) else {}
        if verification.get("mock"):
            return []
        values = []
        for claim in verification.get("claims", []) if isinstance(verification.get("claims"), list) else []:
            if isinstance(claim, dict) and claim.get("status") in CONTENT_RISK:
                values.append({"source": claim.get("claim_id"), "value": CONTENT_RISK[claim["status"]], "weight": 1.0, "status": claim["status"]})
        return values

    @staticmethod
    def _provenance_values(report: dict[str, Any]) -> list[dict[str, Any]]:
        provenance = (report.get("axes") or {}).get("provenance") if isinstance(report.get("axes"), dict) else {}
        if not isinstance(provenance, dict) or provenance.get("mock"):
            return []
        value = _number(provenance.get("risk_score"))
        return [{"source": "provenance.axis", "value": value / 100.0, "weight": 1.0}] if value is not None and 0.0 <= value <= 100.0 else []

    @staticmethod
    def _internet_values(report: dict[str, Any]) -> list[dict[str, Any]]:
        value = report.get("internet_verification") if isinstance(report.get("internet_verification"), dict) else {}
        score = _number(value.get("risk_score"))
        if value.get("status") != "completed" or value.get("mock") or score is None:
            return []
        return [{"source": "internet_verification", "value": score / 100.0, "weight": 1.0, "verdict": value.get("verdict_code")}]
