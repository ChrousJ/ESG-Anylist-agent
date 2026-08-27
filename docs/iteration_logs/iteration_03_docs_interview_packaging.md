# Iteration 03：文档体系与秋招面试包装加固

## 1. 本轮目标

用户要求继续阅读 `docs/` 并提升项目文档，使后续能通过文档搞清楚：

- 项目当前到底改了什么；
- 面试时应该怎么讲；
- 哪些边界不能夸大；
- 下一步如何用评测结果增强可信度。

## 2. 修改前问题

- 项目对外名称混用 `ESG-Anylist Agent` 和 `ESG-Insight Agent`；
- `roadmap.md` 中部分已实现能力仍是未勾选状态；
- `project-story.md` 中节点数量和当前 LangGraph 实现不完全一致；
- 缺少专门的架构说明、代码地图、简历素材、Demo 脚本、Data Card 和风险边界文档；
- `.gitignore` 全局忽略 `*.md`，不利于 README 和 docs 纳入版本管理；
- `docs/.DS_Store` 等系统文件污染文档目录。

## 3. 修改内容

| 文件 | 修改点 | 目的 |
|---|---|---|
| `README.md` | 统一对外项目名为 ESG-Insight Agent，更新核心功能和架构图，新增文档入口 | 提升项目门面一致性 |
| `.gitignore` | 移除全局忽略 `*.md/*.json/*.jsonl`，改为只忽略临时输出 | 避免文档无法被版本管理 |
| `docs/README.md` | 新增文档索引 | 让面试官/开发者/自己准备面试都有阅读路径 |
| `docs/architecture.md` | 新增当前 LangGraph 架构、节点职责、降级路径 | 技术面试可直接讲架构 |
| `docs/code-map.md` | 新增能力到代码文件映射 | 便于现场打开代码讲实现 |
| `docs/resume-bullets.md` | 新增简历 bullet 和 STAR 讲法 | 直接服务秋招简历和面试 |
| `docs/demo-script.md` | 新增 3 分钟 / 8 分钟 Demo 流程和翻车预案 | 降低现场演示风险 |
| `docs/data-card.md` | 新增数据覆盖、缺失值处理、capabilities 说明 | 强化可靠性和边界意识 |
| `docs/model-risk-and-boundaries.md` | 新增模型风险、绿漂定义、合规边界 | 回答金融/ESG 风险追问 |
| `docs/experiment-results.md` | 新增实验结果入口和指标模板 | 为后续正式评测留位置 |
| `docs/roadmap.md` | 更新当前状态、Next 7 Days、Interview-ready checklist、披露评分任务状态 | 让计划与实现同步 |
| `docs/decision-log.md` | 新增确定性 Rubric 和 claim-evidence mismatch 两条决策 | 补齐重要技术取舍留痕 |
| `docs/traceability.md` | 更新文档体系和目录说明 | 让留痕规范匹配当前仓库 |
| `docs/demo-cases.md` | 增加通用观察点和翻车预案 | 提升演示可执行性 |
| `docs/project-story.md` | 更新项目名和 LangGraph 节点描述 | 避免故事文档与代码脱节 |

## 4. 验证

```bash
python3 -m unittest discover -s tests
```

结果：

```text
Ran 5 tests in 0.002s
OK
```

## 5. 面试表达

本轮后，项目可以从三个层次讲：

1. **项目定位**：ESG-Insight Agent，不是普通 PDF QA；
2. **技术架构**：LangGraph 白盒节点 + SQL/RAG 双证据 + 双 Evaluator；
3. **业务亮点**：披露质量评分 + 绿漂风险人工核查点 + 数据边界和降级策略。

## 6. 下一步

- 扩充 eval dataset 中的 disclosure / greenwashing cases；
- 让 `scripts/run_evaluation.py` 输出 disclosure score presence、greenwashing risk presence、citation presence；
- 跑一次 20 条小样本评测，把结果填入 `docs/experiment-results.md`；
- 准备 demo 截图或录屏。
