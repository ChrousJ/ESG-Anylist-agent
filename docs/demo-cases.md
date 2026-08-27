# ESG-Insight Agent Demo Cases

> 用途：面试或录屏时优先使用这些问题，避免现场随机提问导致数据覆盖不足或模型波动。  
> 对外项目名统一使用 **ESG-Insight Agent**。

---

## Demo 前准备

```bash
make test
make dev
```

打开：

- Chat UI: <http://127.0.0.1:8000/static/index.html>
- Dashboard: <http://127.0.0.1:8000/static/dashboard.html>
- Capabilities: <http://127.0.0.1:8000/api/capabilities>

---

## Case 1：单公司三年趋势

**问题**

```text
比亚迪2022到2024年碳排放趋势如何？
```

**展示点**

- Context 识别公司、年份、碳排放指标；
- SQL/RAG 并行获取结构化数字和 PDF 证据；
- Synthesizer 输出趋势分析；
- `disclosure_scorer` 给出披露质量评分；
- `greenwashing_detector` 给出潜在绿漂风险雷达。

**讲法**

> 这个 case 展示的是完整链路。普通 RAG 只会总结 PDF，我这里会同时查结构化库和原文，并在回答后附上披露质量评分和风险雷达。

---

## Case 2：银行业绿色金融指标

**问题**

```text
工商银行近三年绿色贷款余额变化趋势
```

**展示点**

- 银行业专属指标表；
- 多年份连续性评分；
- 绿色金融业务语义证据。

**讲法**

> 这个 case 说明系统不是只支持通用指标，还能根据行业进入不同子表，例如银行业的绿色贷款、普惠金融等。

---

## Case 3：横向对比 / Ranking

**问题**

```text
对比新能源行业所有公司2023年范围一碳排放
```

**展示点**

- 行业级横向对比；
- SQL ranking；
- 口径一致性和可比性提醒。

**讲法**

> ESG 横向对比最怕口径不一致，所以我在评分里单独设置了可比性维度，并在 Evaluator-D 里检查数据口径。

---

## Case 4：缺失 / 覆盖边界

**问题**

```text
蔚来2024年碳排放表现如何？
```

**展示点**

- 系统发现当前数据集未覆盖或证据不足；
- 输出 coverage gap / no-data response；
- 不编造数字。

**讲法**

> 可靠 Agent 的关键不是所有问题都硬答，而是知道什么时候不能答。这个 case 展示系统的安全边界。

---

## Case 5：潜在绿漂风险

**问题**

```text
华友钴业报告中有哪些缺少数据支撑的绿色承诺？
```

**展示点**

- RAG 召回绿色转型相关段落；
- `greenwashing_detector` 检测强表述弱证据；
- 输出“人工核查点”，而不是武断定性。

**讲法**

> 我把绿漂风险收窄成 claim-evidence mismatch，不判断企业真实绿漂，只提示报告中哪些强表述附近缺少量化证据。

---

## Case 6：知识问答快速出口

**问题**

```text
什么是范围一、范围二、范围三碳排放？
```

**展示点**

- Context 判断为 knowledge；
- 不走 SQL/RAG 完整流程；
- 直接回答，降低延迟和成本。

**讲法**

> 并不是所有问题都要走完整 Agent，知识类问题走快速出口，这也是成本控制的一部分。

---

## 每个 Demo Case 的通用观察点

- Chat UI 是否展示节点进度；
- Dashboard 是否能看到 trace；
- 最终回答是否明确证据来源或覆盖限制；
- 是否输出 `disclosure_quality`；
- 是否输出 `greenwashing_risks`；
- 遇到未覆盖问题时是否降级而非编造。

## 翻车预案

| 现场问题 | 处理方式 |
|---|---|
| 模型 API 超时 | 打开 `docs/demo-script.md` 和 `docs/architecture.md`，讲离线架构与测试结果 |
| 某问题没有数据 | 打开 `/api/capabilities` 解释数据边界 |
| 前端展示异常 | 查看 `/api/chat` 或 SSE 最终 JSON，说明后端 state 已输出 |
| 绿漂风险为空 | 强调不是所有公司都应有风险，系统不会为了展示而制造风险 |

## 推荐演示顺序

1. 打开 README，讲项目定位；
2. 打开 `/api/capabilities`，讲覆盖边界；
3. 跑 Case 1，展示完整链路；
4. 跑 Case 4，展示不胡编；
5. 打开 dashboard，讲可观测性；
6. 打开 `docs/interview-script.md`，讲迭代思路。
