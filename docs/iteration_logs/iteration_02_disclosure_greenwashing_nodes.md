# Iteration 02：披露评分节点化与绿漂风险雷达

日期：2026-08-08

## 本轮目标

把上轮已经实现的披露质量评分从 `synthesizer` 内部逻辑升级为显式 LangGraph 节点，并新增一个规则型绿漂风险识别节点。这样项目的业务亮点可以在 trace、前端 pipeline 和面试讲述中被直接看见。

## 代码改动

### 1. 新增独立披露质量节点

新增：

```text
agent/nodes/disclosure_scorer.py
```

职责：

- 调用 `agent/disclosure_quality.py`；
- 计算 0-100 披露质量评分；
- 将评分 Markdown 追加到最终报告；
- 将 `disclosure_quality` 写入 AgentState；
- 将评分摘要写入 `key_findings`。

架构变化：

```text
Before:
synthesizer → evaluator_o

After:
synthesizer → disclosure_scorer → greenwashing_detector → evaluator_o
```

### 2. 新增绿漂风险识别模块

新增：

```text
agent/greenwashing.py
agent/nodes/greenwashing_detector.py
```

设计边界：

- 不判断公司是否真实绿漂；
- 只识别 ESG 报告中的 claim-evidence mismatch；
- 即“强 ESG 表述”附近缺少数字、同比、年度进展或第三方鉴证。

输出写入：

```python
greenwashing_risks = {
    "risk_count": int,
    "risks": [...],
    "summary": str,
    "method": str,
}
```

### 3. API 和前端

修改：

```text
api/main.py
static/app.js
static/style.css
static/dashboard.html
```

新增响应字段：

```json
"greenwashing_risks": {...}
```

前端新增：

- `disclosure_scorer` 节点；
- `greenwashing_detector` 节点；
- 潜在绿漂风险雷达卡片；
- dashboard 节点排序。

### 4. 文档

新增：

```text
docs/interview-script.md
docs/demo-cases.md
```

用于后续面试复习和现场演示。

### 5. 测试

新增：

```text
tests/test_greenwashing.py
```

覆盖：

- 强 ESG 表述但无证据 → 触发风险；
- 强 ESG 表述且有量化证据 → 不触发风险。

## 面试讲法

> 我把披露质量评分和绿漂风险识别从“报告生成后的附属逻辑”升级成了显式 Agent 节点。这样每个业务判断都能在 trace 中看到，前端也能显示节点进度。披露评分是确定性 Rubric，绿漂风险是规则型 claim-evidence mismatch，二者都强调可解释和可复现。

## 为什么第一版绿漂识别用规则而不是 LLM

1. 可解释：每个风险都有明确触发条件；
2. 稳定：不会因为模型波动导致同一片段今天判风险、明天不判；
3. 成本低：不增加额外 LLM 调用；
4. 适合迭代：后续可以用 LLM judge 做语义增强，但规则层仍可作为 baseline。

## 验证命令

```bash
python3 -m compileall agent api scripts tests
python3 -m unittest discover -s tests
```
