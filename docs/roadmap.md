# ESG-Insight Agent 落地路线图

> 目的：将项目建设拆解为可执行、可验收、可回溯的阶段任务。  
> 当前版本：v0.2  
> 更新日期：2026-08-09

---

## 0. 总体目标

在秋招项目展示和结业论文要求下，最终交付一个完整闭环：

```text
一个真实金融文本场景
+ 一个可运行的 LangGraph Agent 应用
+ 一个自动化评测体系
+ 一个 Bad Case 归因和优化闭环
+ 一组对比实验
+ 一篇格式标准的结业论文
+ 一套面试讲述材料
```

项目不追求大而全，而追求“小而深”：围绕 **ESG 披露质量评估与绿漂风险识别** 深挖。

## 当前状态快照（2026-08-09）

| 模块 | 状态 | 说明 |
|---|---|---|
| LangGraph 主流程 | 已完成 v1 | SQL/RAG、双 evaluator、降级路径、记忆更新已接入 |
| `/api/capabilities` | 已完成 v1 | 可扫描 PDF、SQLite、指标字典和能力边界 |
| 披露质量评分 | 已完成 v1 | 确定性五维 Rubric，已作为 `disclosure_scorer` 节点接入 |
| 绿漂风险雷达 | 已完成 v1 | 规则型 claim-evidence mismatch，已作为 `greenwashing_detector` 节点接入 |
| 前端展示 | 已完成 v1 | 节点 pipeline、披露评分卡片、风险雷达卡片 |
| 单元测试 | 已完成 v1 | capabilities / disclosure quality / greenwashing 轻量测试 |
| 正式评测结果 | 部分就绪 | smoke eval 已跑通产物链路，但当前环境缺少 `langgraph`，需要依赖完整环境下重跑业务指标 |

---

## Next 7 Days：最优先落地任务

- [x] 生成 50 条 eval case，并新增 `eval/datasets/esg_eval_smoke.jsonl` 作为 10 条快速 smoke eval；
- [x] 扩展 `scripts/run_evaluation.py` 指标：evidence/citation presence、disclosure score presence、greenwashing radar presence；
- [x] 跑一次小样本评测并把结果写入 `docs/experiment-results.md`（本机依赖缺失，已记录为环境失败，不作为业务效果）；
- [ ] 准备 2-3 张 demo 截图或一段录屏，避免面试现场外部 API 不稳定；
- [ ] 将简历项目名统一为 ESG-Insight Agent。

---

## Interview-ready Checklist

- [x] README 能 1 分钟说明项目定位；
- [x] `docs/architecture.md` 能说明当前架构；
- [x] `docs/resume-bullets.md` 能直接转简历；
- [x] `docs/demo-script.md` 能指导现场演示；
- [x] `docs/model-risk-and-boundaries.md` 能回答绿漂误判和金融合规边界；
- [ ] `docs/experiment-results.md` 有依赖完整环境下的真实业务指标；
- [ ] demo 截图 / 录屏可离线展示。

---

## 1. Phase 0：项目定位与文档沉淀

### 目标

统一项目故事、产品定位、用户场景和后续开发方向。

### 任务

- [x] 梳理“为什么选择 ESG 报告分析”的故事；
- [x] 明确目标用户和消费场景；
- [x] 明确项目不做什么；
- [x] 将“ESG 披露质量 + 绿漂风险识别”作为主线；
- [x] 建立路线图和可追踪文档体系。

### 产出

- `docs/project-story.md`
- `docs/product-design.md`
- `docs/roadmap.md`
- `docs/decision-log.md`
- `docs/traceability.md`

### 验收标准

- 能用 3 分钟讲清楚项目为什么做、做给谁、解决什么问题；
- 后续每个开发任务都能映射到某个业务痛点或评测指标。

---

## 2. Phase 1：构建最小可评测数据集

### 时间建议

第 1 周。

### 目标

构建一个小而高质量的 ESG Agent Benchmark，使项目从“主观感觉效果不错”升级为“有数据、有指标、有样本”。

### 范围

优先选择新能源汽车产业链：

- 比亚迪；
- 宁德时代；
- 长城汽车；
- 长安汽车；
- 广汽集团；
- 亿纬锂能；
- 国轩高科；
- 华友钴业。

### 样本类型

| 类型 | 建议数量 | 示例 |
|---|---:|---|
| 单指标事实查询 | 20 | 比亚迪 2024 年温室气体排放量是多少？ |
| 跨年趋势分析 | 15 | 宁德时代 2022-2024 年能源消耗趋势如何？ |
| 同行横向对比 | 15 | 对比比亚迪和长城汽车 2024 年碳排放披露情况。 |
| 披露质量判断 | 20 | 广汽集团是否连续披露员工培训指标？ |
| 绿漂风险识别 | 15 | 华友钴业报告中有哪些缺少数据支撑的绿色承诺？ |

总量：第一版 60-100 条即可。

### 建议目录

```text
ESG-Anylist-agent-master/eval/
  datasets/
    esg_eval_v1.jsonl
  rubrics/
    disclosure_quality_rubric.yaml
    greenwashing_rubric.yaml
  README.md
```

### 验收标准

- 每条 case 有明确 query、任务类型、公司、年份、指标；
- 关键 case 有 expected evidence 或 judge rubric；
- 能被 `scripts/run_evaluation.py` 消费。

---

## 3. Phase 2：评测引擎升级

### 时间建议

第 2 周。

### 目标

将当前评测脚本升级为能输出多维指标、Bad Case 和运行报告的自动化评测流水线。

### 离线质量指标

| 指标 | 含义 |
|---|---|
| Completion Rate | 是否成功完成任务 |
| Evidence Coverage | 关键结论是否有证据支持 |
| Numeric Faithfulness | 回答中的数字是否来自 SQL / RAG 证据 |
| Entity Consistency | 是否出现用户没问的公司、年份、指标 |
| Disclosure Judgment Accuracy | 披露 / 未披露 / 口径不一致判断是否正确 |
| Greenwashing Risk Recall | 是否识别标注中的有效绿漂风险 |

### 工程指标

| 指标 | 含义 |
|---|---|
| average latency | 平均耗时 |
| p95 latency | 尾延迟 |
| average token cost | 平均 token 消耗 |
| replan rate | Re-plan 触发率 |
| correction rate | 输出修正触发率 |
| degrade rate | 降级率 |
| SQL success rate | SQL 查询成功率 |
| RAG recall success rate | RAG 召回成功率 |

### 输出目录

```text
ESG-Anylist-agent-master/outputs/eval_runs/{run_id}/
  eval_report.md
  metrics.json
  bad_cases.jsonl
  trace_summary.csv
```

### 验收标准

- 一条命令可以跑完整评测；
- 自动生成指标和 bad case；
- 能对比 ReAct baseline 与 LangGraph workflow。

---

## 4. Phase 3：披露质量评分

### 时间建议

第 3 周。

### 目标

实现项目第一个业务亮点：让 Agent 不只是回答“有没有数据”，而是判断“披露质量好不好”。

### 技术任务

- [x] 新增 `agent/disclosure_quality.py`；
- [x] 定义 5 维评分规则；
- [x] 将评分结果接入 LangGraph 主流程和 API 响应；
- [x] 新增 `disclosure_scorer` 节点；
- [ ] 在评测集中增加披露质量相关 case。

### 输出结构示例

```json
{
  "score": 82,
  "level": "B+",
  "dimensions": {
    "completeness": 24,
    "continuity": 18,
    "comparability": 15,
    "verifiability": 17,
    "specificity": 8
  },
  "deductions": [
    "Scope 3 排放未披露",
    "部分减排目标缺少年度进展"
  ],
  "evidence": []
}
```

### 验收标准

- 对任一公司 / 指标 / 年份范围能输出披露质量评分；
- 扣分项必须有证据或明确缺失原因；
- 评分逻辑可以被评测脚本复现和统计。

---

## 5. Phase 4：绿漂风险识别

### 时间建议

第 4 周。

### 目标

实现项目第二个业务亮点：识别 ESG 报告中“强表述、弱证据”的潜在绿漂风险。

### 技术任务

- [x] 新增 `agent/greenwashing.py` 与 `agent/nodes/greenwashing_detector.py`；
- [x] 第一版基于关键词抽取 ESG strong claim；
- [x] 第一版做规则型 claim-evidence matching（强表述附近无数字/进展/鉴证则触发）；
- [x] 第一版定义 vague_commitment / target_without_progress / supply_chain_claim_weak_evidence / safety_claim_weak_evidence 等风险；
- [x] 输出风险等级、claim 原文片段、证据缺口、公司/年份/页码；
- [ ] 在评测集中增加 greenwashing case。

### 风险类型

- 空泛承诺；
- 目标无进展；
- 选择性披露；
- 口径漂移；
- 证据不足。

### 验收标准

- 能指出原文中的风险表述；
- 能解释为什么证据不足；
- 不把所有宣传性语言都粗暴判成绿漂，而是标记为“潜在风险”。

---

## 6. Phase 5：Bad Case 归因与数据飞轮

### 时间建议

第 5 周。

### 目标

让评测结果不止是分数，而是能驱动修复。

### 错误类型

| 错误类型 | 修复方向 |
|---|---|
| Intent Error | 优化 context node |
| Entity Error | 加实体词典、别名表 |
| SQL Error | 优化 schema prompt / SQL validator |
| RAG Recall Error | 优化 query rewrite / chunking / rerank |
| Evidence Missing | 强制 citation / 改 synthesizer |
| Numeric Hallucination | 数字校验器 |
| Trend Error | 加趋势计算工具 |
| Judge Error | 调整 rubric |

### 技术任务

- [ ] 新增 `eval/error_analyzer.py`；
- [ ] 从 trace、sql_result、rag_chunks、judge_result 中归因；
- [ ] 输出 `primary_error`、`root_cause`、`suggested_fix`；
- [ ] 建立 `docs/iteration_logs/`。

### 验收标准

- 每个 bad case 至少有一个主错误类型；
- 每个主错误类型有修复建议；
- 每轮修复后能做回归评测。

---

## 7. Phase 6：系统优化实验

### 时间建议

第 6 周。

### 目标

形成论文和面试都能展示的 ablation study。

### 对比实验

| 实验组 | 说明 |
|---|---|
| ReAct Baseline | 黑盒 ReAct Agent |
| LangGraph without Evaluator | 白盒 workflow，但无质量检查 |
| LangGraph + Evaluator-D | 加数据质量检查 |
| LangGraph + Evaluator-D + Evaluator-O | 加输出质量检查 |
| Full System | 加披露质量评分和绿漂识别 |

### 验收标准

- 有真实指标表；
- 有 bad case 分析；
- 能说明效果、成本、延迟之间的 trade-off。

---

## 8. Phase 7：Demo 与可视化

### 时间建议

第 7 周。

### 目标

让面试官一眼看懂系统，而不只是看到一个聊天框。

### 前端展示模块

- 用户问题；
- Agent 节点执行轨迹；
- SQL / RAG 证据；
- 披露质量评分；
- 绿漂风险；
- 最终报告；
- 评测 Dashboard。

### 验收标准

- 能现场演示一个横向对比问题；
- 能展示每个结论的证据来源；
- 能展示系统评测指标和 bad case 分布。

---

## 9. Phase 8：论文与面试材料

### 时间建议

第 8 周。

### 目标

完成结业论文初稿、README 和项目讲述材料。

### 论文题目

中文：

> 基于评测驱动工作流的 ESG 报告分析智能体研究

英文：

> An Evaluation-Driven Agentic Workflow for ESG Report Analysis

### 产出

```text
paper/esg_eval_agent_paper.md
docs/interview-script.md
README.md
```

### 验收标准

- 有论文完整结构；
- 有 3 分钟、8 分钟、15 分钟项目讲述版本；
- 简历 bullet 能准确表达项目亮点。
