"""
agent/materiality.py  —  行业实质性议题表 & 查询增强逻辑
=========================================================

【什么是"实质性议题"（Materiality Topics）？】

ESG（环境、社会、治理）涵盖几百个细分指标，但不是每个指标对每个行业
都同样重要。"实质性议题"就是指对某个特定行业最核心、最有影响力的 ESG 话题。

例如：
  - 新能源行业 → "电池回收与循环利用"、"供应链碳足迹" 是关键议题
  - 金融行业 → "绿色金融"、"反洗钱" 是关键议题
  - 消费品行业 → "包装材料可回收性"、"供应商劳工权益" 是关键议题

【这个模块的用途】

Supervisor 在规划 RAG 检索策略时，会调用这个模块来：
  1. 获取当前行业的核心实质性议题列表
  2. 把用户的宽泛查询扩展为带实质性维度的检索变体
     例如："比亚迪碳排放" → ["比亚迪碳排放强度与脱碳承诺",
                              "比亚迪电池全生命周期碳足迹"]

设计原则：
  · 全部硬编码，不依赖 LLM，零延迟，确定性强
  · 议题颗粒度：足够细到能生成有意义的检索 query，
    但不能细到变成指标（那是 SQL 的职责）
  · 每个议题都带「检索关键词」字段，
    直接拼进 RAG Worker 的 query 变体
"""

from __future__ import annotations
from dataclasses import dataclass, field


# ══════════════════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class MaterialityTopic:
    """
    单条实质性议题。
    search_keywords 直接用于 RAG 检索 query 的构造。
    related_metrics 与 SQLite 的 metric_key 对应，
    供 Supervisor 判断这条议题需不需要走 SQL Worker。
    """
    topic_id:        str           # 唯一标识，如 "ne_battery_recycling"
    name:            str           # 中文名称
    description:     str           # 议题说明（注入 Synthesizer prompt 用）
    search_keywords: list[str]     # RAG 检索关键词列表
    related_metrics: list[str]     # 对应的 SQLite metric_key
    priority:        int           # 1=核心 / 2=重要 / 3=一般
    applicable_to:   list[str]     # 适用行业


# ══════════════════════════════════════════════════════════════════════════════
# 1.  新能源 / 汽车制造行业实质性议题
# ══════════════════════════════════════════════════════════════════════════════

NEW_ENERGY_TOPICS: list[MaterialityTopic] = [

    MaterialityTopic(
        topic_id="ne_battery_lifecycle",
        name="电池全生命周期碳足迹与回收",
        description=(
            "动力电池从原材料开采、生产制造到报废回收的全生命周期碳排放，"
            "以及电池回收体系建设、梯次利用规模和回收率目标完成情况。"
            "是新能源汽车行业最核心的 ESG 实质性议题，直接影响 MSCI 评级。"
        ),
        search_keywords=[
            "电池回收", "动力电池回收率", "梯次利用", "电池全生命周期",
            "报废电池", "电池碳足迹", "LFP回收", "三元电池回收",
            "电池材料再生", "回收网络", "电池溯源",
        ],
        related_metrics=["scope_3_emissions", "scope_1_emissions"],
        priority=1,
        applicable_to=["new_energy"],
    ),

    MaterialityTopic(
        topic_id="ne_supply_chain_labor",
        name="供应链劳工标准与人权",
        description=(
            "核心供应商（尤其是锂、钴、镍等原材料供应商）的劳工权益保障、"
            "童工禁止、矿山安全生产标准，以及供应商 ESG 审核体系覆盖情况。"
            "钴矿产地（刚果金）的劳工问题是国际 ESG 评级机构重点关注项。"
        ),
        search_keywords=[
            "供应链管理", "供应商审核", "供应商ESG评估", "负责任采购",
            "原材料溯源", "钴矿来源", "矿山安全", "劳工标准",
            "供应商行为准则", "核心供应商", "供应链尽职调查",
        ],
        related_metrics=["supplier_esg_audit_ratio"],
        priority=1,
        applicable_to=["new_energy"],
    ),

    MaterialityTopic(
        topic_id="ne_carbon_emissions",
        name="运营碳排放与脱碳路径",
        description=(
            "制造工厂的直接排放（Scope 1）、外购电力排放（Scope 2）"
            "及价值链排放（Scope 3），以及公司的碳中和目标、"
            "减排技术路线（绿电采购、工艺改造、碳汇）的落地进度。"
        ),
        search_keywords=[
            "碳排放", "温室气体排放", "Scope 1", "Scope 2", "Scope 3",
            "碳中和", "碳达峰", "减碳目标", "绿电", "可再生能源采购",
            "碳强度", "单位产值碳排放", "脱碳路径",
        ],
        related_metrics=[
            "scope_1_emissions", "scope_2_emissions",
            "scope_3_emissions", "total_energy_consumption",
        ],
        priority=1,
        applicable_to=["new_energy"],
    ),

    MaterialityTopic(
        topic_id="ne_rd_innovation",
        name="研发投入与技术路线竞争力",
        description=(
            "年度研发投入强度（研发费用/营收）、核心技术专利数量、"
            "固态电池/快充/智能驾驶等下一代技术的研发进展，"
            "以及技术路线选择对长期 ESG 竞争力的影响。"
        ),
        search_keywords=[
            "研发投入", "研发费用", "技术创新", "专利", "固态电池",
            "智能驾驶", "快充技术", "研发人员", "核心技术",
            "技术路线", "创新投入", "研发强度",
        ],
        related_metrics=["rd_investment_total"],
        priority=1,
        applicable_to=["new_energy"],
    ),

    MaterialityTopic(
        topic_id="ne_product_safety",
        name="产品安全与召回管理",
        description=(
            "车辆自燃、电池热失控等安全事故记录、召回次数与规模，"
            "以及主动安全管理体系建设情况。"
            "直接影响品牌声誉和监管合规风险。"
        ),
        search_keywords=[
            "产品召回", "安全事故", "自燃", "热失控", "电池安全",
            "质量管理", "产品安全", "FMEA", "安全认证",
            "缺陷处理", "主动召回",
        ],
        related_metrics=["safety_accidents_count", "regulatory_penalties"],
        priority=2,
        applicable_to=["new_energy"],
    ),

    MaterialityTopic(
        topic_id="ne_data_privacy",
        name="用户数据安全与隐私保护",
        description=(
            "智能网联汽车收集的用户驾驶数据、位置数据的存储、使用和跨境传输合规，"
            "以及数据安全事件记录。在欧盟 GDPR 和国内《数据安全法》框架下的合规风险。"
        ),
        search_keywords=[
            "数据安全", "用户隐私", "数据保护", "网络安全",
            "个人信息保护", "数据合规", "GDPR", "数据跨境",
            "信息安全", "车联网数据",
        ],
        related_metrics=["regulatory_penalties"],
        priority=2,
        applicable_to=["new_energy"],
    ),
]


# ══════════════════════════════════════════════════════════════════════════════
# 2.  电力行业实质性议题
# ══════════════════════════════════════════════════════════════════════════════

POWER_TOPICS: list[MaterialityTopic] = [

    MaterialityTopic(
        topic_id="pw_clean_energy_transition",
        name="清洁能源转型进度",
        description=(
            "非化石能源（风电、光伏、水电、核电）装机容量及占比，"
            "清洁能源发电量占比，以及退煤路径和时间表。"
            "是电力行业 MSCI 评级权重最高的单项议题。"
        ),
        search_keywords=[
            "清洁能源", "可再生能源", "风电", "光伏", "水电", "核电",
            "装机容量", "非化石能源占比", "退煤", "煤电转型",
            "绿电", "清洁能源装机", "新能源发电",
        ],
        related_metrics=["clean_energy_ratio", "total_energy_consumption"],
        priority=1,
        applicable_to=["power"],
    ),

    MaterialityTopic(
        topic_id="pw_carbon_intensity",
        name="碳排放强度与脱碳承诺",
        description=(
            "单位发电量碳排放强度（gCO₂/kWh），绝对碳排放量，"
            "碳达峰/碳中和目标设定及完成进度，CCUS 等脱碳技术部署情况。"
        ),
        search_keywords=[
            "碳排放强度", "单位发电量碳排放", "碳达峰", "碳中和",
            "温室气体排放", "CCUS", "碳捕集", "减排目标",
            "碳排放总量", "Scope 1排放",
        ],
        related_metrics=[
            "scope_1_emissions", "scope_2_emissions",
            "scope_3_emissions", "energy_intensity",
        ],
        priority=1,
        applicable_to=["power"],
    ),

    MaterialityTopic(
        topic_id="pw_stranded_asset",
        name="煤电资产搁浅风险",
        description=(
            "存量煤电机组的账面价值、剩余使用年限、提前退役风险，"
            "以及气候转型情景下的资产减值压力测试。"
            "TCFD 框架要求披露的核心气候财务风险。"
        ),
        search_keywords=[
            "煤电机组", "搁浅资产", "资产减值", "煤电退出",
            "碳资产", "气候风险", "TCFD", "转型风险",
            "化石能源资产", "煤电容量", "提前退役",
        ],
        related_metrics=["regulatory_penalties", "external_esg_rating"],
        priority=1,
        applicable_to=["power"],
    ),

    MaterialityTopic(
        topic_id="pw_grid_efficiency",
        name="输配电效率与线损管理",
        description=(
            "电网输配电损耗率（线损率），智能电网建设投入，"
            "储能配套规模。直接影响运营碳排放和经济效益。"
        ),
        search_keywords=[
            "线损率", "输配电损耗", "电网效率", "智能电网",
            "储能", "调峰", "电网升级", "配电网", "降损措施",
        ],
        related_metrics=["energy_intensity", "total_energy_consumption"],
        priority=2,
        applicable_to=["power"],
    ),

    MaterialityTopic(
        topic_id="pw_env_compliance",
        name="环保合规与排污管理",
        description=(
            "火电机组的 SO₂、NOₓ、烟尘等大气污染物排放达标情况，"
            "环保违规处罚记录，超低排放改造完成比例。"
        ),
        search_keywords=[
            "环保合规", "排污", "SO₂排放", "NOx排放", "烟尘",
            "超低排放", "环保处罚", "排放达标", "脱硫脱硝",
            "环境违规", "环保改造",
        ],
        related_metrics=["regulatory_penalties", "scope_1_emissions"],
        priority=2,
        applicable_to=["power"],
    ),

    MaterialityTopic(
        topic_id="pw_rd_innovation",
        name="清洁技术研发投入",
        description=(
            "储能技术、氢能、CCUS、智能电网等清洁技术的研发投入，"
            "以及技术成果转化情况。"
        ),
        search_keywords=[
            "研发投入", "技术创新", "储能技术", "氢能", "清洁技术",
            "研发费用", "技术研发", "科技创新", "专利",
        ],
        related_metrics=["rd_investment_total"],
        priority=2,
        applicable_to=["power"],
    ),
]


# ══════════════════════════════════════════════════════════════════════════════
# 3.  银行行业实质性议题
# ══════════════════════════════════════════════════════════════════════════════

BANK_TOPICS: list[MaterialityTopic] = [

    MaterialityTopic(
        topic_id="bk_green_finance",
        name="绿色金融规模与结构",
        description=(
            "绿色贷款余额、绿色债券发行规模、绿色基金管理规模，"
            "以及绿色信贷在行业内的分布结构（清洁能源/绿色建筑/污染防治等）。"
            "是银行 ESG 评级中权重最高的单项指标。"
        ),
        search_keywords=[
            "绿色贷款", "绿色信贷", "绿色金融", "绿色债券",
            "绿色贷款余额", "清洁能源贷款", "绿色融资",
            "ESG贷款", "可持续金融", "绿色资产",
        ],
        related_metrics=["green_finance_balance"],
        priority=1,
        applicable_to=["bank"],
    ),

    MaterialityTopic(
        topic_id="bk_brown_exposure",
        name="高碳/棕色资产敞口",
        description=(
            "对煤炭、油气等高碳行业的贷款敞口规模和占比，"
            "以及退出高碳行业的路径和时间表。"
            "直接影响气候转型风险下的资产质量压力。"
        ),
        search_keywords=[
            "煤炭贷款", "高碳行业", "棕色资产", "化石能源敞口",
            "碳密集行业", "转型风险", "煤矿贷款", "石油贷款",
            "高碳客户", "行业退出", "限制行业",
        ],
        related_metrics=["regulatory_penalties", "external_esg_rating"],
        priority=1,
        applicable_to=["bank"],
    ),

    MaterialityTopic(
        topic_id="bk_climate_risk",
        name="气候风险整合与压力测试",
        description=(
            "将气候实体风险（洪涝、干旱等）和转型风险整合进信贷审批流程，"
            "以及开展气候情景分析和压力测试的情况。"
            "监管机构（央行、银保监）重点关注项。"
        ),
        search_keywords=[
            "气候风险", "压力测试", "气候情景分析", "TCFD",
            "环境风险", "ESG风险管理", "碳风险敞口",
            "气候相关信息披露", "物理风险", "转型风险",
        ],
        related_metrics=["esg_committee_setup", "external_esg_rating"],
        priority=1,
        applicable_to=["bank"],
    ),

    MaterialityTopic(
        topic_id="bk_inclusive_finance",
        name="普惠金融覆盖与质量",
        description=(
            "普惠型小微企业贷款余额、户数、增速，涉农贷款规模，"
            "以及普惠金融的不良率水平（覆盖广度 vs 资产质量平衡）。"
        ),
        search_keywords=[
            "普惠金融", "小微企业贷款", "涉农贷款", "普惠型贷款",
            "小微贷款", "农业贷款", "乡村振兴", "普惠贷款余额",
            "小微企业", "首贷户",
        ],
        related_metrics=["inclusive_finance_balance"],
        priority=1,
        applicable_to=["bank"],
    ),

    MaterialityTopic(
        topic_id="bk_compliance_governance",
        name="合规治理与反腐败",
        description=(
            "监管处罚记录（次数、金额）、反洗钱合规、"
            "反腐败培训覆盖率，以及高管薪酬与 ESG 指标挂钩情况。"
        ),
        search_keywords=[
            "监管处罚", "合规", "反洗钱", "反腐败",
            "行政处罚", "违规处罚", "内控管理", "廉洁从业",
            "合规培训", "处罚记录", "监管风险",
        ],
        related_metrics=[
            "regulatory_penalties", "anti_corruption_coverage",
        ],
        priority=1,
        applicable_to=["bank"],
    ),

    MaterialityTopic(
        topic_id="bk_consumer_protection",
        name="消费者权益保护",
        description=(
            "客户投诉办结率、理财产品适当性管理、"
            "个人金融信息保护，以及监管关于消费者保护专项检查结果。"
        ),
        search_keywords=[
            "消费者保护", "客户投诉", "投诉处理", "理财销售",
            "适当性管理", "金融消费者", "客户权益",
            "个人信息保护", "客户满意度",
        ],
        related_metrics=["customer_complaint_res", "regulatory_penalties"],
        priority=2,
        applicable_to=["bank"],
    ),
]


# ══════════════════════════════════════════════════════════════════════════════
# 4.  通用议题（三个行业共用）
# ══════════════════════════════════════════════════════════════════════════════

UNIVERSAL_TOPICS: list[MaterialityTopic] = [

    MaterialityTopic(
        topic_id="uni_board_governance",
        name="董事会结构与独立性",
        description=(
            "董事会规模、独立董事占比、女性董事占比，"
            "以及 ESG 专项委员会设立情况。"
            "治理结构是 ESG 评级中 G 维度的核心评分项。"
        ),
        search_keywords=[
            "董事会", "独立董事", "女性董事", "董事会结构",
            "ESG委员会", "审计委员会", "薪酬委员会",
            "董事会多元化", "治理结构", "监事会",
        ],
        related_metrics=[
            "independent_director_ratio", "female_director_ratio",
            "esg_committee_setup",
        ],
        priority=2,
        applicable_to=["new_energy", "power", "bank"],
    ),

    MaterialityTopic(
        topic_id="uni_employee_welfare",
        name="员工发展与职业安全",
        description=(
            "员工培训投入与人均培训时长，职业健康安全事故率，"
            "员工离职率与人才吸引力。"
        ),
        search_keywords=[
            "员工培训", "人均培训", "职业安全", "安全生产",
            "工伤", "员工发展", "人才培养", "职工培训",
            "安全事故", "伤亡率",
        ],
        related_metrics=[
            "employee_training_hours", "safety_accidents_count",
        ],
        priority=2,
        applicable_to=["new_energy", "power", "bank"],
    ),
]


# ══════════════════════════════════════════════════════════════════════════════
# 5.  汇总索引
# ══════════════════════════════════════════════════════════════════════════════

# 按行业索引
TOPICS_BY_INDUSTRY: dict[str, list[MaterialityTopic]] = {
    "new_energy": NEW_ENERGY_TOPICS + UNIVERSAL_TOPICS,
    "power":      POWER_TOPICS + UNIVERSAL_TOPICS,
    "bank":       BANK_TOPICS + UNIVERSAL_TOPICS,
}

# 按 topic_id 索引（全局查找）
ALL_TOPICS: dict[str, MaterialityTopic] = {
    t.topic_id: t
    for topics in TOPICS_BY_INDUSTRY.values()
    for t in topics
}

# 按 metric_key 反查相关议题
METRIC_TO_TOPICS: dict[str, list[str]] = {}
for _topic in ALL_TOPICS.values():
    for _metric in _topic.related_metrics:
        METRIC_TO_TOPICS.setdefault(_metric, []).append(_topic.topic_id)


# ══════════════════════════════════════════════════════════════════════════════
# 6.  对外接口函数
# ══════════════════════════════════════════════════════════════════════════════

def get_topics_for_industry(
    industry: str,
    priority_threshold: int = 3,
) -> list[MaterialityTopic]:
    """
    获取某行业的实质性议题列表。
    priority_threshold: 只返回优先级 <= 该值的议题（1=只返回核心）
    """
    topics = TOPICS_BY_INDUSTRY.get(industry, UNIVERSAL_TOPICS)
    return [t for t in topics if t.priority <= priority_threshold]


def get_topics_for_metrics(metric_keys: list[str]) -> list[MaterialityTopic]:
    """
    根据用户查询的指标列表，返回相关的实质性议题。
    用于 Supervisor 在 "用户已明确指标" 的情况下精准注入议题。
    """
    topic_ids = set()
    for key in metric_keys:
        topic_ids.update(METRIC_TO_TOPICS.get(key, []))
    return [ALL_TOPICS[tid] for tid in topic_ids if tid in ALL_TOPICS]


def build_materiality_query_variants(
    original_query: str,
    industry: str,
    metric_keys: list[str] | None = None,
    max_topics: int = 4,
) -> list[str]:
    """
    核心函数：把原始 query 扩展为带实质性议题维度的检索变体列表。

    逻辑：
      1. 如果用户已指定 metric_keys，优先取这些指标对应的议题
      2. 否则取该行业 priority=1 的核心议题
      3. 每个议题的 search_keywords 拼成检索变体
      4. 返回：[原始query] + [议题增强变体1, 议题增强变体2, ...]

    示例：
      original_query = "分析比亚迪的ESG表现"
      industry = "new_energy"
      →
      [
        "分析比亚迪的ESG表现",
        "比亚迪 电池回收 动力电池回收率 梯次利用",
        "比亚迪 供应链劳工标准 供应商ESG审核覆盖率",
        "比亚迪 碳排放 Scope1 Scope2 碳中和目标",
        "比亚迪 研发投入 技术路线 固态电池",
      ]
    """
    if metric_keys:
        topics = get_topics_for_metrics(metric_keys)
        # 如果指标没匹配到议题，fallback 到行业核心议题
        if not topics:
            topics = get_topics_for_industry(industry, priority_threshold=1)
    else:
        topics = get_topics_for_industry(industry, priority_threshold=1)

    # 截取前 max_topics 个议题
    selected_topics = topics[:max_topics]

    variants = [original_query]
    for topic in selected_topics:
        # 取前5个关键词拼成检索 query
        keywords = " ".join(topic.search_keywords[:5])
        # 提取 query 里的公司名（简单启发式：query里的中文名词）
        variant = f"{keywords}"
        variants.append(variant)

    return variants


def get_materiality_context_for_synthesizer(
    industry: str,
    metric_keys: list[str] | None = None,
) -> str:
    """
    为 Synthesizer 生成实质性议题背景说明文本。
    注入到四层分析的 prompt 里，让 LLM 知道
    "这个行业真正重要的是什么"，避免只罗列数字。
    """
    if metric_keys:
        topics = get_topics_for_metrics(metric_keys)
        if not topics:
            topics = get_topics_for_industry(industry, priority_threshold=2)
    else:
        topics = get_topics_for_industry(industry, priority_threshold=2)

    lines = [f"=== {industry_display(industry)} 核心实质性议题 ===\n"]
    for t in topics:
        priority_label = {1: "🔴 核心", 2: "🟡 重要", 3: "🟢 一般"}.get(
            t.priority, ""
        )
        lines.append(f"【{priority_label}】{t.name}")
        lines.append(f"  {t.description}")
        lines.append("")

    lines.append(
        "分析时必须围绕上述实质性议题展开，"
        "不得仅罗列数字，需结合议题重要性给出价值判断。"
    )
    return "\n".join(lines)


def industry_display(industry: str) -> str:
    """行业代码转中文显示名。"""
    return {
        "new_energy": "新能源 / 汽车制造",
        "power":      "电力",
        "bank":       "银行 / 金融",
        "mixed":      "跨行业",
    }.get(industry, industry)