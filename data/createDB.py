"""
createDB.py  —  ESG 报告结构化数据库构建脚本
=================================================
目录结构假设：
  ./finance/   ← 银行行业 PDF
  ./car/       ← 新能源汽车行业 PDF
  ./electric/  ← 电力行业 PDF
  ./createDB.py

运行前安装依赖：
  pip install pdfplumber camelot-py[cv] openai pydantic rapidfuzz pandas
"""

# ── 标准库 ──────────────────────────────────────────────────────────────────
import os
import re
import json
import sqlite3
import hashlib
import logging
from pathlib import Path
from typing import Optional
from datetime import datetime
from dotenv import load_dotenv

# ── 第三方库 ─────────────────────────────────────────────────────────────────
import pdfplumber
import pandas as pd
from pydantic import BaseModel, Field, model_validator
from openai import OpenAI
from rapidfuzz import fuzz

# ══════════════════════════════════════════════════════════════════════════════
# 0.  全局配置
# ══════════════════════════════════════════════════════════════════════════════
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("ingestion.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

DB_PATH   = "esg_data.db"
# 用环境变量注入，避免明文泄露（示例：DASHSCOPE_API_KEY=sk-***）
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "").strip()
if not DASHSCOPE_API_KEY:
    raise RuntimeError(
        "DASHSCOPE_API_KEY 未设置，请在环境变量或 .env 中配置。"
    )
client = OpenAI(
    api_key=DASHSCOPE_API_KEY,
    base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
)
QWEN_MODELS = [
    "qwen-plus-2025-07-14",
    "qwen3-max-2025-09-23",
    "qwen-plus-2025-04-28",
    "qwen-plus",
    "qwen3-max-preview"
]
CURRENT_MODEL_IDX = 0

INDUSTRY_FOLDERS = {
    "finance":  "bank",
    "car":      "new_energy",
    "electric": "power",
}

# 每个行业必须存在的通用指标（用于缺失率统计）
UNIVERSAL_REQUIRED = [
    "scope_1_emissions", "scope_2_emissions",
    "total_energy_consumption", "energy_intensity",
    "employee_training_hours", "safety_accidents_count",
    "customer_complaint_res", "charitable_donations",
    "independent_director_ratio", "female_director_ratio",
    "anti_corruption_coverage", "regulatory_penalties",
    "esg_committee_setup", "external_esg_rating",
]
BANKING_REQUIRED   = ["green_finance_balance", "inclusive_finance_balance"]
AUTO_REQUIRED      = ["scope_3_emissions", "rd_investment_total",
                      "supplier_esg_audit_ratio"]
POWER_REQUIRED     = ["scope_3_emissions", "clean_energy_ratio",
                      "rd_investment_total"]

# ══════════════════════════════════════════════════════════════════════════════
# 1.  Pydantic 数据模型（含全量别名 & 原始值/单位分离）
# ══════════════════════════════════════════════════════════════════════════════

class RawMetric(BaseModel):
    """单个指标的原始提取结果：只记录报告里写的原始数字和单位，不做换算。"""
    raw_value: Optional[str] = Field(None, description="报告原文数值字符串，如'1,234.5'或'约100'")
    raw_unit:  Optional[str] = Field(None, description="报告原文单位字符串，如'万吨CO₂当量'")
    page:      Optional[str] = Field(None, description="出现在第几页，如'45'或'45-46'")
    excerpt:   Optional[str] = Field(None, description="原文片段（≤80字），用于溯源核验")


class UniversalMetricsRaw(BaseModel):
    """
    14 个通用指标的原始提取 —— 适用全部三个行业。
    Field description 里列出所有已知别名，让 LLM 在全文中匹配。
    所有字段必须允许 None；找不到时严禁填 0。
    """
    scope_1_emissions: Optional[RawMetric] = Field(
        None,
        description=(
            "范围一直接温室气体排放量。别名：直接温室气体排放、范围1排放、Scope 1排放量、"
            "直接碳排放量、GHG直接排放、温室气体排放（范围一/范围1）、直接排放量、"
            "Scope1 GHG Emissions。单位通常为：万吨CO₂当量、tCO₂e、吨CO₂当量。"
        ),
    )
    scope_2_emissions: Optional[RawMetric] = Field(
        None,
        description=(
            "范围二间接温室气体排放量（外购电力/热力）。别名：间接温室气体排放、范围2排放、"
            "Scope 2排放量、能源间接排放、外购电力碳排放、电力消费排放、"
            "温室气体排放（范围二/范围2）、Scope2 GHG Emissions。"
            "单位通常为：万吨CO₂当量、tCO₂e。"
        ),
    )
    total_energy_consumption: Optional[RawMetric] = Field(
        None,
        description=(
            "运营综合能耗总量。别名：能源消耗总量、综合能耗、能源消费总量、总能耗、"
            "能源消耗量、能源消费量、能源使用总量、综合能源消耗、运营能耗。"
            "单位通常为：万吨标准煤、GJ、万GJ、MWh、亿千瓦时。"
        ),
    )
    energy_intensity: Optional[RawMetric] = Field(
        None,
        description=(
            "单位营收/产值能耗强度。别名：能耗强度、单位营业收入综合能耗、"
            "万元产值综合能耗、能源消耗强度、单位GDP能耗、单位收入能源消耗。"
            "单位通常为：tce/万元、GJ/万元、kWh/万元。"
        ),
    )
    employee_training_hours: Optional[RawMetric] = Field(
        None,
        description=(
            "员工年度人均培训总时长。别名：人均培训学时、人均年度培训时间、"
            "员工平均培训课时、年人均培训时数、员工培训平均学时、"
            "人均接受培训时长、平均培训时间。单位通常为：小时/人、课时/人、学时。"
        ),
    )
    safety_accidents_count: Optional[RawMetric] = Field(
        None,
        description=(
            "安全生产事故次数或工亡人数。别名：生产安全事故、工伤事故次数、"
            "安全事故总数、职业安全事故、因工死亡人数、工亡人数、"
            "重大安全事故次数、伤亡人数、安全生产伤亡事故、LTIR、工伤率。"
            "单位通常为：次、人、起。若明确为0请如实填写并说明。"
        ),
    )
    customer_complaint_res: Optional[RawMetric] = Field(
        None,
        description=(
            "客户投诉办结率或客户满意度。别名：投诉处理完结率、客诉解决率、"
            "客户投诉完成率、消费者投诉办结率、客户满意度得分、用户满意度、"
            "服务满意度、客户投诉处理率。单位通常为：%、百分比、分（满分100）。"
        ),
    )
    charitable_donations: Optional[RawMetric] = Field(
        None,
        description=(
            "公益慈善捐赠总额。别名：慈善捐款总额、公益投入、社会捐赠、"
            "捐赠金额、公益慈善支出、社会公益投入、捐赠总额、公益事业支出、"
            "对外捐赠。单位通常为：万元、亿元。"
        ),
    )
    independent_director_ratio: Optional[RawMetric] = Field(
        None,
        description=(
            "独立董事在董事会中的占比。别名：独立董事比例、独立董事占比、"
            "董事会独立性、独立非执行董事占比、外部独立董事比例。"
            "单位通常为：%、百分比、分数形式如'5/9'。"
        ),
    )
    female_director_ratio: Optional[RawMetric] = Field(
        None,
        description=(
            "董事会或高管团队中女性占比。别名：女性董事比例、董事会女性占比、"
            "女性高管比例、董事会多元化（性别）、女性高管及董事占比、"
            "女性领导者比例。单位通常为：%、百分比。"
        ),
    )
    anti_corruption_coverage: Optional[RawMetric] = Field(
        None,
        description=(
            "反腐败及商业道德培训覆盖率。别名：廉洁从业培训覆盖率、反腐培训比例、"
            "商业道德培训覆盖、诚信合规培训覆盖率、廉洁教育覆盖率、"
            "反腐败培训员工覆盖率。单位通常为：%、百分比。"
        ),
    )
    regulatory_penalties: Optional[RawMetric] = Field(
        None,
        description=(
            "年度受监管机构违规处罚次数或金额。别名：行政处罚次数、违规罚款金额、"
            "监管处罚、合规处罚、受罚次数、违规处罚金额、行政处罚金额、"
            "被监管处罚情况。单位通常为：次、万元、亿元（金额时）。"
            "若年度内无处罚请注明0次，不要填None。"
        ),
    )
    esg_committee_setup: Optional[RawMetric] = Field(
        None,
        description=(
            "是否设立ESG或可持续发展委员会/风险管理委员会。别名：ESG委员会、"
            "可持续发展委员会、社会责任委员会、风险与ESG委员会、"
            "ESG工作小组、环境社会治理委员会。"
            "原始值填'是'/'否'/'已设立'/'未设立'等原文描述。"
        ),
    )
    external_esg_rating: Optional[RawMetric] = Field(
        None,
        description=(
            "第三方主流机构ESG评级得分或等级。别名：ESG评级、MSCI ESG评级、"
            "富时罗素ESG评分、标普全球ESG评分、Sustainalytics评分、"
            "明晟ESG评级、ESG综合评分、第三方评级结果。"
            "原始值填评级机构名+等级，如'MSCI: AA'、'BBB'。"
        ),
    )


class BankingMetricsRaw(BaseModel):
    """银行专属指标原始提取。"""
    green_finance_balance: Optional[RawMetric] = Field(
        None,
        description=(
            "绿色金融/绿色信贷业务总余额。别名：绿色贷款余额、绿色信贷总量、"
            "绿色金融贷款余额、绿色信贷规模、绿色贷款总额、绿色融资余额、"
            "绿色金融业务余额、清洁能源贷款余额。单位通常为：亿元、万亿元。"
        ),
    )
    inclusive_finance_balance: Optional[RawMetric] = Field(
        None,
        description=(
            "普惠型小微企业及涉农贷款余额。别名：普惠金融贷款余额、普惠型小微贷款、"
            "小微企业贷款余额、涉农贷款余额、普惠贷款规模、"
            "普惠型贷款总额、小微及涉农贷款、普惠金融业务余额。"
            "单位通常为：亿元、万亿元。"
        ),
    )


class AutoMetricsRaw(BaseModel):
    """新能源/汽车制造专属指标原始提取。"""
    scope_3_emissions: Optional[RawMetric] = Field(
        None,
        description=(
            "范围三价值链碳排放量（含供应链及产品使用阶段）。别名：范围3排放、"
            "Scope 3排放量、价值链排放、供应链碳排放、间接价值链温室气体排放、"
            "全生命周期碳排放（范围三部分）。单位通常为：万吨CO₂当量、tCO₂e。"
        ),
    )
    rd_investment_total: Optional[RawMetric] = Field(
        None,
        description=(
            "研发投入总额。别名：研发费用、研究与开发支出、研发投资总额、"
            "R&D投入、技术研发费用、研发经费、年度研发费用、研究开发费用总额。"
            "单位通常为：亿元、万元。"
        ),
    )
    supplier_esg_audit_ratio: Optional[RawMetric] = Field(
        None,
        description=(
            "核心供应商ESG审核/评估覆盖率。别名：供应商ESG评估覆盖率、"
            "核心供应商审核比例、供应链ESG审查覆盖、供应商可持续发展审计覆盖率、"
            "战略供应商ESG评估比例。单位通常为：%、百分比。"
        ),
    )


class PowerMetricsRaw(BaseModel):
    """电力行业专属指标原始提取。"""
    scope_3_emissions: Optional[RawMetric] = Field(
        None,
        description=(
            "范围三价值链碳排放量。别名：范围3排放、Scope 3排放量、"
            "价值链间接排放、燃料上游排放、电力输配损耗排放。"
            "单位通常为：万吨CO₂当量、tCO₂e。"
        ),
    )
    clean_energy_ratio: Optional[RawMetric] = Field(
        None,
        description=(
            "清洁能源装机容量占比或清洁/可再生能源发电量占比。"
            "别名：清洁能源占比、可再生能源装机比例、非化石能源占比、"
            "绿色能源发电占比、清洁电力占比、可再生能源装机容量占比、"
            "清洁能源装机比重。单位通常为：%、百分比。"
        ),
    )
    rd_investment_total: Optional[RawMetric] = Field(
        None,
        description=(
            "研发投入总额。别名：研发费用、研究与开发支出、R&D投入、"
            "技术研发费用、研发经费总额。单位通常为：亿元、万元。"
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# 2.  单位归一化（确定性 Python 逻辑，不依赖 LLM）
# ══════════════════════════════════════════════════════════════════════════════

# 目标标准单位
STD_UNITS = {
    "scope_1_emissions":          "tCO2e",
    "scope_2_emissions":          "tCO2e",
    "scope_3_emissions":          "tCO2e",
    "total_energy_consumption":   "GJ",
    "energy_intensity":           "GJ/亿元",
    "green_finance_balance":      "亿元",
    "inclusive_finance_balance":  "亿元",
    "rd_investment_total":        "亿元",
    "charitable_donations":       "万元",
    "regulatory_penalties":       "次",        # 次数优先；金额单独放 notes
    "clean_energy_ratio":         "%",
    "supplier_esg_audit_ratio":   "%",
    "independent_director_ratio": "%",
    "female_director_ratio":      "%",
    "anti_corruption_coverage":   "%",
    "customer_complaint_res":     "%",
    "employee_training_hours":    "小时/人",
    "safety_accidents_count":     "次",
    "esg_committee_setup":        "bool",      # 特殊：转布尔
    "external_esg_rating":        "text",      # 特殊：保留文本
}

# 换算因子表: (原始单位规范化字符串) -> 乘以该系数 -> 目标单位
UNIT_FACTORS: dict[str, dict[str, float]] = {
    "tco2e": {"tCO2e": 1.0},
    "吨co2e": {"tCO2e": 1.0},
    "吨co₂e": {"tCO2e": 1.0},
    "吨二氧化碳当量": {"tCO2e": 1.0},
    "吨二氧化碳": {"tCO2e": 1.0},
    "吨co2当量": {"tCO2e": 1.0},
    "吨co₂当量": {"tCO2e": 1.0},
    "公吨二氧化碳当量": {"tCO2e": 1.0},
    "噸二氧化碳當量": {"tCO2e": 1.0},
    "噸co2e": {"tCO2e": 1.0},
    "噸co₂e": {"tCO2e": 1.0},
    "噸CO₂e": {"tCO2e": 1.0},
    "吨CO₂e": {"tCO2e": 1.0},

    "万吨co2e": {"tCO2e": 1e4},
    "万吨co₂e": {"tCO2e": 1e4},
    "万吨二氧化碳当量": {"tCO2e": 1e4},
    "万吨二氧化碳": {"tCO2e": 1e4},
    "万吨co2当量": {"tCO2e": 1e4},
    "万吨co₂当量": {"tCO2e": 1e4},
    "萬噸co2當量": {"tCO2e": 1e4},
    "萬噸二氧化碳當量": {"tCO2e": 1e4},

    "千吨co2e": {"tCO2e": 1e3},
    "千吨co₂e": {"tCO2e": 1e3},
    "千吨co2当量": {"tCO2e": 1e3},
    "千吨co₂当量": {"tCO2e": 1e3},

    "亿吨co2e": {"tCO2e": 1e8},
    "亿吨co₂e": {"tCO2e": 1e8},
    "亿吨二氧化碳当量": {"tCO2e": 1e8},

    "吨": {"tCO2e": 1.0, "t": 1.0},
    "万吨": {"tCO2e": 1e4, "t": 1e4},
    "千吨": {"tCO2e": 1e3, "t": 1e3},
    "噸": {"tCO2e": 1.0, "t": 1.0},
    "萬噸": {"tCO2e": 1e4, "t": 1e4},
    "千噸": {"tCO2e": 1e3, "t": 1e3},

    # ──────────────────────────────────────────────────────────────────────────
    # 能源类 → 目标单位 GJ（或复合单位 GJ/亿元）
    # ──────────────────────────────────────────────────────────────────────────
    "gj": {"GJ": 1.0},
    "千兆焦耳": {"GJ": 1000.0},
    "万gj": {"GJ": 1e4},

    "tce": {"GJ": 29.307},
    "吨标准煤": {"GJ": 29.307},
    "万吨标准煤": {"GJ": 29.307e4},
    "万tce": {"GJ": 29.307e4},
    "噸標準煤": {"GJ": 29.307},

    "mwh": {"GJ": 3.6},
    "兆瓦时": {"GJ": 3.6},
    "兆瓦時": {"GJ": 3.6},
    "万mwh": {"GJ": 3.6e4},
    "万千瓦时": {"GJ": 36.0},

    "kwh": {"GJ": 0.0036},
    "千瓦时": {"GJ": 0.0036},
    "万kwh": {"GJ": 36.0},
    "亿kwh": {"GJ": 3.6e5},
    "亿千瓦时": {"GJ": 3.6e5},

    "兆瓦时/亿元": {"GJ/亿元": 3.6},
    "mwh/亿元": {"GJ/亿元": 3.6},
    "万千瓦时/亿元": {"GJ/亿元": 36.0},
    "吨标准煤/亿元": {"GJ/亿元": 29.307},

    # ──────────────────────────────────────────────────────────────────────────
    # 金融金额类 → 目标单位 亿元 / 万元（捐赠）
    # ──────────────────────────────────────────────────────────────────────────
    "元": {"亿元": 1e-8, "万元": 1e-4},
    "万元": {"亿元": 1e-4, "万元": 1.0},
    "亿元": {"亿元": 1.0, "万元": 1e4},
    "百亿元": {"亿元": 100.0, "万元": 1e6},
    "万亿元": {"亿元": 1e4, "万元": 1e8},
    "萬元": {"亿元": 1e-4, "万元": 1.0},
    "億元": {"亿元": 1.0, "万元": 1e4},
    "萬億元": {"亿元": 1e4, "万元": 1e8},

    "万元_to_万元": {"万元": 1.0},

    # ──────────────────────────────────────────────────────────────────────────
    # 百分比类 → 目标单位 %
    # ──────────────────────────────────────────────────────────────────────────
    "%": {"%": 1.0},
    "百分比": {"%": 1.0},
    "％": {"%": 1.0},
    "percent": {"%": 1.0},

    # ──────────────────────────────────────────────────────────────────────────
    # 工时/培训类 → 目标单位 小时/人
    # ──────────────────────────────────────────────────────────────────────────
    # 【核心修复】修复报错：学时、小時、天
    "小时/人": {"小时/人": 1.0},
    "小時/人": {"小时/人": 1.0},
    "学时": {"小时/人": 1.0},
    "學時": {"小时/人": 1.0},
    "小时": {"小时/人": 1.0},
    "小時": {"小时/人": 1.0},
    "天/人": {"小时/人": 8.0},
    "天": {"小时/人": 8.0},
    "工作日/人": {"小时/人": 8.0},


    "次": {"次": 1.0},
    "起": {"次": 1.0},
    "項": {"次": 1.0},
    "项": {"次": 1.0},
    "人": {"次": 1.0},
    "人数": {"次": 1.0},
}


def _normalize_unit_key(unit: str) -> str:
    """把原始单位字符串规范化为 UNIT_FACTORS 的键。"""
    u = unit.strip().lower()
    u = u.replace("₂", "2").replace("²", "2")
    u = u.replace(" ", "").replace("　", "")
    return u


def _extract_numeric(raw_value: str) -> Optional[float]:
    """从原始值字符串提取数字，处理千分位逗号、约数前缀、范围取中间值。"""
    if not raw_value:
        return None

    text = raw_value.strip()

    # 约数标记去除
    text = re.sub(r"[约≈~大约]", "", text)

    # 去除千分位逗号
    text = text.replace(",", "").replace("，", "")

    # 范围值取均值，如 "100-120" → 110
    range_match = re.match(r"([\d.]+)\s*[-–—]\s*([\d.]+)", text)
    if range_match:
        a, b = float(range_match.group(1)), float(range_match.group(2))
        return (a + b) / 2

    # 提取第一个数字
    num_match = re.search(r"[-+]?[\d]+\.?[\d]*", text)
    if num_match:
        return float(num_match.group())

    return None


def normalize_metric(
    metric_key: str,
    raw: Optional[RawMetric],
) -> dict:
    """
    把一个 RawMetric 转换为标准值。
    返回 dict：{std_value, std_unit, data_quality, confidence_penalty}
    """
    if raw is None or raw.raw_value is None:
        return {
            "std_value": None,
            "std_unit": STD_UNITS.get(metric_key, ""),
            "data_quality": "missing",
            "confidence_penalty": 0.0,
        }

    target_unit = STD_UNITS.get(metric_key, "")

    # ── 特殊：布尔类型（esg_committee_setup）──────────────────────────────
    if target_unit == "bool":
        positive = ["是", "已设立", "已建立", "设有", "成立", "yes", "true", "√"]
        val_lower = raw.raw_value.lower().strip()
        std_value = 1 if any(p in val_lower for p in positive) else 0
        return {
            "std_value": float(std_value),
            "std_unit": "bool(1=是/0=否)",
            "data_quality": "normal",
            "confidence_penalty": 0.0,
        }

    # ── 特殊：文本类型（external_esg_rating）─────────────────────────────
    if target_unit == "text":
        return {
            "std_value": None,             # 文本不存数值列
            "std_unit": "text",
            "data_quality": "text_only",
            "confidence_penalty": 0.0,
        }

    # ── 百分比类型 ─────────────────────────────────────────────────────────
    if target_unit == "%":
        numeric = _extract_numeric(raw.raw_value)
        if numeric is None:
            return {"std_value": None, "std_unit": "%",
                    "data_quality": "parse_failed", "confidence_penalty": -0.3}
        # 如果值 > 1 且原始单位是小数形式 0.xx，乘 100
        if numeric <= 1.0 and (raw.raw_unit or "").strip() in ["", "小数"]:
            numeric *= 100
        penalty = -0.1 if "约" in (raw.raw_value or "") else 0.0
        return {"std_value": round(numeric, 4), "std_unit": "%",
                "data_quality": "estimated" if penalty else "normal",
                "confidence_penalty": penalty}

    # ── 通用数值换算 ───────────────────────────────────────────────────────
    numeric = _extract_numeric(raw.raw_value)
    if numeric is None:
        return {"std_value": None, "std_unit": target_unit,
                "data_quality": "parse_failed", "confidence_penalty": -0.3}

    raw_unit_key = _normalize_unit_key(raw.raw_unit or "")

    # 捐赠特殊：目标是万元
    if metric_key == "charitable_donations":
        donation_map = {
            "万元": 1.0, "亿元": 1e4, "元": 1e-4,
            "百万元": 100.0, "千万元": 1000.0,
        }
        factor = donation_map.get(raw_unit_key, None)
        if factor is None:
            factor = donation_map.get((raw.raw_unit or "").strip(), 1.0)
        std_value = numeric * factor
        quality = "estimated" if "约" in (raw.raw_value or "") else "normal"
        if raw.raw_unit and (raw.raw_unit.strip() not in donation_map):
            quality = "unit_converted"
        return {"std_value": round(std_value, 4), "std_unit": "万元",
                "data_quality": quality, "confidence_penalty": 0.0}

    # 普通换算：查 UNIT_FACTORS
    target_unit_key = _normalize_unit_key(target_unit)

    if raw_unit_key == target_unit_key:
        factor = 1.0
    else:
        factors_for_raw = UNIT_FACTORS.get(raw_unit_key, {})
        factor = factors_for_raw.get(target_unit, None)

    if factor is None:
        # 找不到换算关系 → 保存原始值，标记
        log.warning(f"  ⚠️  无法换算单位 '{raw.raw_unit}' → '{target_unit}'  "
                    f"(metric={metric_key}, value={raw.raw_value})")
        return {
            "std_value": numeric,
            "std_unit": raw.raw_unit or "unknown",
            "data_quality": "unit_not_converted",
            "confidence_penalty": -0.2,
        }

    std_value = numeric * factor
    quality = "estimated" if "约" in (raw.raw_value or "") else "normal"
    if raw_unit_key != _normalize_unit_key(target_unit):
        quality = quality if quality == "estimated" else "unit_converted"

    return {
        "std_value": round(std_value, 6),
        "std_unit": target_unit,
        "data_quality": quality,
        "confidence_penalty": 0.0,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 3.  数据可信度校验（写库前）
# ══════════════════════════════════════════════════════════════════════════════

# 各指标的合理值范围（用于异常检测）
# 格式: metric_key -> (min_plausible, max_plausible, unit_for_check)
PLAUSIBILITY_RANGES = {
    "scope_1_emissions":          (0,      5e8,   "tCO2e"),   # 0 ~ 5亿吨
    "scope_2_emissions":          (0,      1e8,   "tCO2e"),
    "scope_3_emissions":          (0,      1e9,   "tCO2e"),
    "total_energy_consumption":   (0,      1e10,  "GJ"),
    "energy_intensity":           (0,      1e6,   "GJ/亿元"),
    "green_finance_balance":      (0,      5e5,   "亿元"),     # 最高50万亿
    "inclusive_finance_balance":  (0,      5e5,   "亿元"),
    "rd_investment_total":        (0,      1e4,   "亿元"),
    "charitable_donations":       (0,      1e8,   "万元"),
    "clean_energy_ratio":         (0,      100,   "%"),
    "supplier_esg_audit_ratio":   (0,      100,   "%"),
    "independent_director_ratio": (0,      100,   "%"),
    "female_director_ratio":      (0,      100,   "%"),
    "anti_corruption_coverage":   (0,      100,   "%"),
    "customer_complaint_res":     (0,      100,   "%"),
    "employee_training_hours":    (0,      2000,  "小时/人"),  # 不超过全年工时
    "safety_accidents_count":     (0,      1e5,   "次"),
    "regulatory_penalties":       (0,      1e4,   "次"),
    "esg_committee_setup":        (0,      1,     "bool"),
}


def validate_metric_value(
    metric_key: str,
    std_value: Optional[float],
    company_name: str,
    year: int,
    conn: sqlite3.Connection,
) -> dict:
    """
    三层可信度校验：
      1. 合理区间校验（规则）
      2. 跨期波动校验（同公司上一年数据对比）
      3. 行业分位校验（与同行业同年均值对比）

    返回: {confidence: float, warnings: list[str]}
    """
    confidence = 1.0
    warnings   = []

    if std_value is None:
        return {"confidence": 0.0, "warnings": ["值为空/未披露"]}

    # ── 层1：绝对合理区间 ─────────────────────────────────────────────────
    if metric_key in PLAUSIBILITY_RANGES:
        lo, hi, _ = PLAUSIBILITY_RANGES[metric_key]
        if not (lo <= std_value <= hi):
            warnings.append(
                f"数值 {std_value} 超出合理区间 [{lo}, {hi}]，疑似单位换算错误"
            )
            confidence -= 0.4

    # ── 层2：跨期波动校验（同公司上一年） ────────────────────────────────
    prev_year = year - 1
    # 先在通用表查
    tables_to_check = ["esg_universal_metrics", "esg_banking_metrics",
                       "esg_auto_metrics", "esg_power_metrics"]
    prev_value = None
    for tbl in tables_to_check:
        try:
            cur = conn.execute(
                f"SELECT {metric_key} FROM {tbl} "
                "WHERE company_name=? AND year=?",
                (company_name, prev_year),
            )
            row = cur.fetchone()
            if row and row[0] is not None:
                prev_value = row[0]
                break
        except sqlite3.OperationalError:
            continue

    if prev_value is not None and prev_value != 0:
        change_pct = abs(std_value - prev_value) / abs(prev_value)
        if change_pct > 2.0:   # 同比变化超过200%
            warnings.append(
                f"同比变化 {change_pct*100:.0f}%（{prev_year}年={prev_value}），"
                "波动异常，请核查"
            )
            confidence -= 0.2

    # ── 层3：行业分位校验（同行业同年中位数） ─────────────────────────────
    # 只在已有足够数据时做
    for tbl in tables_to_check:
        try:
            cur = conn.execute(
                f"SELECT AVG({metric_key}), COUNT({metric_key}) "
                f"FROM {tbl} WHERE year=? AND {metric_key} IS NOT NULL",
                (year,),
            )
            row = cur.fetchone()
            if row and row[1] and row[1] >= 3:  # 至少3家才有参考价值
                peer_avg = row[0]
                if peer_avg and peer_avg != 0:
                    ratio = std_value / peer_avg
                    if ratio > 20 or ratio < 0.05:
                        warnings.append(
                            f"数值为行业均值的 {ratio:.1f} 倍（均值={peer_avg:.2f}），"
                            "可能量级有误"
                        )
                        confidence -= 0.15
            break
        except sqlite3.OperationalError:
            continue

    confidence = max(0.0, round(confidence, 2))
    return {"confidence": confidence, "warnings": warnings}


# ══════════════════════════════════════════════════════════════════════════════
# 4.  数据库建表
# ══════════════════════════════════════════════════════════════════════════════

DDL_UNIVERSAL = """
CREATE TABLE IF NOT EXISTS esg_universal_metrics (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name                TEXT    NOT NULL,
    year                        INTEGER NOT NULL,
    industry                    TEXT    NOT NULL,   -- 'bank'|'new_energy'|'power'

    -- ── 14 个通用指标（标准值） ─────────────────────────────────────────
    scope_1_emissions           REAL,   -- tCO2e
    scope_2_emissions           REAL,   -- tCO2e
    total_energy_consumption    REAL,   -- GJ
    energy_intensity            REAL,   -- GJ/亿元
    employee_training_hours     REAL,   -- 小时/人
    safety_accidents_count      REAL,   -- 次
    customer_complaint_res      REAL,   -- %
    charitable_donations        REAL,   -- 万元
    independent_director_ratio  REAL,   -- %
    female_director_ratio       REAL,   -- %
    anti_corruption_coverage    REAL,   -- %
    regulatory_penalties        REAL,   -- 次
    esg_committee_setup         REAL,   -- 1=是/0=否
    external_esg_rating         TEXT,   -- 评级文本

    -- ── 原始值（溯源用） ─────────────────────────────────────────────────
    raw_scope_1                 TEXT,   -- JSON: {raw_value, raw_unit, page, excerpt}
    raw_scope_2                 TEXT,
    raw_energy_total            TEXT,
    raw_energy_intensity        TEXT,
    raw_training_hours          TEXT,
    raw_safety_accidents        TEXT,
    raw_complaint_res           TEXT,
    raw_donations               TEXT,
    raw_ind_dir_ratio           TEXT,
    raw_female_dir_ratio        TEXT,
    raw_anti_corruption         TEXT,
    raw_penalties               TEXT,
    raw_esg_committee           TEXT,
    raw_esg_rating              TEXT,

    -- ── 数据质量 ─────────────────────────────────────────────────────────
    data_quality                TEXT    DEFAULT '{}',  -- JSON: {metric_key: quality}
    confidence_scores           TEXT    DEFAULT '{}',  -- JSON: {metric_key: float}
    validation_warnings         TEXT    DEFAULT '{}',  -- JSON: {metric_key: [warnings]}

    -- ── 元信息 ───────────────────────────────────────────────────────────
    source_file                 TEXT,
    extraction_method           TEXT    DEFAULT 'llm_pydantic',
    created_at                  TEXT    DEFAULT (datetime('now')),
    updated_at                  TEXT    DEFAULT (datetime('now')),

    UNIQUE(company_name, year)
);
"""

DDL_BANKING = """
CREATE TABLE IF NOT EXISTS esg_banking_metrics (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name                TEXT    NOT NULL,
    year                        INTEGER NOT NULL,

    green_finance_balance       REAL,   -- 亿元
    inclusive_finance_balance   REAL,   -- 亿元

    raw_green_finance           TEXT,
    raw_inclusive_finance       TEXT,

    data_quality                TEXT    DEFAULT '{}',
    confidence_scores           TEXT    DEFAULT '{}',
    validation_warnings         TEXT    DEFAULT '{}',

    source_file                 TEXT,
    created_at                  TEXT    DEFAULT (datetime('now')),

    UNIQUE(company_name, year),
    FOREIGN KEY(company_name, year)
        REFERENCES esg_universal_metrics(company_name, year)
        ON DELETE CASCADE
);
"""

DDL_AUTO = """
CREATE TABLE IF NOT EXISTS esg_auto_metrics (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name                TEXT    NOT NULL,
    year                        INTEGER NOT NULL,

    scope_3_emissions           REAL,   -- tCO2e
    rd_investment_total         REAL,   -- 亿元
    supplier_esg_audit_ratio    REAL,   -- %

    raw_scope_3                 TEXT,
    raw_rd_investment           TEXT,
    raw_supplier_audit          TEXT,

    data_quality                TEXT    DEFAULT '{}',
    confidence_scores           TEXT    DEFAULT '{}',
    validation_warnings         TEXT    DEFAULT '{}',

    source_file                 TEXT,
    created_at                  TEXT    DEFAULT (datetime('now')),

    UNIQUE(company_name, year),
    FOREIGN KEY(company_name, year)
        REFERENCES esg_universal_metrics(company_name, year)
        ON DELETE CASCADE
);
"""

DDL_POWER = """
CREATE TABLE IF NOT EXISTS esg_power_metrics (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name                TEXT    NOT NULL,
    year                        INTEGER NOT NULL,

    scope_3_emissions           REAL,   -- tCO2e
    clean_energy_ratio          REAL,   -- %
    rd_investment_total         REAL,   -- 亿元

    raw_scope_3                 TEXT,
    raw_clean_energy            TEXT,
    raw_rd_investment           TEXT,

    data_quality                TEXT    DEFAULT '{}',
    confidence_scores           TEXT    DEFAULT '{}',
    validation_warnings         TEXT    DEFAULT '{}',

    source_file                 TEXT,
    created_at                  TEXT    DEFAULT (datetime('now')),

    UNIQUE(company_name, year),
    FOREIGN KEY(company_name, year)
        REFERENCES esg_universal_metrics(company_name, year)
        ON DELETE CASCADE
);
"""

DDL_MISSING_LOG = """
CREATE TABLE IF NOT EXISTS missing_data_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name    TEXT NOT NULL,
    year            INTEGER NOT NULL,
    metric_key      TEXT NOT NULL,
    industry        TEXT,
    missing_reason  TEXT,
    -- 'not_disclosed' | 'parse_failed' | 'report_missing' | 'not_applicable'
    source_file     TEXT,
    logged_at       TEXT DEFAULT (datetime('now')),
    UNIQUE(company_name, year, metric_key)
);
"""


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    for ddl in [DDL_UNIVERSAL, DDL_BANKING, DDL_AUTO, DDL_POWER, DDL_MISSING_LOG]:
        conn.executescript(ddl)
    conn.commit()
    log.info(f"数据库初始化完成：{db_path}")
    return conn


# ══════════════════════════════════════════════════════════════════════════════
# 5.  PDF 文本提取
# ══════════════════════════════════════════════════════════════════════════════

def extract_pdf_text(pdf_path: str, max_pages: int = 200) -> list[dict]:
    """
    返回列表，每项为一页的信息：
    {page_num: int, text: str, char_count: int}
    """
    pages = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            total = min(len(pdf.pages), max_pages)
            for i, page in enumerate(pdf.pages[:total]):
                text = page.extract_text() or ""
                # 表格也提取出来拼入文本
                for tbl in page.extract_tables() or []:
                    for row in tbl:
                        text += " ".join(str(c) for c in row if c) + "\n"
                pages.append({
                    "page_num": i + 1,
                    "text": text.strip(),
                    "char_count": len(text),
                })
    except Exception as e:
        log.error(f"PDF 提取失败 {pdf_path}: {e}")
    return pages


def build_context_window(pages: list[dict], max_chars: int = 35000) -> str:
    """
    【升级版】页面级关键词预过滤机制
    只将包含高频 ESG 指标特征词的页面组合成上下文，极大节省 Token 并防止超载。
    max_chars 设为 35000（约 2 万 Token），完美适配绝大多数 Qwen 模型。
    """
    # 建立强大的 ESG 诱导词典
    esg_keywords = [
        "排放", "温室气体", "范围一", "范围二", "范围三", "碳", "tCO2e",
        "能耗", "能源", "电力", "兆瓦", "GJ", "用水",
        "培训", "课时", "小时/人", "事故", "伤亡", "死亡", "安全",
        "投诉", "满意度", "捐赠", "公益", "万元", "亿元",
        "董事", "女性", "比例", "反腐", "贪污", "处罚", "违规", "评级",
        "绿色信贷", "普惠", "研发", "供应商", "审查",
        "scope 1", "scope 2", "scope 3", "标准煤",  "MWh",
        "绿色贷款", "占比", "工亡", "客户", "慈善",
        "一", "二", "三", "四", "五", "六", "七", "八", "九", "十"
    ]

    # 1. 为每一页打分（词频统计）
    for p in pages:
        text = p["text"]
        # 简单粗暴的词频统计，包含数字的页面额外加分
        keyword_score = sum(text.count(kw) for kw in esg_keywords)
        number_score = len(re.findall(r'\d+', text)) * 0.1

        # 赋予报告最后 20 页极高的权重（因为 ESG 报告通常把 KPI 数据总表放在最后）
        position_bonus = 20 if p["page_num"] > len(pages) - 20 else 0

        p["score"] = keyword_score + number_score + position_bonus

    # 2. 剔除完全不相关的垃圾页 (得分过低)
    relevant_pages = [p for p in pages if p["score"] > 5]

    # 3. 按得分从高到低排序，优先选取数据最密集的页面
    relevant_pages.sort(key=lambda x: x["score"], reverse=True)

    selected_pages = []
    current_chars = 0

    for p in relevant_pages:
        if current_chars + p["char_count"] <= max_chars:
            selected_pages.append(p)
            current_chars += p["char_count"]
        else:
            break  # 达到长度上限，停止添加

    # 4. 【关键】将选出的高分页面重新按物理页码排序，恢复阅读连贯性
    selected_pages.sort(key=lambda x: x["page_num"])

    parts = []
    for p in selected_pages:
        parts.append(f"【第{p['page_num']}页】\n{p['text']}")

    return "\n\n...[已过滤非核心页面]...\n\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# 6.  LLM 提取（Pydantic 结构化输出）
# ══════════════════════════════════════════════════════════════════════════════

def _build_gemini_system_prompt(industry: str) -> str:
    """构建 Gemini 专用的 System Prompt（独立出来，因为 Gemini 支持 System Instruction）。"""
    industry_hint = {
        "bank":       "该报告来自银行/金融行业，需重点关注绿色金融和普惠金融指标。",
        "new_energy": "该报告来自新能源汽车制造行业，需重点关注碳排放范围三、研发投入和供应链ESG指标。",
        "power":      "该报告来自电力行业，需重点关注清洁能源比例、碳排放和研发投入指标。",
    }.get(industry, "")

    return f"""你是一个专业的 ESG 报告数据提取专家。
{industry_hint}

你的任务：从给定的 ESG 报告文本中，精确提取指定指标的【原始数值】和【原始单位】。

严格规则：
1. 只提取报告中明确出现的数据，绝对不允许推算、估算或捏造数据。
2. 找不到某指标时，必须返回 null，严禁填写 0 或任何假设值。
3. 每个指标必须提供：raw_value（原文数字字符串）、raw_unit（原文单位）、
   page（页码）、excerpt（含该数据的原文片段，≤80字）。
4. 如果一个指标有多年数据，只取报告所属年度的数据。
5. raw_value 保留原始字符串，如"约1,234.5"，不要自行换算。
6. 返回合法的 JSON，不要包含任何解释文字。"""


def _call_qwen_with_schema(
        system_prompt: str,
        user_context: str,
        response_schema: type[BaseModel],
        temperature: float = 0.0,
) -> dict:
    global CURRENT_MODEL_IDX

    # 将 Pydantic Schema 转换为 JSON 格式的字符串，喂给 Qwen
    schema_str = json.dumps(response_schema.model_json_schema(), ensure_ascii=False)

    user_input = (
        f"请从以下 ESG 报告文本中提取数据：\n\n{user_context}\n\n"
        f"【强制要求】请严格按照以下 JSON Schema 的结构输出，缺失字段填 null，不要输出任何解释说明文本：\n"
        f"{schema_str}"
    )

    # 重试循环：最多重试模型池里的所有模型
    for _ in range(len(QWEN_MODELS)):
        current_model = QWEN_MODELS[CURRENT_MODEL_IDX]
        try:
            log.info(f"正在使用模型 [{current_model}] 进行抽取...")
            response = client.chat.completions.create(
                model=current_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_input}
                ],
                # 开启 JSON Mode 保证输出合法的 JSON
                response_format={"type": "json_object"},
                temperature=temperature
            )

            raw_json = response.choices[0].message.content.strip()

            # 清理可能存在的 Markdown 标签兜底
            raw_json = re.sub(r"^```json\s*", "", raw_json)
            raw_json = re.sub(r"^```\s*", "", raw_json)
            raw_json = re.sub(r"\s*```$", "", raw_json)

            # 使用 Pydantic 验证并解析返回结果
            parsed_result = response_schema.model_validate_json(raw_json)
            return parsed_result.model_dump()

        except Exception as e:
            error_msg = str(e).lower()
            # 捕捉阿里云额度耗尽、欠费或并发超限相关的常见报错关键词
            if any(keyword in error_msg for keyword in ["quota", "balance", "limit", "arrears", "insufficient"]):
                log.warning(f"⚠️ 模型 {current_model} 额度耗尽或限流！自动切换下一个模型...")
                # 索引 + 1，循环切换
                CURRENT_MODEL_IDX = (CURRENT_MODEL_IDX + 1) % len(QWEN_MODELS)
            else:
                # 如果是其他错误（比如 JSON 解析失败），直接抛出
                log.error(f"Qwen API 调用失败: {e}")
                raise e

    # 如果所有模型都遍历完了还是报错
    raise Exception("🚨 严重错误：所有 Qwen 模型的免费额度均已耗尽！请充值或更换 API Key。")


def extract_metrics_with_llm(
    context: str,
    industry: str,
) -> tuple[UniversalMetricsRaw, object]:
    """
    两次 Gemini 调用：
      第一次提取通用指标 → UniversalMetricsRaw
      第二次提取行业专属指标 → Banking/Auto/PowerMetricsRaw

    返回 (universal_raw, industry_specific_raw)
    """
    system_prompt = _build_gemini_system_prompt(industry)

    # ── 通用指标提取 ─────────────────────────────────────────────────────
    try:
        raw_u = _call_qwen_with_schema(
            system_prompt=system_prompt,
            user_context=context,
            response_schema=UniversalMetricsRaw,
        )
        universal_raw = UniversalMetricsRaw.model_validate(raw_u)
    except Exception as e:
        log.error(f"通用指标提取失败: {e}")
        universal_raw = UniversalMetricsRaw()

    # ── 行业专属指标提取 ─────────────────────────────────────────────────
    if industry == "bank":
        spec_cls = BankingMetricsRaw
    elif industry == "new_energy":
        spec_cls = AutoMetricsRaw
    else:
        spec_cls = PowerMetricsRaw

    try:
        raw_s = _call_qwen_with_schema(
            system_prompt=system_prompt,
            user_context=context,
            response_schema=spec_cls,
        )
        industry_raw = spec_cls.model_validate(raw_s)
    except Exception as e:
        log.error(f"行业专属指标提取失败: {e}")
        industry_raw = spec_cls()

    return universal_raw, industry_raw


# ══════════════════════════════════════════════════════════════════════════════
# 7.  写入数据库
# ══════════════════════════════════════════════════════════════════════════════

def _raw_to_json(raw: Optional[RawMetric]) -> Optional[str]:
    """RawMetric → JSON 字符串（存入 raw_* 列）。"""
    if raw is None:
        return None
    return json.dumps({
        "raw_value": raw.raw_value,
        "raw_unit":  raw.raw_unit,
        "page":      raw.page,
        "excerpt":   raw.excerpt,
    }, ensure_ascii=False)


def _norm(metric_key, raw_metric) -> dict:
    """简化调用 normalize_metric。"""
    return normalize_metric(metric_key, raw_metric)


def upsert_universal(
    conn: sqlite3.Connection,
    company_name: str,
    year: int,
    industry: str,
    source_file: str,
    u: UniversalMetricsRaw,
) -> dict:
    """写入通用指标表，返回 {metric_key: std_value} 字典供子表参考。"""

    # 逐指标归一化 + 可信度校验
    metrics_map = {
        "scope_1_emissions":         u.scope_1_emissions,
        "scope_2_emissions":         u.scope_2_emissions,
        "total_energy_consumption":  u.total_energy_consumption,
        "energy_intensity":          u.energy_intensity,
        "employee_training_hours":   u.employee_training_hours,
        "safety_accidents_count":    u.safety_accidents_count,
        "customer_complaint_res":    u.customer_complaint_res,
        "charitable_donations":      u.charitable_donations,
        "independent_director_ratio":u.independent_director_ratio,
        "female_director_ratio":     u.female_director_ratio,
        "anti_corruption_coverage":  u.anti_corruption_coverage,
        "regulatory_penalties":      u.regulatory_penalties,
        "esg_committee_setup":       u.esg_committee_setup,
        "external_esg_rating":       u.external_esg_rating,
    }

    std_values    = {}
    quality_map   = {}
    confidence_map= {}
    warnings_map  = {}

    for key, raw_metric in metrics_map.items():
        norm = _norm(key, raw_metric)
        std_values[key]     = norm["std_value"]
        quality_map[key]    = norm["data_quality"]

        val_result = validate_metric_value(
            key, norm["std_value"], company_name, year, conn
        )
        base_conf = 0.9 if norm["data_quality"] in ("normal", "unit_converted") else \
                    0.7 if norm["data_quality"] == "estimated" else \
                    0.5 if norm["data_quality"] == "parse_failed" else \
                    0.0
        confidence_map[key] = max(0.0, round(
            base_conf + norm["confidence_penalty"] + val_result["confidence"] - 1.0, 2
        ))
        # 简化：直接用 validate 结果的 confidence
        confidence_map[key] = val_result["confidence"]
        if norm["data_quality"] in ("parse_failed", "unit_not_converted"):
            confidence_map[key] = min(confidence_map[key], 0.5)
        if norm["data_quality"] == "missing":
            confidence_map[key] = 0.0

        if val_result["warnings"]:
            warnings_map[key] = val_result["warnings"]

    # external_esg_rating 是文本，单独存
    esg_rating_text = u.external_esg_rating.raw_value \
                      if u.external_esg_rating else None

    placeholders = ", ".join(["?"] * 35)

    conn.execute(f"""
            INSERT INTO esg_universal_metrics (
                company_name, year, industry,
                scope_1_emissions, scope_2_emissions,
                total_energy_consumption, energy_intensity,
                employee_training_hours, safety_accidents_count,
                customer_complaint_res, charitable_donations,
                independent_director_ratio, female_director_ratio,
                anti_corruption_coverage, regulatory_penalties,
                esg_committee_setup, external_esg_rating,

                raw_scope_1, raw_scope_2, raw_energy_total,
                raw_energy_intensity, raw_training_hours,
                raw_safety_accidents, raw_complaint_res,
                raw_donations, raw_ind_dir_ratio,
                raw_female_dir_ratio, raw_anti_corruption,
                raw_penalties, raw_esg_committee, raw_esg_rating,

                data_quality, confidence_scores, validation_warnings,
                source_file
            ) VALUES ({placeholders})
            ON CONFLICT(company_name, year) DO UPDATE SET
                scope_1_emissions           = excluded.scope_1_emissions,
                scope_2_emissions           = excluded.scope_2_emissions,
                total_energy_consumption    = excluded.total_energy_consumption,
                energy_intensity            = excluded.energy_intensity,
                employee_training_hours     = excluded.employee_training_hours,
                safety_accidents_count      = excluded.safety_accidents_count,
                customer_complaint_res      = excluded.customer_complaint_res,
                charitable_donations        = excluded.charitable_donations,
                independent_director_ratio  = excluded.independent_director_ratio,
                female_director_ratio       = excluded.female_director_ratio,
                anti_corruption_coverage    = excluded.anti_corruption_coverage,
                regulatory_penalties        = excluded.regulatory_penalties,
                esg_committee_setup         = excluded.esg_committee_setup,
                external_esg_rating         = excluded.external_esg_rating,
                raw_scope_1 = excluded.raw_scope_1,
                raw_scope_2 = excluded.raw_scope_2,
                raw_energy_total = excluded.raw_energy_total,
                raw_energy_intensity = excluded.raw_energy_intensity,
                raw_training_hours = excluded.raw_training_hours,
                raw_safety_accidents = excluded.raw_safety_accidents,
                raw_complaint_res = excluded.raw_complaint_res,
                raw_donations = excluded.raw_donations,
                raw_ind_dir_ratio = excluded.raw_ind_dir_ratio,
                raw_female_dir_ratio = excluded.raw_female_dir_ratio,
                raw_anti_corruption = excluded.raw_anti_corruption,
                raw_penalties = excluded.raw_penalties,
                raw_esg_committee = excluded.raw_esg_committee,
                raw_esg_rating = excluded.raw_esg_rating,
                data_quality = excluded.data_quality,
                confidence_scores = excluded.confidence_scores,
                validation_warnings = excluded.validation_warnings,
                updated_at = datetime('now')
        """, (
        company_name, year, industry,
        std_values.get("scope_1_emissions"),
        std_values.get("scope_2_emissions"),
        std_values.get("total_energy_consumption"),
        std_values.get("energy_intensity"),
        std_values.get("employee_training_hours"),
        std_values.get("safety_accidents_count"),
        std_values.get("customer_complaint_res"),
        std_values.get("charitable_donations"),
        std_values.get("independent_director_ratio"),
        std_values.get("female_director_ratio"),
        std_values.get("anti_corruption_coverage"),
        std_values.get("regulatory_penalties"),
        std_values.get("esg_committee_setup"),
        esg_rating_text,
        _raw_to_json(u.scope_1_emissions),
        _raw_to_json(u.scope_2_emissions),
        _raw_to_json(u.total_energy_consumption),
        _raw_to_json(u.energy_intensity),
        _raw_to_json(u.employee_training_hours),
        _raw_to_json(u.safety_accidents_count),
        _raw_to_json(u.customer_complaint_res),
        _raw_to_json(u.charitable_donations),
        _raw_to_json(u.independent_director_ratio),
        _raw_to_json(u.female_director_ratio),
        _raw_to_json(u.anti_corruption_coverage),
        _raw_to_json(u.regulatory_penalties),
        _raw_to_json(u.esg_committee_setup),
        _raw_to_json(u.external_esg_rating),
        json.dumps(quality_map,    ensure_ascii=False),
        json.dumps(confidence_map, ensure_ascii=False),
        json.dumps(warnings_map,   ensure_ascii=False),
        source_file,
    ))
    conn.commit()
    return std_values


def upsert_banking(
    conn, company_name, year, source_file, b: BankingMetricsRaw
):
    fields = {
        "green_finance_balance":     b.green_finance_balance,
        "inclusive_finance_balance": b.inclusive_finance_balance,
    }
    std, quality, confidence, warnings = {}, {}, {}, {}
    for k, raw in fields.items():
        n = _norm(k, raw)
        std[k] = n["std_value"]
        quality[k] = n["data_quality"]
        vr = validate_metric_value(k, n["std_value"], company_name, year, conn)
        confidence[k] = vr["confidence"]
        if vr["warnings"]:
            warnings[k] = vr["warnings"]

    conn.execute("""
        INSERT INTO esg_banking_metrics
            (company_name, year,
             green_finance_balance, inclusive_finance_balance,
             raw_green_finance, raw_inclusive_finance,
             data_quality, confidence_scores, validation_warnings, source_file)
        VALUES (?,?, ?,?, ?,?, ?,?,?,?)
        ON CONFLICT(company_name, year) DO UPDATE SET
            green_finance_balance    = excluded.green_finance_balance,
            inclusive_finance_balance= excluded.inclusive_finance_balance,
            raw_green_finance        = excluded.raw_green_finance,
            raw_inclusive_finance    = excluded.raw_inclusive_finance,
            data_quality             = excluded.data_quality,
            confidence_scores        = excluded.confidence_scores,
            validation_warnings      = excluded.validation_warnings
    """, (
        company_name, year,
        std["green_finance_balance"], std["inclusive_finance_balance"],
        _raw_to_json(b.green_finance_balance),
        _raw_to_json(b.inclusive_finance_balance),
        json.dumps(quality, ensure_ascii=False),
        json.dumps(confidence, ensure_ascii=False),
        json.dumps(warnings, ensure_ascii=False),
        source_file,
    ))
    conn.commit()


def upsert_auto(conn, company_name, year, source_file, a: AutoMetricsRaw):
    fields = {
        "scope_3_emissions":        a.scope_3_emissions,
        "rd_investment_total":      a.rd_investment_total,
        "supplier_esg_audit_ratio": a.supplier_esg_audit_ratio,
    }
    std, quality, confidence, warnings = {}, {}, {}, {}
    for k, raw in fields.items():
        n = _norm(k, raw)
        std[k] = n["std_value"]
        quality[k] = n["data_quality"]
        vr = validate_metric_value(k, n["std_value"], company_name, year, conn)
        confidence[k] = vr["confidence"]
        if vr["warnings"]:
            warnings[k] = vr["warnings"]

    conn.execute("""
        INSERT INTO esg_auto_metrics
            (company_name, year,
             scope_3_emissions, rd_investment_total, supplier_esg_audit_ratio,
             raw_scope_3, raw_rd_investment, raw_supplier_audit,
             data_quality, confidence_scores, validation_warnings, source_file)
        VALUES (?,?, ?,?,?, ?,?,?, ?,?,?,?)
        ON CONFLICT(company_name, year) DO UPDATE SET
            scope_3_emissions        = excluded.scope_3_emissions,
            rd_investment_total      = excluded.rd_investment_total,
            supplier_esg_audit_ratio = excluded.supplier_esg_audit_ratio,
            raw_scope_3              = excluded.raw_scope_3,
            raw_rd_investment        = excluded.raw_rd_investment,
            raw_supplier_audit       = excluded.raw_supplier_audit,
            data_quality             = excluded.data_quality,
            confidence_scores        = excluded.confidence_scores,
            validation_warnings      = excluded.validation_warnings
    """, (
        company_name, year,
        std["scope_3_emissions"], std["rd_investment_total"],
        std["supplier_esg_audit_ratio"],
        _raw_to_json(a.scope_3_emissions),
        _raw_to_json(a.rd_investment_total),
        _raw_to_json(a.supplier_esg_audit_ratio),
        json.dumps(quality, ensure_ascii=False),
        json.dumps(confidence, ensure_ascii=False),
        json.dumps(warnings, ensure_ascii=False),
        source_file,
    ))
    conn.commit()


def upsert_power(conn, company_name, year, source_file, p: PowerMetricsRaw):
    fields = {
        "scope_3_emissions":  p.scope_3_emissions,
        "clean_energy_ratio": p.clean_energy_ratio,
        "rd_investment_total":p.rd_investment_total,
    }
    std, quality, confidence, warnings = {}, {}, {}, {}
    for k, raw in fields.items():
        n = _norm(k, raw)
        std[k] = n["std_value"]
        quality[k] = n["data_quality"]
        vr = validate_metric_value(k, n["std_value"], company_name, year, conn)
        confidence[k] = vr["confidence"]
        if vr["warnings"]:
            warnings[k] = vr["warnings"]

    conn.execute("""
        INSERT INTO esg_power_metrics
            (company_name, year,
             scope_3_emissions, clean_energy_ratio, rd_investment_total,
             raw_scope_3, raw_clean_energy, raw_rd_investment,
             data_quality, confidence_scores, validation_warnings, source_file)
        VALUES (?,?, ?,?,?, ?,?,?, ?,?,?,?)
        ON CONFLICT(company_name, year) DO UPDATE SET
            scope_3_emissions   = excluded.scope_3_emissions,
            clean_energy_ratio  = excluded.clean_energy_ratio,
            rd_investment_total = excluded.rd_investment_total,
            raw_scope_3         = excluded.raw_scope_3,
            raw_clean_energy    = excluded.raw_clean_energy,
            raw_rd_investment   = excluded.raw_rd_investment,
            data_quality        = excluded.data_quality,
            confidence_scores   = excluded.confidence_scores,
            validation_warnings = excluded.validation_warnings
    """, (
        company_name, year,
        std["scope_3_emissions"], std["clean_energy_ratio"],
        std["rd_investment_total"],
        _raw_to_json(p.scope_3_emissions),
        _raw_to_json(p.clean_energy_ratio),
        _raw_to_json(p.rd_investment_total),
        json.dumps(quality, ensure_ascii=False),
        json.dumps(confidence, ensure_ascii=False),
        json.dumps(warnings, ensure_ascii=False),
        source_file,
    ))
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
# 8.  缺失指标日志
# ══════════════════════════════════════════════════════════════════════════════

def log_missing_metrics(
    conn: sqlite3.Connection,
    company_name: str,
    year: int,
    industry: str,
    source_file: str,
    std_values_universal: dict,
    industry_std_values: dict,
):
    """把所有 std_value=None 的必填指标写入 missing_data_log。"""
    required = list(UNIVERSAL_REQUIRED)
    if industry == "bank":
        required += BANKING_REQUIRED
    elif industry == "new_energy":
        required += AUTO_REQUIRED
    else:
        required += POWER_REQUIRED

    all_std = {**std_values_universal, **industry_std_values}

    for key in required:
        val = all_std.get(key)
        if val is None:
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO missing_data_log
                        (company_name, year, metric_key, industry,
                         missing_reason, source_file)
                    VALUES (?,?,?,?, 'not_disclosed', ?)
                """, (company_name, year, key, industry, source_file))
            except sqlite3.IntegrityError:
                pass
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
# 9.  文件名解析工具
# ══════════════════════════════════════════════════════════════════════════════

def parse_filename(filename: str) -> tuple[Optional[str], Optional[int]]:
    """
    从文件名中提取公司名和年份。
    支持格式：工商银行2024年ESG报告.pdf、比亚迪_2023_ESG.pdf 等。
    返回: (company_name, year) 或 (None, None)
    """
    stem = Path(filename).stem

    # 提取年份
    year_match = re.search(r"(20\d{2})", stem)
    if not year_match:
        return None, None
    year = int(year_match.group(1))

    # 公司名：年份之前的部分，去除分隔符
    company_part = stem[:year_match.start()]
    company_name = re.sub(r"[_\-\s]+$", "", company_part).strip()

    if not company_name:
        return None, None

    return company_name, year


# ══════════════════════════════════════════════════════════════════════════════
# 10.  主流程
# ══════════════════════════════════════════════════════════════════════════════

def process_single_pdf(
    pdf_path: Path,
    industry: str,
    conn: sqlite3.Connection,
) -> dict:
    """
    处理单个 PDF 文件的完整流程。
    返回摘要字典。
    """
    filename     = pdf_path.name
    company_name, year = parse_filename(filename)

    if not company_name or not year:
        log.warning(f"无法从文件名解析公司/年份，跳过：{filename}")
        return {"status": "skipped", "reason": "filename_parse_failed"}

    log.info(f"▶  开始处理：{company_name} {year}年  [{industry}]  {filename}")

    # ── Step 1: 提取 PDF 文本 ─────────────────────────────────────────────
    pages = extract_pdf_text(str(pdf_path))
    if not pages:
        log.error(f"PDF 文本提取为空：{filename}")
        return {"status": "failed", "reason": "empty_pdf"}

    context = build_context_window(pages)
    log.info(f"   PDF 提取完成，共 {len(pages)} 页，上下文 {len(context)} 字符")

    # ── Step 2: LLM 结构化提取 ──────────────────────────────────────────
    try:
        universal_raw, industry_raw = extract_metrics_with_llm(context, industry)
    except Exception as e:
        log.error(f"LLM 提取异常：{e}")
        return {"status": "failed", "reason": f"llm_error: {e}"}

    # ── Step 3: 写入通用表 ───────────────────────────────────────────────
    std_universal = upsert_universal(
        conn, company_name, year, industry, filename, universal_raw
    )
    log.info(f"   通用指标写入完成，"
             f"非空字段：{sum(1 for v in std_universal.values() if v is not None)}/14")

    # ── Step 4: 写入行业子表 ─────────────────────────────────────────────
    std_industry = {}
    if industry == "bank":
        upsert_banking(conn, company_name, year, filename, industry_raw)
        std_industry = {
            "green_finance_balance":     industry_raw.green_finance_balance,
            "inclusive_finance_balance": industry_raw.inclusive_finance_balance,
        }
    elif industry == "new_energy":
        upsert_auto(conn, company_name, year, filename, industry_raw)
        std_industry = {
            "scope_3_emissions":        industry_raw.scope_3_emissions,
            "rd_investment_total":      industry_raw.rd_investment_total,
            "supplier_esg_audit_ratio": industry_raw.supplier_esg_audit_ratio,
        }
    else:  # power
        upsert_power(conn, company_name, year, filename, industry_raw)
        std_industry = {
            "scope_3_emissions":  industry_raw.scope_3_emissions,
            "clean_energy_ratio": industry_raw.clean_energy_ratio,
            "rd_investment_total":industry_raw.rd_investment_total,
        }
    log.info(f"   行业专属指标写入完成")

    # ── Step 5: 缺失指标日志 ─────────────────────────────────────────────
    # 把 RawMetric 对象转为 std 值字典用于缺失检测
    industry_std_flat = {}
    for k, v in std_industry.items():
        if isinstance(v, RawMetric) or v is None:
            industry_std_flat[k] = None
        else:
            industry_std_flat[k] = v

    log_missing_metrics(
        conn, company_name, year, industry, filename,
        std_universal, industry_std_flat,
    )

    log.info(f"✅ 完成：{company_name} {year}年")
    return {
        "status":       "success",
        "company_name": company_name,
        "year":         year,
        "industry":     industry,
        "universal_filled": sum(1 for v in std_universal.values() if v is not None),
    }


def run_ingestion(base_dir: str = "."):
    """遍历三个行业文件夹，处理所有 PDF 并写入数据库。"""
    base = Path(base_dir)
    conn = init_db(DB_PATH)

    results  = []
    failures = []

    for folder, industry in INDUSTRY_FOLDERS.items():
        folder_path = base / folder
        if not folder_path.exists():
            log.warning(f"文件夹不存在，跳过：{folder_path}")
            continue

        pdfs = sorted(folder_path.glob("*.pdf"))
        log.info(f"\n{'='*60}")
        log.info(f"行业：{industry}  文件夹：{folder}  共 {len(pdfs)} 个 PDF")
        log.info(f"{'='*60}")

        for pdf_path in pdfs:
            result = process_single_pdf(pdf_path, industry, conn)
            if result["status"] == "success":
                results.append(result)
            elif result["status"] != "skipped":
                failures.append({**result, "file": str(pdf_path)})

    # ── 输出质量报告 ─────────────────────────────────────────────────────
    print_quality_report(conn, results, failures)
    conn.close()
    log.info(f"\n数据库已保存至：{DB_PATH}")


def print_quality_report(conn, results, failures):
    """打印数据摄入质量报告。"""
    print("\n" + "=" * 60)
    print("  ESG 数据摄入质量报告")
    print("=" * 60)

    print(f"\n✅ 成功：{len(results)} 份报告")
    if failures:
        print(f"❌ 失败：{len(failures)} 份报告")
        for f in failures:
            print(f"   {f.get('file', '?')} → {f.get('reason', '?')}")

    # 各公司指标覆盖率
    print("\n── 各公司指标覆盖情况 ──")
    try:
        df = pd.read_sql_query("""
            SELECT company_name, year, industry,
                   ROUND(
                     (CAST(
                       (CASE WHEN scope_1_emissions IS NOT NULL THEN 1 ELSE 0 END +
                        CASE WHEN scope_2_emissions IS NOT NULL THEN 1 ELSE 0 END +
                        CASE WHEN total_energy_consumption IS NOT NULL THEN 1 ELSE 0 END +
                        CASE WHEN energy_intensity IS NOT NULL THEN 1 ELSE 0 END +
                        CASE WHEN employee_training_hours IS NOT NULL THEN 1 ELSE 0 END +
                        CASE WHEN safety_accidents_count IS NOT NULL THEN 1 ELSE 0 END +
                        CASE WHEN customer_complaint_res IS NOT NULL THEN 1 ELSE 0 END +
                        CASE WHEN charitable_donations IS NOT NULL THEN 1 ELSE 0 END +
                        CASE WHEN independent_director_ratio IS NOT NULL THEN 1 ELSE 0 END +
                        CASE WHEN female_director_ratio IS NOT NULL THEN 1 ELSE 0 END +
                        CASE WHEN anti_corruption_coverage IS NOT NULL THEN 1 ELSE 0 END +
                        CASE WHEN regulatory_penalties IS NOT NULL THEN 1 ELSE 0 END +
                        CASE WHEN esg_committee_setup IS NOT NULL THEN 1 ELSE 0 END +
                        CASE WHEN external_esg_rating IS NOT NULL THEN 1 ELSE 0 END)
                     AS REAL) / 14 * 100), 1) AS coverage_pct
            FROM esg_universal_metrics
            ORDER BY industry, company_name, year
        """, conn)
        print(df.to_string(index=False))
    except Exception as e:
        print(f"覆盖率统计失败: {e}")

    # 缺失率最高的指标
    print("\n── 缺失次数最多的指标（TOP 10）──")
    try:
        df2 = pd.read_sql_query("""
            SELECT metric_key, industry,
                   COUNT(*) AS missing_count,
                   missing_reason
            FROM missing_data_log
            GROUP BY metric_key, industry
            ORDER BY missing_count DESC
            LIMIT 10
        """, conn)
        print(df2.to_string(index=False))
    except Exception as e:
        print(f"缺失统计失败: {e}")

    # 低置信度预警
    print("\n── 低置信度预警（需人工核查）──")
    try:
        rows = conn.execute("""
            SELECT company_name, year, confidence_scores, validation_warnings
            FROM esg_universal_metrics
            WHERE validation_warnings != '{}'
        """).fetchall()
        for row in rows[:10]:
            warns = json.loads(row[3] or "{}")
            if warns:
                print(f"  {row[0]} {row[1]}年：{warns}")
    except Exception as e:
        print(f"预警统计失败: {e}")

    print("\n" + "=" * 60)


# ══════════════════════════════════════════════════════════════════════════════
# 11.  入口
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ESG 报告结构化数据库构建")
    parser.add_argument(
        "--base-dir", default=".",
        help="包含 finance/car/electric 文件夹的根目录（默认当前目录）"
    )
    parser.add_argument(
        "--db", default=DB_PATH,
        help=f"SQLite 数据库路径（默认：{DB_PATH}）"
    )
    parser.add_argument(
        "--model", default="gemini-3-flash-preview",
        help="LLM 模型名（默认：gemini-3-flash-preview）"
    )
    args = parser.parse_args()

    DB_PATH    = args.db
    LLM_MODEL  = args.model

    run_ingestion(base_dir=args.base_dir)
