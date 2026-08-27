# ESG-Insight Agent 秋招项目审查报告

> 审查日期：2026-08-12  
> 结论：项目已经具备明显的 Agent 架构和 ESG 业务差异化，但要成为“王牌项目”，最需要补的是正式评测证据、可复现运行环境和离线 Demo 资产，而不是继续增加节点。

## 1. 综合评价

| 维度 | 当前评价 | 说明 |
|---|---|---|
| 业务选题 | 8.5/10 | ESG 长文档、结构化指标和绿漂核查有真实业务复杂度 |
| Agent 架构 | 8/10 | 有显式状态图、并行 worker、双 evaluator、修正与降级 |
| 差异化 | 8/10 | 披露评分和 claim-evidence mismatch 比普通 RAG 更有记忆点 |
| 工程质量 | 7/10 | 有 API、SSE、trace、测试；本轮新增 doctor 和 CI |
| 评测可信度 | 4.5/10 | 评测框架完整，但还没有依赖完整环境下的正式业务结果 |
| 可复现性 | 5.5/10 | 此前依赖未锁定且本地 Python 版本不兼容；本轮已增加版本约束与自检 |
| Demo 稳定性 | 5/10 | 尚缺预录视频/截图和可离线复现的成功结果 |
| 面试讲述 | 8.5/10 | 本轮新增统一讲解手册，可从 30 秒讲到架构深挖 |

## 2. 关键欠缺

### P0：没有可引用的正式业务指标

此前 smoke eval 的 10 条 case 因缺少 `langgraph` 全部 crash。这证明失败产物链路有效，但不能作为 Agent 能力证明。面试官追问“效果提升多少”时，目前只能讲评测设计，不能给可信数字。

### P0：运行环境尚未闭环

项目依赖较重，Python 3.14 与部分 AI 依赖兼容性不足；当前目录也没有已构建的 SQLite 数据库和向量索引。README 虽然有构建步骤，但还缺一次完整成功运行记录。

### P0：缺少离线 Demo 保险

现场依赖模型 API 和 RAG 索引风险较高。应准备截图、短视频和一份成功响应 JSON，确保没有网络也能讲完整链路。

### P1：测试偏确定性业务模块，图/API 集成测试不足

现有测试主要覆盖 capabilities、披露评分、绿漂规则和 eval metrics。后续应 mock LLM/worker，验证关键路由：clarify、both_failed、re-plan 上限、Evaluator-O correction。

### P1：依赖版本还未完全锁定

本轮加入了 Python `>=3.10,<3.13` 约束和轻量 CI，但 `requirements.txt` 仍是未固定版本。正式展示前应在成功环境导出锁定版本。

### P1：数据版权与仓库体积

项目包含约 1.4GB PDF。作为公开作品集时，需要核查报告再分发权限；更稳妥的方式是只提交少量样例和下载/构建脚本，或使用 Git LFS，并在 data card 说明来源与许可。

### P2：绿漂识别仍是局部规则

当前只检查单个召回片段附近是否存在量化证据，可能漏掉跨段或跨页证据。后续要用标注集评估 precision/recall，再决定是否加入语义 evidence linker。

## 3. 本轮已经补齐

- 新增 `scripts/project_doctor.py`：零第三方依赖的运行前自检；
- 新增 `tests/test_project_doctor.py`；
- 新增 `.github/workflows/quality.yml`：Python 3.10/3.11/3.12 CI；
- 新增 `pyproject.toml`：明确项目元信息与 Python 版本范围；
- 新增 `requirements-dev.txt`：轻量确定性测试依赖；
- 扩展 Makefile：`doctor`、`doctor-source`、`quality`；
- 新增 `docs/interview-guide.md`：统一项目讲解、追问和简历表达。

## 4. 面试前验收线

只有满足以下条件，才建议在简历上写成“王牌项目”：

- [ ] `make doctor` 显示 runtime ready；
- [ ] `make quality` 全部通过；
- [ ] 10-case smoke eval 成功跑完并人工复核；
- [ ] 至少一组 baseline vs full system 对比；
- [ ] 有 3 分钟录屏和 3 张核心截图；
- [ ] 简历数字均能映射到 `outputs/eval_runs/{run_id}`；
- [ ] 数据来源与再分发边界写清楚。
