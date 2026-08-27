"""
agent/data_dictionary.py  —  数据字典 & Few-Shot SQL 样例
==========================================================

【什么是数据字典？】

数据字典就像图书馆的"藏书目录"——它不是数据本身，而是描述数据的"元数据"。
它告诉 LLM："数据库里有哪些表、每个字段叫什么名、是什么含义、单位是什么"。

没有数据字典，LLM 就像一个不懂中文的翻译被扔进中文图书馆——根本不知道该查什么。

【这个模块负责什么？】

  1. 完整描述 SQLite 数据库中 4 张表的字段语义、单位、计算公式、NULL 含义
  2. 提供 Few-Shot SQL 样例——给 LLM 看几个"好的 SQL 写法"作为参考
     （Few-Shot 是 prompt engineering 的核心技巧，极大提升 Text2SQL 准确率）
  3. 提供 Schema 组装函数（供 Schema Injector 调用）：
     根据本次查询涉及的指标和行业，动态裁剪出最相关的 schema 片段
  4. 提供已知缺失预查函数：
     在生成 SQL 前告知 LLM 哪些数据已知为空，避免生成注定返回空集的 SQL

设计原则：
  · 全部硬编码，零延迟，确定性强
  · 字段描述以"SQL Worker 能读懂并正确使用"为标准，
    不是给人类读者的文档
  · Few-Shot 样例覆盖：单公司单指标、多公司对比、跨年趋势、
    JOIN 子表、NULL 处理、归一化计算 共 6 种模式
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = "./data/esg_data.db"

# ══════════════════════════════════════════════════════════════════════════════
# 1.  字段级数据字典
# ══════════════════════════════════════════════════════════════════════════════

# 格式：
# TABLE_DICT[table_name] = {
#     "_meta": {description, join_hints},
#     field_name: {type, unit, std_unit, description, formula,
#                  null_meaning, applicable_industries}
# }

TABLE_DICT: dict[str, dict] = {

    # ──────────────────────────────────────────────────────────────────────────
    "esg_universal_metrics": {
        "_meta": {
            "description": (
                "所有行业通用 ESG 指标主表。"
                "每行代表一家公司某一年的通用指标快照。"
                "主键为 (company_name, year)。"
            ),
            "join_hints": [
                "银行专属指标：JOIN esg_banking_metrics ON (company_name, year)",
                "新能源专属指标：JOIN esg_auto_metrics ON (company_name, year)",
                "电力专属指标：JOIN esg_power_metrics ON (company_name, year)",
                "行业过滤：WHERE industry = 'bank'/'new_energy'/'power'",
            ],
        },
        "company_name": {
            "type": "TEXT",
            "description": "公司短名称，如'比亚迪'、'工商银行'、'华能国际'",
            "null_meaning": "不存在 NULL，主键字段",
        },
        "year": {
            "type": "INTEGER",
            "description": "报告年份，取值范围 2022~2024",
            "null_meaning": "不存在 NULL，主键字段",
        },
        "industry": {
            "type": "TEXT",
            "description": "行业代码。取值：'bank'（银行）/ 'new_energy'（新能源汽车）/ 'power'（电力）",
            "null_meaning": "不存在 NULL",
        },
        "scope_1_emissions": {
            "type": "REAL",
            "unit": "tCO2e（吨二氧化碳当量）",
            "description": "范围一：公司直接控制的排放源产生的温室气体排放量",
            "formula": "直接计量，无需计算",
            "null_meaning": "该公司该年度未在报告中披露此指标，不等于0，不得参与平均值/对比计算",
            "applicable_industries": ["new_energy", "power", "bank"],
        },
        "scope_2_emissions": {
            "type": "REAL",
            "unit": "tCO2e",
            "description": "范围二：外购电力、热力产生的间接温室气体排放量",
            "formula": "直接计量，通常由外购电量 × 电网排放因子计算",
            "null_meaning": "未披露，不等于0",
            "applicable_industries": ["new_energy", "power", "bank"],
        },
        "total_energy_consumption": {
            "type": "REAL",
            "unit": "GJ（吉焦）",
            "description": "运营综合能耗总量，已标准化为 GJ。原始数据可能为万吨标准煤/亿千瓦时，已换算",
            "formula": "各类能源消耗量 × 对应折算系数之和",
            "null_meaning": "未披露",
            "applicable_industries": ["new_energy", "power", "bank"],
        },
        "energy_intensity": {
            "type": "REAL",
            "unit": "GJ/亿元（营业收入）",
            "description": "单位营收综合能耗强度，数值越低代表能源效率越高",
            "formula": "total_energy_consumption(GJ) ÷ 营业收入(亿元)",
            "null_meaning": "未披露或分母（营收）数据缺失",
            "applicable_industries": ["new_energy", "power", "bank"],
        },
        "employee_training_hours": {
            "type": "REAL",
            "unit": "小时/人（年人均）",
            "description": "全体员工年度平均培训时长",
            "formula": "培训总学时 ÷ 员工总数",
            "null_meaning": "未披露",
            "applicable_industries": ["new_energy", "power", "bank"],
        },
        "safety_accidents_count": {
            "type": "REAL",
            "unit": "次（或人，若为工亡人数）",
            "description": "安全生产事故次数或工亡/重伤人数。0 表示明确披露零事故，NULL 表示未披露",
            "formula": "直接计量",
            "null_meaning": "未披露（注意：0 和 NULL 语义完全不同）",
            "applicable_industries": ["new_energy", "power", "bank"],
        },
        "customer_complaint_res": {
            "type": "REAL",
            "unit": "%（百分比）",
            "description": "客户投诉办结率或客户满意度得分（统一换算为百分比）",
            "formula": "已办结投诉数 ÷ 总投诉数 × 100",
            "null_meaning": "未披露",
            "applicable_industries": ["new_energy", "power", "bank"],
        },
        "charitable_donations": {
            "type": "REAL",
            "unit": "万元",
            "description": "年度公益慈善捐赠总额，已统一换算为万元",
            "formula": "直接计量",
            "null_meaning": "未披露（不等于未捐款）",
            "applicable_industries": ["new_energy", "power", "bank"],
        },
        "independent_director_ratio": {
            "type": "REAL",
            "unit": "%",
            "description": "独立董事占董事会总人数的比例",
            "formula": "独立董事人数 ÷ 董事会总人数 × 100",
            "null_meaning": "未披露",
            "applicable_industries": ["new_energy", "power", "bank"],
        },
        "female_director_ratio": {
            "type": "REAL",
            "unit": "%",
            "description": "女性董事或女性高管占对应群体总人数的比例",
            "formula": "女性人数 ÷ 总人数 × 100",
            "null_meaning": "未披露",
            "applicable_industries": ["new_energy", "power", "bank"],
        },
        "anti_corruption_coverage": {
            "type": "REAL",
            "unit": "%",
            "description": "参加反腐败/廉洁从业培训的员工占全体员工比例",
            "formula": "参训人数 ÷ 员工总数 × 100",
            "null_meaning": "未披露",
            "applicable_industries": ["new_energy", "power", "bank"],
        },
        "regulatory_penalties": {
            "type": "REAL",
            "unit": "次",
            "description": "年度受监管机构行政处罚次数。0 表示明确披露零处罚，NULL 表示未披露",
            "formula": "直接计量",
            "null_meaning": "未披露（0 和 NULL 语义不同，0 是好的信号）",
            "applicable_industries": ["new_energy", "power", "bank"],
        },
        "esg_committee_setup": {
            "type": "REAL",
            "unit": "布尔值（1=已设立，0=未设立）",
            "description": "是否设立了 ESG、可持续发展或相关专项委员会",
            "formula": "无，原始文本判断后转布尔",
            "null_meaning": "报告未提及",
            "applicable_industries": ["new_energy", "power", "bank"],
        },
        "external_esg_rating": {
            "type": "TEXT",
            "unit": "文本，如'MSCI: AA'、'BBB'",
            "description": "第三方机构 ESG 评级结果，格式为'机构名: 等级'",
            "formula": "直接文本",
            "null_meaning": "无第三方评级或未披露",
            "applicable_industries": ["new_energy", "power", "bank"],
        },
        "data_quality": {
            "type": "TEXT（JSON）",
            "description": (
                "各指标的数据质量标签，JSON 格式。"
                "取值：'normal'正常/'estimated'约数/'unit_converted'已换算单位/"
                "'missing'缺失/'parse_failed'解析失败。"
                "查询时可用 json_extract(data_quality, '$.scope_1_emissions') 提取"
            ),
            "null_meaning": "不应为 NULL",
        },
        "confidence_scores": {
            "type": "TEXT（JSON）",
            "description": "各指标置信度分数（0~1），JSON 格式。0.9 以上可直接使用，0.5 以下需人工核查",
            "null_meaning": "不应为 NULL",
        },
        "source_file": {
            "type": "TEXT",
            "description": "数据来源 PDF 文件名，用于溯源",
            "null_meaning": "数据来源未记录",
        },
    },

    # ──────────────────────────────────────────────────────────────────────────
    "esg_banking_metrics": {
        "_meta": {
            "description": (
                "银行行业专属指标子表。"
                "必须与 esg_universal_metrics JOIN 使用，"
                "单独查询无行业和公司信息。"
            ),
            "join_hints": [
                "JOIN esg_universal_metrics u ON b.company_name=u.company_name AND b.year=u.year",
                "WHERE u.industry = 'bank'",
            ],
        },
        "green_finance_balance": {
            "type": "REAL",
            "unit": "亿元",
            "description": "绿色贷款/绿色信贷余额，已统一换算为亿元。是银行 ESG 评级最核心指标",
            "formula": "直接计量（期末余额）",
            "null_meaning": "未披露，不等于零",
        },
        "inclusive_finance_balance": {
            "type": "REAL",
            "unit": "亿元",
            "description": "普惠型小微企业及涉农贷款余额，已换算为亿元",
            "formula": "直接计量",
            "null_meaning": "未披露",
        },
    },

    # ──────────────────────────────────────────────────────────────────────────
    "esg_auto_metrics": {
        "_meta": {
            "description": (
                "新能源 / 汽车制造行业专属指标子表。"
                "必须与 esg_universal_metrics JOIN 使用。"
            ),
            "join_hints": [
                "JOIN esg_universal_metrics u ON a.company_name=u.company_name AND a.year=u.year",
                "WHERE u.industry = 'new_energy'",
            ],
        },
        "scope_3_emissions": {
            "type": "REAL",
            "unit": "tCO2e",
            "description": "范围三：价值链上下游排放（含供应商生产和消费者使用阶段）",
            "formula": "各类别范围三排放量加总",
            "null_meaning": "未披露（范围三披露率在行业内普遍偏低，属行业性缺失）",
        },
        "rd_investment_total": {
            "type": "REAL",
            "unit": "亿元",
            "description": "年度研发投入总额，已换算为亿元",
            "formula": "直接计量（当期费用化+资本化研发支出之和）",
            "null_meaning": "未披露",
        },
        "supplier_esg_audit_ratio": {
            "type": "REAL",
            "unit": "%",
            "description": "核心/战略供应商中完成 ESG 审核或评估的比例",
            "formula": "完成 ESG 审核的供应商数 ÷ 核心供应商总数 × 100",
            "null_meaning": "未披露（新兴披露指标，缺失率较高）",
        },
    },

    # ──────────────────────────────────────────────────────────────────────────
    "esg_power_metrics": {
        "_meta": {
            "description": (
                "电力行业专属指标子表。"
                "必须与 esg_universal_metrics JOIN 使用。"
            ),
            "join_hints": [
                "JOIN esg_universal_metrics u ON p.company_name=u.company_name AND p.year=u.year",
                "WHERE u.industry = 'power'",
            ],
        },
        "scope_3_emissions": {
            "type": "REAL",
            "unit": "tCO2e",
            "description": "范围三：燃料上游排放、输配电损耗排放等价值链间接排放",
            "formula": "各类别范围三排放量加总",
            "null_meaning": "未披露",
        },
        "clean_energy_ratio": {
            "type": "REAL",
            "unit": "%",
            "description": "清洁能源（风光水核）装机容量或发电量占总量的比例",
            "formula": "清洁能源装机容量(GW) ÷ 总装机容量(GW) × 100，或发电量口径",
            "null_meaning": "未披露（注意：口径可能是装机容量或发电量，对比时需校验一致性）",
        },
        "rd_investment_total": {
            "type": "REAL",
            "unit": "亿元",
            "description": "清洁技术研发投入总额，已换算为亿元",
            "formula": "直接计量",
            "null_meaning": "未披露",
        },
    },

    # ──────────────────────────────────────────────────────────────────────────
    "missing_data_log": {
        "_meta": {
            "description": (
                "数据缺失记录表。记录了在 PDF 解析时明确确认为'未披露'的指标。"
                "Schema Injector 在生成 SQL 前必须查此表，"
                "避免生成注定返回空集的 SQL。"
            ),
            "join_hints": [],
        },
        "company_name": {"type": "TEXT", "description": "公司名称"},
        "year":         {"type": "INTEGER", "description": "年份"},
        "metric_key":   {"type": "TEXT", "description": "指标字段名，与主表字段名一致"},
        "missing_reason": {
            "type": "TEXT",
            "description": (
                "缺失原因。"
                "'not_disclosed'=报告未披露 / "
                "'parse_failed'=解析失败 / "
                "'not_applicable'=不适用"
            ),
        },
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# 2.  Few-Shot SQL 样例库
# ══════════════════════════════════════════════════════════════════════════════

FEW_SHOT_EXAMPLES: list[dict] = [

    # ── 模式1：单公司单指标多年趋势 ──────────────────────────────────────────
    {
        "pattern": "single_company_trend",
        "question": "查询比亚迪2022到2024年的范围一碳排放趋势",
        "sql": """
SELECT
    company_name,
    year,
    scope_1_emissions,
    json_extract(data_quality, '$.scope_1_emissions')    AS quality,
    json_extract(confidence_scores, '$.scope_1_emissions') AS confidence
FROM esg_universal_metrics
WHERE company_name = '比亚迪'
  AND year BETWEEN 2022 AND 2024
  AND scope_1_emissions IS NOT NULL
ORDER BY year ASC;
""",
        "notes": [
            "IS NOT NULL 过滤至关重要，NULL 代表未披露不代表 0",
            "同时查出 data_quality 和 confidence，让 Synthesizer 知道数据可信度",
            "ORDER BY year ASC 保证时间序列正确",
        ],
    },

    # ── 模式2：多公司横向对比（通用表） ──────────────────────────────────────
    {
        "pattern": "multi_company_compare",
        "question": "对比新能源行业所有公司2023年的碳排放范围一和范围二",
        "sql": """
SELECT
    company_name,
    year,
    scope_1_emissions,
    scope_2_emissions,
    (COALESCE(scope_1_emissions, 0) + COALESCE(scope_2_emissions, 0))
        AS scope_1_2_total,
    json_extract(data_quality, '$.scope_1_emissions') AS s1_quality,
    json_extract(data_quality, '$.scope_2_emissions') AS s2_quality
FROM esg_universal_metrics
WHERE industry = 'new_energy'
  AND year = 2023
ORDER BY scope_1_emissions DESC NULLS LAST;
""",
        "notes": [
            "NULLS LAST 让未披露公司排在已披露公司后面，不干扰排名",
            "COALESCE 只用于计算合并列，单列对比仍用原始值（含NULL）",
            "查出 data_quality 便于 Synthesizer 标注数据质量",
        ],
    },

    # ── 模式3：JOIN 子表（银行专属） ─────────────────────────────────────────
    {
        "pattern": "join_banking_subtable",
        "question": "查询银行行业2022-2024年绿色贷款余额",
        "sql": """
SELECT
    u.company_name,
    u.year,
    b.green_finance_balance,
    b.inclusive_finance_balance,
    json_extract(b.data_quality, '$.green_finance_balance') AS gf_quality,
    json_extract(b.confidence_scores, '$.green_finance_balance') AS gf_confidence
FROM esg_universal_metrics u
JOIN esg_banking_metrics b
  ON u.company_name = b.company_name
  AND u.year = b.year
WHERE u.industry = 'bank'
  AND u.year BETWEEN 2022 AND 2024
ORDER BY u.company_name, u.year;
""",
        "notes": [
            "银行专属指标必须 JOIN esg_banking_metrics，主表里没有",
            "JOIN 条件同时包含 company_name 和 year，确保精确匹配",
            "WHERE 加 industry='bank' 过滤，防止错误匹配",
        ],
    },

    # ── 模式4：JOIN 子表（新能源专属） ───────────────────────────────────────
    {
        "pattern": "join_auto_subtable",
        "question": "查询新能源汽车行业所有公司2023年的研发投入和供应商ESG审核覆盖率",
        "sql": """
SELECT
    u.company_name,
    u.year,
    a.rd_investment_total,
    a.supplier_esg_audit_ratio,
    a.scope_3_emissions,
    json_extract(a.data_quality, '$.rd_investment_total') AS rd_quality
FROM esg_universal_metrics u
JOIN esg_auto_metrics a
  ON u.company_name = a.company_name
  AND u.year = a.year
WHERE u.industry = 'new_energy'
  AND u.year = 2023
ORDER BY a.rd_investment_total DESC NULLS LAST;
""",
        "notes": [
            "新能源专属字段在 esg_auto_metrics，不在主表",
            "scope_3_emissions 也在子表里",
        ],
    },

    # ── 模式5：纵横都有（矩阵查询）+ 缺失感知 ───────────────────────────────
    {
        "pattern": "matrix_with_null_awareness",
        "question": "查询三家电力公司2022-2024年清洁能源占比和碳排放，含缺失情况",
        "sql": """
SELECT
    u.company_name,
    u.year,
    u.scope_1_emissions,
    p.clean_energy_ratio,
    p.rd_investment_total,
    CASE
        WHEN u.scope_1_emissions IS NULL THEN '未披露'
        WHEN json_extract(u.data_quality,'$.scope_1_emissions') = 'estimated' THEN '约数'
        ELSE '正常'
    END AS scope1_status,
    CASE
        WHEN p.clean_energy_ratio IS NULL THEN '未披露'
        ELSE CAST(ROUND(p.clean_energy_ratio, 1) AS TEXT) || '%'
    END AS clean_ratio_display
FROM esg_universal_metrics u
LEFT JOIN esg_power_metrics p
  ON u.company_name = p.company_name
  AND u.year = p.year
WHERE u.industry = 'power'
  AND u.year BETWEEN 2022 AND 2024
ORDER BY u.company_name, u.year;
""",
        "notes": [
            "用 LEFT JOIN 而不是 JOIN，确保即使子表缺数据也能返回主表行",
            "CASE WHEN IS NULL 生成 display 列，让未披露状态可见",
            "避免 WHERE clean_energy_ratio IS NOT NULL，那样会把整行过滤掉",
        ],
    },

    # ── 模式6：归一化计算（碳排放强度）──────────────────────────────────────
    {
        "pattern": "normalization_calculation",
        "question": "计算新能源行业各公司2023年碳排放强度（需要和营收结合）",
        "sql": """
-- 注意：营业收入不在 ESG 表中，碳排放强度用 energy_intensity 字段近似
-- 如需精确强度，须联合财务数据，当前 ESG 库中使用 energy_intensity 字段
SELECT
    company_name,
    year,
    scope_1_emissions,
    scope_2_emissions,
    (COALESCE(scope_1_emissions, 0) + COALESCE(scope_2_emissions, 0))
        AS total_scope_1_2,
    energy_intensity,
    json_extract(data_quality, '$.energy_intensity') AS intensity_quality
FROM esg_universal_metrics
WHERE industry = 'new_energy'
  AND year = 2023
  AND (scope_1_emissions IS NOT NULL OR scope_2_emissions IS NOT NULL)
ORDER BY total_scope_1_2 ASC NULLS LAST;
""",
        "notes": [
            "营收数据不在 ESG 库，用 energy_intensity 字段代替精确强度计算",
            "对比碳排放绝对值时，Synthesizer 必须做规模归一化，SQL 只负责取数",
            "OR 条件确保只要有一个范围的数据就返回",
        ],
    },

    # ── 模式7：治理指标查询 ───────────────────────────────────────────────────
    {
        "pattern": "governance_metrics",
        "question": "查询所有行业公司2023年的治理指标（独立董事占比、女性董事、ESG委员会）",
        "sql": """
SELECT
    company_name,
    industry,
    year,
    independent_director_ratio,
    female_director_ratio,
    esg_committee_setup,
    CASE WHEN esg_committee_setup = 1 THEN '已设立'
         WHEN esg_committee_setup = 0 THEN '未设立'
         ELSE '未披露'
    END AS committee_status,
    external_esg_rating
FROM esg_universal_metrics
WHERE year = 2023
ORDER BY industry, independent_director_ratio DESC NULLS LAST;
""",
        "notes": [
            "esg_committee_setup 是布尔型，用 CASE WHEN 转换为可读文本",
            "external_esg_rating 是文本字段，不能做数值排序",
        ],
    },

    # ── 模式8：查询已知缺失（Schema Injector 专用）──────────────────────────
    {
        "pattern": "check_known_missing",
        "question": "查询某批公司某些指标的已知缺失情况",
        "sql": """
SELECT
    company_name,
    year,
    metric_key,
    missing_reason
FROM missing_data_log
WHERE company_name IN ('比亚迪', '宁德时代', '广汽集团')
  AND year IN (2022, 2023, 2024)
  AND metric_key IN ('scope_1_emissions', 'scope_3_emissions', 'rd_investment_total')
ORDER BY company_name, year, metric_key;
""",
        "notes": [
            "Schema Injector 在生成业务 SQL 之前先查此表",
            "把缺失信息注入 prompt，让 LLM 知道哪些字段预计为 NULL",
        ],
    },
]


# ══════════════════════════════════════════════════════════════════════════════
# 3.  口径注意事项（Evaluator-D 口径校验使用）
# ══════════════════════════════════════════════════════════════════════════════

SCOPE_CAVEATS: dict[str, list[str]] = {
    "scope_1_emissions": [
        "部分公司仅披露运营边界（自有设施），部分包含合并报表范围",
        "部分公司使用 GWP 100年值（AR5），部分使用 AR4，导致数值不可比",
        "火电公司的 Scope 1 量级（亿吨级）远大于制造业（万吨级），跨行业对比无意义",
    ],
    "scope_3_emissions": [
        "披露率极低（<30%），且各公司涵盖的类别数量不同（GHG Protocol 共15类）",
        "新能源车企若包含'汽车使用阶段排放'，数字会比只含供应链的公司大10倍以上",
        "未对齐类别直接对比会完全误导结论",
    ],
    "clean_energy_ratio": [
        "部分公司口径为装机容量占比，部分为发电量占比，两者差异可达10-20个百分点",
        "水电丰枯年份影响发电量口径数据，同一公司不同年份可比性受限",
    ],
    "green_finance_balance": [
        "各银行采用的绿色金融认定标准不完全一致（中国绿金委标准 vs 内部标准）",
        "部分银行含绿色债券，部分仅含贷款，口径差异最大可达50%",
    ],
    "rd_investment_total": [
        "部分公司为'费用化研发支出'，部分包含'资本化研发支出'，合计口径约大20-40%",
        "研发投入强度（研发/营收）比绝对值更有对比意义",
    ],
    "energy_intensity": [
        "分母口径：部分为营业收入，部分为工业产值，跨公司对比需确认一致",
    ],
    "regulatory_penalties": [
        "部分公司披露次数，部分披露金额，两者不可比",
        "0 次处罚是明确的好信号，但需区分'0次'和'未披露'",
    ],
}


# ══════════════════════════════════════════════════════════════════════════════
# 4.  Schema Injector 组装函数
# ══════════════════════════════════════════════════════════════════════════════

def get_relevant_schema(
    metric_keys: list[str],
    industry: str,
    companies: list[str],
    years: list[int],
    db_path: str = DB_PATH,
) -> str:
    """
    Schema Injector 节点调用此函数，
    动态组装注入给 SQL Worker 的 prompt 片段。

    返回完整的 schema 上下文字符串，包含：
      1. 相关表结构和字段说明
      2. 口径注意事项
      3. 已知缺失预查结果
      4. 匹配的 Few-Shot 样例
      5. 严格约束规则
    """
    parts: list[str] = []

    # ── Part 1：相关表和字段说明 ──────────────────────────────────────────────
    parts.append("=== 数据库表结构与字段语义 ===\n")

    # 确定需要哪些表
    tables_needed = ["esg_universal_metrics"]
    if industry == "bank":
        tables_needed.append("esg_banking_metrics")
    elif industry == "new_energy":
        tables_needed.append("esg_auto_metrics")
    elif industry == "power":
        tables_needed.append("esg_power_metrics")

    for table_name in tables_needed:
        table_info = TABLE_DICT.get(table_name, {})
        meta = table_info.get("_meta", {})
        parts.append(f"【表名】{table_name}")
        parts.append(f"【说明】{meta.get('description', '')}")

        join_hints = meta.get("join_hints", [])
        if join_hints:
            parts.append("【关联方式】")
            for hint in join_hints:
                parts.append(f"  · {hint}")

        parts.append("【关键字段】")
        for field_name, field_info in table_info.items():
            if field_name.startswith("_"):
                continue
            # 只显示与本次查询相关的字段（+ 必要的元字段）
            always_show = {
                "company_name", "year", "industry",
                "data_quality", "confidence_scores", "source_file",
            }
            if field_name not in always_show and field_name not in metric_keys:
                continue
            unit = field_info.get("unit", "")
            desc = field_info.get("description", "")
            null = field_info.get("null_meaning", "")
            formula = field_info.get("formula", "")
            parts.append(f"  {field_name}  [{unit}]")
            parts.append(f"    含义：{desc}")
            if formula and formula != "直接计量" and formula != "无，原始文本判断后转布尔":
                parts.append(f"    公式：{formula}")
            parts.append(f"    NULL含义：{null}")
        parts.append("")

    # ── Part 2：口径注意事项 ──────────────────────────────────────────────────
    relevant_caveats = {
        k: v for k, v in SCOPE_CAVEATS.items() if k in metric_keys
    }
    if relevant_caveats:
        parts.append("=== ⚠️  口径注意事项（直接影响对比有效性）===\n")
        for metric, caveats in relevant_caveats.items():
            parts.append(f"【{metric}】")
            for c in caveats:
                parts.append(f"  · {c}")
        parts.append("")

    # ── Part 3：已知缺失预查 ──────────────────────────────────────────────────
    missing_preview = query_known_missing(
        companies=companies,
        years=years,
        metric_keys=metric_keys,
        db_path=db_path,
    )
    if missing_preview:
        parts.append("=== 已知数据缺失（以下字段在数据库中确认为未披露）===\n")
        for row in missing_preview:
            parts.append(
                f"  · {row['company_name']} {row['year']}年 "
                f"{row['metric_key']}：{row['missing_reason']}"
            )
        parts.append("")
        parts.append(
            "⚠️  生成 SQL 时请注意：上述字段已确认缺失，"
            "即使查询也会返回 NULL，请在 SQL 中用 IS NOT NULL 过滤或用 NULLS LAST 排序，"
            "不要因为空结果触发错误。\n"
        )

    # ── Part 4：匹配 Few-Shot 样例 ────────────────────────────────────────────
    matched_examples = _match_few_shots(metric_keys, industry)
    if matched_examples:
        parts.append("=== SQL 参考样例 ===\n")
        for ex in matched_examples[:3]:  # 最多3个样例，避免 prompt 过长
            parts.append(f"【{ex['question']}】")
            parts.append(ex["sql"].strip())
            for note in ex.get("notes", []):
                parts.append(f"-- 注：{note}")
            parts.append("")

    # ── Part 5：严格约束规则 ──────────────────────────────────────────────────
    parts.append("=== 生成 SQL 的严格约束 ===\n")
    parts.extend([
        "1. NULL 代表未披露，严禁用 0 替代，严禁把 NULL 行纳入平均值计算",
        "2. 跨表查询时必须使用 (company_name, year) 双字段 JOIN",
        "3. 行业专属指标必须 JOIN 对应子表，不要猜测字段在哪张表",
        "4. 排序时对可能为 NULL 的字段使用 ORDER BY xxx DESC NULLS LAST",
        "5. 只生成 SELECT 语句，严禁 INSERT / UPDATE / DELETE / DROP",
        "6. 银行专属：green_finance_balance、inclusive_finance_balance 在 esg_banking_metrics",
        "7. 新能源专属：scope_3_emissions、rd_investment_total、supplier_esg_audit_ratio 在 esg_auto_metrics",
        "8. 电力专属：scope_3_emissions、clean_energy_ratio、rd_investment_total 在 esg_power_metrics",
        "9. 同时查出 data_quality 字段，让下游节点知道数据可信度",
    ])

    return "\n".join(parts)


def query_known_missing(
    companies: list[str],
    years: list[int],
    metric_keys: list[str],
    db_path: str = DB_PATH,
) -> list[dict]:
    """
    从 missing_data_log 预查已知缺失。
    Schema Injector 在生成 SQL 前调用，把结果注入 prompt。
    """
    if not Path(db_path).exists():
        return []

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        placeholders_c = ",".join("?" * len(companies)) if companies else "''"
        placeholders_y = ",".join("?" * len(years))     if years     else "0"
        placeholders_m = ",".join("?" * len(metric_keys)) if metric_keys else "''"

        query = f"""
            SELECT company_name, year, metric_key, missing_reason
            FROM missing_data_log
            WHERE company_name IN ({placeholders_c})
              AND year IN ({placeholders_y})
              AND metric_key IN ({placeholders_m})
            ORDER BY company_name, year, metric_key
        """
        params = (
            list(companies or [])
            + [int(y) for y in (years or [])]
            + list(metric_keys or [])
        )
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"查询已知缺失失败: {e}")
        return []


def _match_few_shots(
    metric_keys: list[str],
    industry: str,
) -> list[dict]:
    """
    根据本次查询的指标和行业，匹配最相关的 Few-Shot 样例。
    优先级：行业匹配 > 指标匹配 > 通用样例。
    """
    scored: list[tuple[int, dict]] = []

    # 行业到样例模式的映射
    industry_patterns = {
        "bank":       {"join_banking_subtable"},
        "new_energy": {"join_auto_subtable"},
        "power":      {"join_power_subtable", "matrix_with_null_awareness"},
    }
    preferred_patterns = industry_patterns.get(industry, set())

    for ex in FEW_SHOT_EXAMPLES:
        score = 0
        # 行业匹配加2分
        if ex["pattern"] in preferred_patterns:
            score += 2
        # 样例 SQL 里包含查询指标加1分
        for mk in metric_keys:
            if mk in ex["sql"]:
                score += 1
        # 通用样例（非子表）给基础分
        if ex["pattern"] in {"single_company_trend", "governance_metrics",
                              "multi_company_compare"}:
            score += 1
        scored.append((score, ex))

    scored.sort(key=lambda x: -x[0])
    return [ex for _, ex in scored]


# ══════════════════════════════════════════════════════════════════════════════
# 5.  指标显示名映射（供 Synthesizer 生成报告时使用）
# ══════════════════════════════════════════════════════════════════════════════

METRIC_DISPLAY_NAMES: dict[str, dict] = {
    "scope_1_emissions":          {"cn": "范围一碳排放",      "unit": "tCO2e"},
    "scope_2_emissions":          {"cn": "范围二碳排放",      "unit": "tCO2e"},
    "scope_3_emissions":          {"cn": "范围三碳排放",      "unit": "tCO2e"},
    "total_energy_consumption":   {"cn": "综合能耗",          "unit": "万GJ"},
    "energy_intensity":           {"cn": "能耗强度",          "unit": "GJ/亿元"},
    "green_finance_balance":      {"cn": "绿色贷款余额",      "unit": "亿元"},
    "inclusive_finance_balance":  {"cn": "普惠金融贷款余额",  "unit": "亿元"},
    "rd_investment_total":        {"cn": "研发投入",          "unit": "亿元"},
    "supplier_esg_audit_ratio":   {"cn": "供应商ESG审核覆盖率","unit": "%"},
    "employee_training_hours":    {"cn": "人均培训时长",      "unit": "小时/人"},
    "safety_accidents_count":     {"cn": "安全事故次数",      "unit": "次"},
    "customer_complaint_res":     {"cn": "客户投诉办结率",    "unit": "%"},
    "charitable_donations":       {"cn": "公益慈善捐赠",      "unit": "万元"},
    "independent_director_ratio": {"cn": "独立董事占比",      "unit": "%"},
    "female_director_ratio":      {"cn": "女性董事占比",      "unit": "%"},
    "anti_corruption_coverage":   {"cn": "反腐培训覆盖率",    "unit": "%"},
    "regulatory_penalties":       {"cn": "监管处罚次数",      "unit": "次"},
    "esg_committee_setup":        {"cn": "ESG委员会设立",     "unit": "是/否"},
    "external_esg_rating":        {"cn": "第三方ESG评级",     "unit": "等级"},
    "clean_energy_ratio":         {"cn": "清洁能源占比",      "unit": "%"},
}


def get_metric_display(metric_key: str) -> tuple[str, str]:
    """返回 (中文名, 标准单位)。"""
    info = METRIC_DISPLAY_NAMES.get(metric_key, {})
    return info.get("cn", metric_key), info.get("unit", "")
