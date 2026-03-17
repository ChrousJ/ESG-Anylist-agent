#!/usr/bin/env python3
"""
scripts/generate_eval_dataset.py
=================================

生成离线评测数据集 eval_dataset.jsonl（约 40 条），
覆盖 4 种典型场景，全部基于已知数据库 schema 硬编码，
零 LLM 调用、零外部依赖。

用法：
    python scripts/generate_eval_dataset.py          # 默认输出到 eval_dataset.jsonl
    python scripts/generate_eval_dataset.py -o xxx.jsonl  # 指定输出路径
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


# ══════════════════════════════════════════════════════════════════════════════
# 评测用例定义
# ══════════════════════════════════════════════════════════════════════════════

def _build_cases() -> list[dict[str, Any]]:
    """返回全部评测用例列表。"""
    cases: list[dict[str, Any]] = []
    idx = 0

    def _add(
        category: str,
        query: str,
        expected_class: str,
        expected_entities: dict[str, Any],
        description: str,
    ) -> None:
        nonlocal idx
        idx += 1
        cases.append({
            "id": f"EVAL-{idx:03d}",
            "category": category,
            "query": query,
            "expected_class": expected_class,
            "expected_entities": expected_entities,
            "description": description,
        })

    # ── 1. 趋势分析（Trend Analysis）──────────────────────────────────────────
    _add(
        category="trend",
        query="比亚迪2022到2024年碳排放趋势如何？",
        expected_class="complex",
        expected_entities={
            "companies": ["比亚迪"],
            "years": [2022, 2023, 2024],
            "metrics": ["scope_1_emissions"],
            "intent": "trend",
            "industry": "new_energy",
        },
        description="单公司多年碳排放趋势，基准场景",
    )
    _add(
        category="trend",
        query="工商银行近三年绿色贷款余额变化趋势",
        expected_class="complex",
        expected_entities={
            "companies": ["工商银行"],
            "years": [2022, 2023, 2024],
            "metrics": ["green_finance_balance"],
            "intent": "trend",
            "industry": "bank",
        },
        description="银行行业子表指标趋势",
    )
    _add(
        category="trend",
        query="华能国际2022-2024清洁能源占比有什么变化？",
        expected_class="complex",
        expected_entities={
            "companies": ["华能国际"],
            "years": [2022, 2023, 2024],
            "metrics": ["clean_energy_ratio"],
            "intent": "trend",
            "industry": "power",
        },
        description="电力行业子表指标趋势",
    )
    _add(
        category="trend",
        query="宁德时代2023和2024年研发投入对比",
        expected_class="complex",
        expected_entities={
            "companies": ["宁德时代"],
            "years": [2023, 2024],
            "metrics": ["rd_investment_total"],
            "intent": "trend",
            "industry": "new_energy",
        },
        description="双年份纵向对比，涉及子表",
    )
    _add(
        category="trend",
        query="招商银行2022至2024年普惠金融贷款余额走势",
        expected_class="complex",
        expected_entities={
            "companies": ["招商银行"],
            "years": [2022, 2023, 2024],
            "metrics": ["inclusive_finance_balance"],
            "intent": "trend",
            "industry": "bank",
        },
        description="银行普惠金融趋势",
    )
    _add(
        category="trend",
        query="广汽集团2022到2024年能耗强度变化",
        expected_class="complex",
        expected_entities={
            "companies": ["广汽集团"],
            "years": [2022, 2023, 2024],
            "metrics": ["energy_intensity"],
            "intent": "trend",
            "industry": "new_energy",
        },
        description="通用表指标趋势",
    )
    _add(
        category="trend",
        query="中国大唐近三年范围一碳排放和范围二碳排放趋势",
        expected_class="complex",
        expected_entities={
            "companies": ["中国大唐"],
            "years": [2022, 2023, 2024],
            "metrics": ["scope_1_emissions", "scope_2_emissions"],
            "intent": "trend",
            "industry": "power",
        },
        description="多指标趋势分析",
    )
    _add(
        category="trend",
        query="建设银行2022到2024年独立董事占比和女性董事占比趋势",
        expected_class="complex",
        expected_entities={
            "companies": ["建设银行"],
            "years": [2022, 2023, 2024],
            "metrics": ["independent_director_ratio", "female_director_ratio"],
            "intent": "trend",
            "industry": "bank",
        },
        description="治理指标趋势分析",
    )
    _add(
        category="trend",
        query="长城汽车2023年与2024年安全事故数量对比",
        expected_class="complex",
        expected_entities={
            "companies": ["长城汽车"],
            "years": [2023, 2024],
            "metrics": ["safety_accidents_count"],
            "intent": "trend",
            "industry": "new_energy",
        },
        description="安全事故双年对比",
    )
    _add(
        category="trend",
        query="国家电投2022-2024年综合能耗总量趋势",
        expected_class="complex",
        expected_entities={
            "companies": ["国家电投"],
            "years": [2022, 2023, 2024],
            "metrics": ["total_energy_consumption"],
            "intent": "trend",
            "industry": "power",
        },
        description="电力公司能耗趋势",
    )

    # ── 2. 对比分析（Compare）─────────────────────────────────────────────────
    _add(
        category="compare",
        query="对比新能源行业所有公司2023年碳排放范围一",
        expected_class="complex",
        expected_entities={
            "companies": [],
            "years": [2023],
            "metrics": ["scope_1_emissions"],
            "intent": "compare",
            "industry": "new_energy",
        },
        description="行业级横向对比",
    )
    _add(
        category="compare",
        query="比亚迪和宁德时代2023年研发投入谁多？",
        expected_class="complex",
        expected_entities={
            "companies": ["比亚迪", "宁德时代"],
            "years": [2023],
            "metrics": ["rd_investment_total"],
            "intent": "compare",
            "industry": "new_energy",
        },
        description="双公司指标对比",
    )
    _add(
        category="compare",
        query="银行行业2023年绿色贷款余额排名",
        expected_class="complex",
        expected_entities={
            "companies": [],
            "years": [2023],
            "metrics": ["green_finance_balance"],
            "intent": "ranking",
            "industry": "bank",
        },
        description="行业排名查询",
    )
    _add(
        category="compare",
        query="电力行业2024年清洁能源占比最高的公司是哪家？",
        expected_class="complex",
        expected_entities={
            "companies": [],
            "years": [2024],
            "metrics": ["clean_energy_ratio"],
            "intent": "ranking",
            "industry": "power",
        },
        description="电力行业排名",
    )
    _add(
        category="compare",
        query="工商银行、建设银行、农业银行2023年绿色贷款横向对比",
        expected_class="complex",
        expected_entities={
            "companies": ["工商银行", "建设银行", "农业银行"],
            "years": [2023],
            "metrics": ["green_finance_balance"],
            "intent": "compare",
            "industry": "bank",
        },
        description="三家银行对比",
    )
    _add(
        category="compare",
        query="2023年所有行业公司的ESG委员会设立情况对比",
        expected_class="complex",
        expected_entities={
            "companies": [],
            "years": [2023],
            "metrics": ["esg_committee_setup"],
            "intent": "compare",
            "industry": "",
        },
        description="全行业治理指标对比",
    )
    _add(
        category="compare",
        query="比亚迪、广汽集团、吉利汽车2023年供应商ESG审核覆盖率对比",
        expected_class="complex",
        expected_entities={
            "companies": ["比亚迪", "广汽集团", "吉利汽车"],
            "years": [2023],
            "metrics": ["supplier_esg_audit_ratio"],
            "intent": "compare",
            "industry": "new_energy",
        },
        description="供应链治理对比",
    )
    _add(
        category="compare",
        query="华能国际和中国三峡2022-2024碳排放对比",
        expected_class="complex",
        expected_entities={
            "companies": ["华能国际", "中国三峡"],
            "years": [2022, 2023, 2024],
            "metrics": ["scope_1_emissions"],
            "intent": "compare",
            "industry": "power",
        },
        description="双公司多年对比（vertical+horizontal）",
    )
    _add(
        category="compare",
        query="2023年新能源行业员工培训时长和公益捐赠对比",
        expected_class="complex",
        expected_entities={
            "companies": [],
            "years": [2023],
            "metrics": ["employee_training_hours", "charitable_donations"],
            "intent": "compare",
            "industry": "new_energy",
        },
        description="多社会指标横向对比",
    )
    _add(
        category="compare",
        query="农业银行和中国银行2024年反腐培训覆盖率对比",
        expected_class="complex",
        expected_entities={
            "companies": ["农业银行", "中国银行"],
            "years": [2024],
            "metrics": ["anti_corruption_coverage"],
            "intent": "compare",
            "industry": "bank",
        },
        description="治理指标对比",
    )

    # ── 3. 缺失/降级（Missing & Degradation）──────────────────────────────────
    _add(
        category="missing_degradation",
        query="比亚迪2022年范围三碳排放是多少？",
        expected_class="complex",
        expected_entities={
            "companies": ["比亚迪"],
            "years": [2022],
            "metrics": ["scope_3_emissions"],
            "intent": "qa",
            "industry": "new_energy",
        },
        description="scope_3 披露率极低，很可能触发缺失降级",
    )
    _add(
        category="missing_degradation",
        query="所有电力公司2022年范围三碳排放对比",
        expected_class="complex",
        expected_entities={
            "companies": [],
            "years": [2022],
            "metrics": ["scope_3_emissions"],
            "intent": "compare",
            "industry": "power",
        },
        description="全行业查 scope_3，大面积缺失场景",
    )
    _add(
        category="missing_degradation",
        query="比亚迪和工商银行2023年碳排放对比",
        expected_class="complex",
        expected_entities={
            "companies": ["比亚迪", "工商银行"],
            "years": [2023],
            "metrics": ["scope_1_emissions"],
            "intent": "compare",
            "industry": "mixed",
        },
        description="跨行业对比，量级差异巨大，应有口径警告",
    )
    _add(
        category="missing_degradation",
        query="新能源行业2022年供应商ESG审核覆盖率排名",
        expected_class="complex",
        expected_entities={
            "companies": [],
            "years": [2022],
            "metrics": ["supplier_esg_audit_ratio"],
            "intent": "ranking",
            "industry": "new_energy",
        },
        description="新兴指标，缺失率高，测试降级处理",
    )
    _add(
        category="missing_degradation",
        query="特斯拉2023年碳排放情况",
        expected_class="complex",
        expected_entities={
            "companies": [],
            "years": [2023],
            "metrics": ["scope_1_emissions"],
            "intent": "qa",
            "industry": "",
        },
        description="数据库外公司，应触发降级或提示不在覆盖范围",
    )
    _add(
        category="missing_degradation",
        query="华能国际和中国大唐2022年清洁能源占比对比",
        expected_class="complex",
        expected_entities={
            "companies": ["华能国际", "中国大唐"],
            "years": [2022],
            "metrics": ["clean_energy_ratio"],
            "intent": "compare",
            "industry": "power",
        },
        description="清洁能源占比口径不一致（装机/发电量），测试口径校验",
    )
    _add(
        category="missing_degradation",
        query="小鹏汽车2024年外部ESG评级",
        expected_class="complex",
        expected_entities={
            "companies": ["小鹏汽车"],
            "years": [2024],
            "metrics": ["external_esg_rating"],
            "intent": "qa",
            "industry": "new_energy",
        },
        description="文本类字段查询，可能为 NULL",
    )
    _add(
        category="missing_degradation",
        query="银行行业2022年客户投诉办结率和监管处罚次数",
        expected_class="complex",
        expected_entities={
            "companies": [],
            "years": [2022],
            "metrics": ["customer_complaint_res", "regulatory_penalties"],
            "intent": "compare",
            "industry": "bank",
        },
        description="部分指标可能缺失，测试部分降级",
    )
    _add(
        category="missing_degradation",
        query="吉利汽车和蔚来汽车2023年能耗强度和碳排放强度对比",
        expected_class="complex",
        expected_entities={
            "companies": ["吉利汽车", "蔚来汽车"],
            "years": [2023],
            "metrics": ["energy_intensity", "scope_1_emissions"],
            "intent": "compare",
            "industry": "new_energy",
        },
        description="能耗强度分母口径可能不一致",
    )
    _add(
        category="missing_degradation",
        query="中广核2022年范围一和范围三碳排放",
        expected_class="complex",
        expected_entities={
            "companies": ["中广核电力"],
            "years": [2022],
            "metrics": ["scope_1_emissions", "scope_3_emissions"],
            "intent": "qa",
            "industry": "power",
        },
        description="同时查通用表和子表指标，scope_3 可能缺失",
    )

    # ── 4. 意图需澄清 / 知识问答（Clarify & Knowledge）─────────────────────────
    _add(
        category="clarify",
        query="帮我分析一下ESG表现",
        expected_class="clarify",
        expected_entities={
            "companies": [],
            "years": [],
            "metrics": [],
            "intent": "qa",
            "industry": "",
        },
        description="无任何实体，应反问",
    )
    _add(
        category="clarify",
        query="对比一下碳排放",
        expected_class="clarify",
        expected_entities={
            "companies": [],
            "years": [],
            "metrics": ["scope_1_emissions"],
            "intent": "compare",
            "industry": "",
        },
        description="有指标但缺公司，应反问对比谁",
    )
    _add(
        category="clarify",
        query="2023年的数据怎么样？",
        expected_class="clarify",
        expected_entities={
            "companies": [],
            "years": [2023],
            "metrics": [],
            "intent": "qa",
            "industry": "",
        },
        description="有年份但缺公司和指标",
    )
    _add(
        category="clarify",
        query="什么是ESG？",
        expected_class="knowledge",
        expected_entities={
            "companies": [],
            "years": [],
            "metrics": [],
            "intent": "qa",
            "industry": "",
        },
        description="纯知识问题，直接 LLM 回答",
    )
    _add(
        category="clarify",
        query="碳中和是什么意思？和碳达峰有什么区别？",
        expected_class="knowledge",
        expected_entities={
            "companies": [],
            "years": [],
            "metrics": [],
            "intent": "qa",
            "industry": "",
        },
        description="概念性知识问题",
    )
    _add(
        category="clarify",
        query="GRI标准和TCFD框架有什么区别？",
        expected_class="knowledge",
        expected_entities={
            "companies": [],
            "years": [],
            "metrics": [],
            "intent": "qa",
            "industry": "",
        },
        description="ESG 框架知识问题",
    )
    _add(
        category="clarify",
        query="绿色金融是什么？银行怎么做绿色金融？",
        expected_class="knowledge",
        expected_entities={
            "companies": [],
            "years": [],
            "metrics": [],
            "intent": "qa",
            "industry": "",
        },
        description="绿色金融概念问题",
    )
    _add(
        category="clarify",
        query="范围一范围二范围三碳排放分别是什么？",
        expected_class="knowledge",
        expected_entities={
            "companies": [],
            "years": [],
            "metrics": [],
            "intent": "qa",
            "industry": "",
        },
        description="碳排放分类知识问题",
    )
    _add(
        category="clarify",
        query="你好",
        expected_class="clarify",
        expected_entities={
            "companies": [],
            "years": [],
            "metrics": [],
            "intent": "qa",
            "industry": "",
        },
        description="纯寒暄，应走 refuse/clarify",
    )
    _add(
        category="clarify",
        query="帮我做个PPT",
        expected_class="clarify",
        expected_entities={
            "companies": [],
            "years": [],
            "metrics": [],
            "intent": "qa",
            "industry": "",
        },
        description="完全超出范围，应拒绝或反问",
    )

    return cases


# ══════════════════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="生成 ESG Agent 离线评测数据集")
    parser.add_argument(
        "-o", "--output",
        type=str,
        default="eval_dataset.jsonl",
        help="输出文件路径（默认：eval_dataset.jsonl）",
    )
    args = parser.parse_args()

    cases = _build_cases()
    out_path = Path(args.output)

    with out_path.open("w", encoding="utf-8") as f:
        for case in cases:
            f.write(json.dumps(case, ensure_ascii=False) + "\n")

    # 统计信息
    from collections import Counter
    cat_counts = Counter(c["category"] for c in cases)
    class_counts = Counter(c["expected_class"] for c in cases)

    print(f"[OK] Generated {len(cases)} eval cases -> {out_path}")
    print(f"     Category dist: {dict(cat_counts)}")
    print(f"     Class dist:    {dict(class_counts)}")


if __name__ == "__main__":
    main()
