"""Rule-based greenwashing risk detection.

Scope: this module does NOT judge whether a company truly greenwashes. It flags
report passages where the language is strong but nearby evidence is weak. That
"claim-evidence mismatch" framing is narrow, explainable, and suitable for a
student project demo.
"""
from __future__ import annotations

import re
from typing import Any

STRONG_CLAIM_PATTERNS = [
    "绿色低碳", "绿色转型", "低碳转型", "碳中和", "碳达峰", "双碳",
    "可持续发展", "生态优先", "全面推进", "持续推进", "持续提升",
    "行业领先", "高度重视", "完善体系", "健全机制", "积极履行",
    "绿色发展", "节能减排", "清洁能源", "循环经济", "环境友好",
]

EVIDENCE_REGEXES = [
    re.compile(r"\d+(?:\.\d+)?\s*%"),
    re.compile(r"\d+(?:\.\d+)?\s*(?:万吨|吨|亿元|万元|GJ|千瓦时|兆瓦时|人|次|项|家|tCO2e|CO2e)", re.IGNORECASE),
    re.compile(r"(?:同比|较上年|比上年|下降|提升|减少|增加|增长)\s*\d+(?:\.\d+)?"),
]

EVIDENCE_KEYWORDS = [
    "第三方鉴证", "鉴证", "审计", "认证", "ISO", "GRI", "TCFD", "CDP",
    "经核查", "覆盖率", "完成率", "同比", "较上年", "下降", "提升", "减少", "增加",
]

_TARGET_NO_PROGRESS = ["碳中和", "碳达峰", "双碳", "净零", "目标", "承诺", "规划", "路线图"]
_SUPPLY_CHAIN = ["供应链", "供应商", "采购", "劳工", "审核", "尽职调查"]
_SAFETY = ["安全", "事故", "工伤", "职业健康", "培训"]


def _chunk_text(chunk: dict[str, Any]) -> str:
    return str(chunk.get("text") or chunk.get("content") or chunk.get("excerpt") or "")


def _contains_strong_claim(text: str) -> str:
    for pattern in STRONG_CLAIM_PATTERNS:
        if pattern.lower() in text.lower():
            return pattern
    return ""


def _has_evidence(text: str) -> bool:
    if any(regex.search(text) for regex in EVIDENCE_REGEXES):
        return True
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in EVIDENCE_KEYWORDS)


def _risk_type(text: str) -> str:
    if any(k in text for k in _TARGET_NO_PROGRESS):
        return "target_without_progress"
    if any(k in text for k in _SUPPLY_CHAIN):
        return "supply_chain_claim_weak_evidence"
    if any(k in text for k in _SAFETY):
        return "safety_claim_weak_evidence"
    return "vague_commitment"


def _level(text: str, evidence_status: str) -> str:
    if evidence_status == "missing" and any(k in text for k in ["碳中和", "碳达峰", "行业领先", "全面"]):
        return "high"
    if evidence_status == "missing":
        return "medium"
    return "low"


def _claim_excerpt(text: str, marker: str, max_len: int = 96) -> str:
    idx = text.find(marker) if marker else -1
    if idx < 0:
        return text.strip()[:max_len]
    start = max(0, idx - 24)
    end = min(len(text), idx + max_len)
    excerpt = text[start:end].strip()
    return excerpt.replace("\n", " ")


def detect_greenwashing_risks(state: dict[str, Any], max_risks: int = 8) -> dict[str, Any]:
    rag_result = state.get("rag_result") if isinstance(state.get("rag_result"), dict) else {}
    chunks = list(rag_result.get("chunks", []) or [])
    risks: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for chunk in chunks:
        text = _chunk_text(chunk)
        if not text:
            continue
        marker = _contains_strong_claim(text)
        if not marker:
            continue

        has_evidence = _has_evidence(text)
        if has_evidence:
            # Strong claim with nearby evidence is not a greenwashing risk in this narrow rubric.
            continue

        risk_type = _risk_type(text)
        evidence_status = "missing"
        claim = _claim_excerpt(text, marker)
        key = (risk_type, claim[:40], str(chunk.get("page_num", chunk.get("page", ""))))
        if key in seen:
            continue
        seen.add(key)

        risks.append({
            "type": risk_type,
            "level": _level(text, evidence_status),
            "claim": claim,
            "evidence_status": evidence_status,
            "reason": "附近文本未发现量化指标、年度进展、同比变化或第三方鉴证信息。",
            "company": chunk.get("company_name") or chunk.get("company") or "",
            "year": chunk.get("year", ""),
            "page": chunk.get("page_num") or chunk.get("page") or "",
            "file": chunk.get("source_file") or chunk.get("file") or "",
        })
        if len(risks) >= max_risks:
            break

    by_level: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
    for risk in risks:
        by_level[str(risk.get("level", "medium"))] = by_level.get(str(risk.get("level", "medium")), 0) + 1

    summary = (
        f"发现 {len(risks)} 个潜在绿漂核查点；"
        f"高风险 {by_level.get('high', 0)} 个，中风险 {by_level.get('medium', 0)} 个。"
        if risks else
        "未在当前召回证据中发现明显的强表述弱证据片段。"
    )
    return {
        "risk_count": len(risks),
        "risks": risks,
        "summary": summary,
        "method": "rule-based claim-evidence mismatch: strong ESG claim without nearby quantitative/progress/assurance evidence",
    }


def render_greenwashing_markdown(result: dict[str, Any]) -> str:
    risks = result.get("risks", []) or []
    lines = [
        "",
        "---",
        "### Greenwashing Risk Radar（潜在绿漂风险雷达）",
        result.get("summary", ""),
        "",
        "| 风险类型 | 等级 | 证据状态 | 公司/年份 | 页码 | 说明 |",
        "|---|---|---|---|---:|---|",
    ]
    if not risks:
        lines.append("| - | - | - | - | - | 当前召回证据未触发规则型风险信号 |")
    else:
        for risk in risks:
            entity = f"{risk.get('company','')} {risk.get('year','')}".strip() or "-"
            page = risk.get("page") or "-"
            detail = f"{risk.get('claim','')[:48]}；{risk.get('reason','')}"
            lines.append(
                f"| {risk.get('type','')} | {risk.get('level','')} | "
                f"{risk.get('evidence_status','')} | {entity} | {page} | {detail} |"
            )
    lines.extend([
        "",
        f"> 方法说明：{result.get('method')}。这是“需进一步人工核查的风险信号”，不是对公司真实 ESG 表现的定性结论。",
    ])
    return "\n".join(lines)
