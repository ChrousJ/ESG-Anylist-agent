"""
agent/nodes/context.py  —  上下文理解节点（流程的第 ① 步）
==============================================================

【在流程中的位置】START → ★context★ → supervisor / knowledge_answer / clarify

【这个节点干什么？】

这是用户提问后第一个被执行的节点。它的任务是"理解用户到底在问什么"：

  1. 指代消解：如果用户之前问过"比亚迪的碳排放"，现在又问"那宁德时代呢"，
     需要把"那"替换成真正的指代对象，生成完整的问题。
  2. 实体抽取：从问题中提取关键信息 →
     - companies: 涉及哪些公司？（如 ["比亚迪", "宁德时代"]）
     - years:     涉及哪些年份？（如 [2022, 2023]）
     - metrics:   涉及哪些ESG指标？（如 ["scope_1_emissions"]）
     - industry:  属于哪个行业？（如 "new_energy"）
     - intent:    用户意图是什么？（如 "compare" = 对比、"trend" = 趋势）
  3. 问题分类：决定用户的问题应该走哪条处理路线 →
     - knowledge: 纯概念问题（如"什么是ESG"）→ 直接用 LLM 回答
     - complex:   业务分析问题（如"比亚迪2023碳排放"）→ 走完整分析流程
     - clarify:   信息不足（如"帮我查一下"）→ 反问用户

【分类规则（优先级从高到低）】

  Rule-1: 包含知识类关键词 + 无具体公司/指标 → knowledge
  Rule-2: 包含公司名、指标名、或年份 → complex
  Rule-3: 意图明确但实体严重不足（如只有年份没有公司名） → clarify
  Rule-4: 以上规则都不匹配 → 调用 LLM 做兜底判断

【写入 State 的关键字段】

  - query_class: "knowledge" | "complex" | "clarify"
  - entities: 提取的实体信息（EntityDict）
  - resolved_query: 消解后的完整问题
  - need_clarify: 是否需要反问用户
  - clarify_question: 反问的具体问题文本
"""

from __future__ import annotations

import json
import logging
import re
import os
from datetime import datetime, timezone

from google import genai
from google.genai import types
from dotenv import load_dotenv

from agent.state import AgentState, EntityDict, append_node_trace, format_degraded_reason
from agent.tracing import trace_node, TraceLogger, llm_call_with_retry

load_dotenv()

log = logging.getLogger(__name__)

_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY", ""))
_MODEL  = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-preview-05-20")

# ── 公司名规范化映射（别名 → 标准名） ────────────────────────────────────────
COMPANY_ALIASES: dict[str, str] = {
    # 新能源/汽车
    "比亚迪": "比亚迪", "BYD": "比亚迪",
    "宁德时代": "宁德时代", "CATL": "宁德时代",
    "广汽": "广汽集团", "广汽集团": "广汽集团",
    "上汽": "上汽集团", "上汽集团": "上汽集团",
    "吉利": "吉利汽车", "吉利汽车": "吉利汽车",
    "长城汽车": "长城汽车", "长城": "长城汽车",
    "长安汽车": "长安汽车", "长安": "长安汽车",
    "理想汽车": "理想汽车", "理想": "理想汽车",
    "蔚来": "蔚来汽车", "蔚来汽车": "蔚来汽车",
    "小鹏汽车": "小鹏汽车", "小鹏": "小鹏汽车",
    # 电力
    "华能": "华能国际", "华能国际": "华能国际",
    "大唐": "大唐发电", "中国大唐": "大唐发电",
    "_大唐_RAG_": "中国大唐", # Internal hint for dual mapping if needed, but let's just add both
    "华电": "华电国际", "中国华电": "华电国际",
    "国电": "国电电力", "国家电投": "国电电力",
    "三峡": "三峡能源", "中国三峡": "三峡能源",
    "国网": "国家电网", "国家电网": "国家电网",
    "南网": "南方电网", "南方电网": "南方电网",
    "龙源电力": "龙源电力",
    "华润电力": "华润电力",
    "中广核": "中国广核", "中广核电力": "中国广核",
    "神华": "中国神华", "中国神华": "中国神华",
    "国投电力": "国投电力",
    "川投能源": "川投能源",
    "长江电力": "长江电力",
    # 银行
    "工商银行": "工商银行", "工行": "工商银行", "ICBC": "工商银行",
    "建设银行": "建设银行", "建行": "建设银行", "CCB": "建设银行",
    "农业银行": "农业银行", "农行": "农业银行", "ABC": "农业银行",
    "中国银行": "中国银行", "中行": "中国银行", "BOC": "中国银行",
    "交通银行": "交通银行", "交行": "交通银行",
    "招商银行": "招商银行", "招行": "招商银行",
    "邮储银行": "邮储银行",
    "兴业银行": "兴业银行",
    "浦发银行": "浦发银行",
    "中信银行": "中信银行",
    "平安银行": "平安银行",
    # 锂电/配套
    "亿纬锂能": "亿纬锂能",
    "华友钴业": "华友钴业",
    "国轩高科": "国轩高科",
    "赛力斯": "赛力斯",
}

# ── 指标名到 metric_key 映射 ──────────────────────────────────────────────────
METRIC_ALIASES: dict[str, str] = {
    "碳排放": "scope_1_emissions",
    "范围一": "scope_1_emissions", "scope1": "scope_1_emissions",
    "范围二": "scope_2_emissions", "scope2": "scope_2_emissions",
    "范围三": "scope_3_emissions", "scope3": "scope_3_emissions",
    "绿色贷款": "green_finance_balance", "绿色信贷": "green_finance_balance",
    "清洁能源占比": "clean_energy_ratio", "清洁能源": "clean_energy_ratio",
    "能耗": "total_energy_consumption", "综合能耗": "total_energy_consumption",
    "能耗强度": "energy_intensity",
    "普惠金融": "inclusive_finance_balance",
    "研发投入": "rd_investment_total", "研发费用": "rd_investment_total",
    "供应商审核": "supplier_esg_audit_ratio",
    "培训时长": "employee_training_hours", "人均培训": "employee_training_hours",
    "安全事故": "safety_accidents_count",
    "投诉": "customer_complaint_res", "客户满意度": "customer_complaint_res",
    "捐赠": "charitable_donations",
    "独立董事": "independent_director_ratio",
    "女性董事": "female_director_ratio",
    "反腐": "anti_corruption_coverage",
    "处罚": "regulatory_penalties", "违规": "regulatory_penalties",
    "esg委员会": "esg_committee_setup",
    "esg评级": "external_esg_rating",
}

# ── 纯知识问题关键词（满足条件则不做 RAG/SQL） ─────────────────────────────────
KNOWLEDGE_PATTERNS: list[str] = [
    "什么是", "怎么定义", "如何理解", "解释一下", "介绍一下",
    "esg是什么", "msci是什么", "gri是什么", "tcfd是什么",
    "碳中和是什么", "双碳是什么", "绿色金融是什么",
    "范围一是什么", "范围二是什么", "范围三是什么",
    "概念", "定义", "含义", "原理",
]

# ══════════════════════════════════════════════════════════════════════════════
# 规则分类器
# ══════════════════════════════════════════════════════════════════════════════

def _rule_classify(query: str, entities: EntityDict) -> str | None:
    """
    基于规则的快速分类，返回分类结果或 None（表示需要 LLM 兜底）。
    """
    q_lower = query.lower()

    # Rule-1：纯知识问题
    has_knowledge_pattern = any(p in q_lower for p in KNOWLEDGE_PATTERNS)
    has_business_entity = bool(
        entities.get("companies") or
        entities.get("metrics") or
        entities.get("years")
    )
    if has_knowledge_pattern and not has_business_entity:
        return "knowledge"

    # Rule-2：有明确业务实体 → 一律复杂
    if has_business_entity:
        return "complex"

    # Rule-3：有行业词但无公司 → 仍然复杂（行业级分析）
    industry_words = [
        "银行", "电力", "新能源", "汽车", "金融", "能源",
        "行业", "板块", "企业", "公司", "上市公司",
    ]
    if any(w in query for w in industry_words):
        return "complex"

    return None  # 需要 LLM 兜底


def _extract_years(query: str) -> list[int]:
    """从 query 中提取年份。"""
    years = []
    # 匹配 "2022年"、"2023"、"22-24年"、"近三年" 等
    explicit = re.findall(r"20(2[0-9])", query)
    years = [int("20" + y) for y in explicit]

    # "近N年" 推算
    m = re.search(r"近(\d+|两|三|四|五)年", query)
    if m:
        n_map = {"两": 2, "三": 3, "四": 4, "五": 5}
        n = n_map.get(m.group(1), int(m.group(1)) if m.group(1).isdigit() else 3)
        current = datetime.now().year
        # 数据集范围 2022-2024
        inferred = [y for y in range(current - n, current) if 2022 <= y <= 2024]
        years = sorted(set(years + inferred))

    # 默认范围（无年份时）
    if not years:
        return []

    return sorted(set(y for y in years if 2022 <= y <= 2024))


def _extract_companies(query: str) -> list[str]:
    """从 query 中匹配公司名（精确 + 别名）。"""
    found = []
    for alias, canonical in COMPANY_ALIASES.items():
        if alias in query and canonical not in found:
            found.append(canonical)
    return found


def _extract_metrics(query: str) -> list[str]:
    """从 query 中匹配指标关键词。"""
    found = []
    q_lower = query.lower()
    for alias, key in METRIC_ALIASES.items():
        if alias.lower() in q_lower and key not in found:
            found.append(key)
    return found


def _infer_industry(companies: list[str], query: str) -> str:
    """根据公司列表或 query 关键词推断行业。"""
    bank_cos = {
        "工商银行", "建设银行", "农业银行", "中国银行", "交通银行",
        "招商银行", "邮储银行", "兴业银行", "浦发银行", "中信银行",
    }
    power_cos = {
        "华能国际", "中国大唐", "中国华电", "国家电投", "中国三峡",
        "国家电网", "南方电网", "龙源电力", "华润电力", "中广核电力",
    }
    new_energy_cos = {
        "比亚迪", "宁德时代", "广汽集团", "上汽集团", "吉利汽车",
        "长城汽车", "长安汽车", "理想汽车", "蔚来汽车", "小鹏汽车",
    }
    cos_set = set(companies)
    if cos_set & bank_cos and not (cos_set & power_cos) and not (cos_set & new_energy_cos):
        return "bank"
    if cos_set & power_cos and not (cos_set & bank_cos) and not (cos_set & new_energy_cos):
        return "power"
    if cos_set & new_energy_cos and not (cos_set & bank_cos) and not (cos_set & power_cos):
        return "new_energy"
    if cos_set & bank_cos or "银行" in query or "金融" in query:
        return "bank"
    if cos_set & power_cos or "电力" in query or "发电" in query:
        return "power"
    if cos_set & new_energy_cos or "新能源" in query or "汽车" in query or "电动车" in query:
        return "new_energy"
    if len(set(companies)) > 0:
        return "mixed"
    return ""


def _infer_compare_dimension(
    companies: list[str], years: list[int], intent: str
) -> str:
    """推断对比维度。"""
    multi_company = len(companies) > 1
    multi_year    = len(years) > 1

    if multi_company and multi_year:
        return "both"
    if multi_company and not multi_year:
        return "horizontal"
    if not multi_company and multi_year:
        return "vertical"
    return "none"


def _check_need_clarify(
    query: str, entities: EntityDict, query_class: str
) -> tuple[bool, str]:
    """
    判断是否需要澄清。
    仅对 complex 问题做检查；知识问题不需要澄清。
    """
    if query_class != "complex":
        return False, ""

    # 意图明确但完全没有公司和年份 → 问公司
    has_company = bool(entities.get("companies"))
    has_year    = bool(entities.get("years"))
    has_metric  = bool(entities.get("metrics"))

    if not has_company and not entities.get("industry"):
        return True, "您想了解哪家公司或哪个行业的 ESG 数据？"

    # 对比类问题只有一家公司
    intent = entities.get("intent", "")
    if intent == "compare" and len(entities.get("companies", [])) < 2:
        return True, "您希望与哪些公司进行对比？"

    return False, ""


# ══════════════════════════════════════════════════════════════════════════════
# LLM 实体补全（规则提取不足时兜底）
# ══════════════════════════════════════════════════════════════════════════════

_ENTITY_PROMPT = """\
你是 ESG 分析助手。分析用户问题，提取结构化信息并判断问题类型。

## 对话历史（最近5轮）
{history}

## 当前问题
{query}

## 规则提取的初步结果（可能不完整）
{partial_entities}

## 任务
返回 JSON，所有字段必填，找不到填空数组或空字符串：
{{
  "resolved_query": "指代消解后的完整问题（替换'它''该公司''上述指标'等）",
  "companies": ["公司名1", "公司名2"],
  "years": [2022, 2023],
  "metrics": ["metric_key1"],
  "intent": "trend|compare|qa|summary|ranking",
  "industry": "new_energy|power|bank|mixed|",
  "compare_dimension": "vertical|horizontal|both|none",
  "query_class": "knowledge|complex|clarify",
  "need_clarify": false,
  "clarify_question": ""
}}

## 分类规则（必须遵守）
- knowledge：纯概念问题，不涉及任何具体公司/年份/指标数据
- complex：涉及任何具体公司 OR 需要数据支撑的判断（无论问题看起来多简单）
- clarify：意图明确但关键实体缺失（如"对比碳排放"但不知道对比谁）

## 严禁
- 把"比亚迪ESG做得好吗"判断为 knowledge（需要数据才能回答）
- 把"什么是ESG"判断为 complex
"""


def _llm_complete_entities(
    query: str,
    history: list[dict],
    partial: EntityDict,
    trace_id: str,
) -> dict:
    history_str = "\n".join(
        f"{m['role']}: {m['content'][:100]}" for m in history[-5:]
    ) if history else "（无历史）"

    prompt = _ENTITY_PROMPT.format(
        history=history_str,
        query=query,
        partial_entities=json.dumps(partial, ensure_ascii=False),
    )

    def _call():
        resp = _client.models.generate_content(
            model=_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )
        text = resp.text.strip()
        text = re.sub(r"^```json\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        return json.loads(text)

    return llm_call_with_retry(
        _call, max_retries=2, timeout_seconds=30,
        caller_name="context_llm", trace_id=trace_id,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 主节点函数
# ══════════════════════════════════════════════════════════════════════════════

@trace_node("context", tags=["understanding"])
def context_node(state: AgentState) -> AgentState:
    trace_id = state.get("trace_id", "")
    log      = TraceLogger("context", trace_id)
    t_start  = datetime.now(timezone.utc).isoformat()

    query   = state["user_query"]
    history = state.get("history", [])

    log.info(f"开始处理，query='{query[:60]}'")

    # ── Step 1：规则提取实体 ─────────────────────────────────────────────────
    companies = _extract_companies(query)
    years     = _extract_years(query)
    metrics   = _extract_metrics(query)
    industry  = _infer_industry(companies, query)

    partial = EntityDict(
        companies=companies,
        years=years,
        metrics=metrics,
        industry=industry,
    )

    # ── Step 2：规则分类 ─────────────────────────────────────────────────────
    rule_class = _rule_classify(query, partial)
    log.info(f"规则分类结果：{rule_class}，实体：{partial}")

    # ── Step 3：LLM 补全（规则分类结果不确定，或实体提取不足时） ──────────────
    need_llm = (
        rule_class is None
        or (rule_class == "complex" and not companies and not industry)
        or len(history) > 0  # 有历史时做指代消解
    )

    if need_llm:
        try:
            llm_result = _llm_complete_entities(query, history, partial, trace_id)
            resolved_query    = llm_result.get("resolved_query", query)
            companies         = llm_result.get("companies", companies) or companies
            years             = llm_result.get("years", years) or years
            metrics           = llm_result.get("metrics", metrics) or metrics
            industry          = llm_result.get("industry", industry) or industry
            intent            = llm_result.get("intent", "qa")
            query_class       = llm_result.get("query_class", rule_class or "complex")
            need_clarify      = llm_result.get("need_clarify", False)
            clarify_question  = llm_result.get("clarify_question", "")
            compare_dimension = llm_result.get("compare_dimension", "none")
            log.info(f"LLM补全完成：class={query_class}, companies={companies}")
        except Exception as e:
            log.warning(f"LLM补全失败，使用规则结果：{e}")
            resolved_query    = query
            intent            = "qa"
            query_class       = rule_class or "complex"
            need_clarify      = False
            clarify_question  = ""
            compare_dimension = _infer_compare_dimension(companies, years, "qa")
    else:
        resolved_query    = query
        intent            = "trend" if len(years) > 1 else (
                            "compare" if len(companies) > 1 else "qa")
        query_class       = rule_class
        compare_dimension = _infer_compare_dimension(companies, years, intent)
        need_clarify, clarify_question = _check_need_clarify(
            query, partial, query_class
        )

    # ── Step 4：规范化公司名 ─────────────────────────────────────────────────
    new_companies = []
    for c in companies:
        mapped = COMPANY_ALIASES.get(c, c)
        new_companies.append(mapped)
        # 针对大唐的特殊处理：同时支持 SQL (大唐发电) 和 RAG (中国大唐)
        if mapped == "大唐发电":
            new_companies.append("中国大唐")
    companies = list(dict.fromkeys(new_companies))  # 去重保序

    # ── Step 5：默认年份（用户未指定时用全部可用年份） ──────────────────────
    if not years and query_class == "complex":
        years = [2022, 2023, 2024]
        log.info("未检测到年份，默认使用全部年份 [2022,2023,2024]")

    if (
        not companies
        and not metrics
        and not industry
        and query_class == "complex"
        and not need_clarify
    ):
        query_class = "refuse"
        state["is_degraded"] = True
        state["degraded_reason"] = format_degraded_reason(
            "OUT_OF_SCOPE", "no ESG entities detected"
        )


    # ── Step 6：组装 entities ────────────────────────────────────────────────
    entities = EntityDict(
        companies=companies,
        years=years,
        metrics=metrics,
        intent=intent,
        industry=industry,
        compare_dimension=compare_dimension,
    )

    log.info(
        f"实体提取完成",
        {
            "class": query_class,
            "companies": companies,
            "years": years,
            "metrics": metrics,
            "intent": intent,
            "industry": industry,
        },
    )

    state["resolved_query"]   = resolved_query
    state["query_class"]      = query_class
    state["entities"]         = entities
    state["need_clarify"]     = need_clarify
    state["clarify_question"] = clarify_question

    return state
