# Iteration 06 — 秋招项目可复现性与讲述闭环强化

> 日期：2026-08-12

## 目标

把项目从“功能较多的 Agent Demo”进一步升级为面试官能够审查、开发者能够自检、候选人能够稳定讲解的作品集项目。

## 代码与工程改动

- 新增 `scripts/project_doctor.py`，仅依赖标准库，检查 Python 版本、目录结构、PDF 语料、smoke dataset、运行依赖、SQLite、向量库与 LLM 配置；
- 新增 `tests/test_project_doctor.py`，验证 source profile 和版本阻断逻辑；
- 新增 `.github/workflows/quality.yml`，在 Python 3.10/3.11/3.12 上运行编译、单测与 source readiness；
- 新增 `pyproject.toml`，明确支持 Python `>=3.10,<3.13`；
- 新增 `requirements-dev.txt`，把轻量 CI 与完整运行依赖分离；
- 扩展 Makefile：支持 `PYTHON=...`、`doctor`、`doctor-source`、`quality`；
- 将 API 和 capabilities 对外名称统一为 `ESG-Insight Agent`；
- 修复 Pydantic 容器字段默认值写法，并使用 Pydantic v2 `model_dump()`。

## 文档改动

- 新增 `docs/interview-guide.md`：秋招主讲文档；
- 新增 `docs/project-audit-20260812.md`：项目客观审查与优先级；
- 新增 `docs/readiness-report.md`：当前机器真实运行准备状态；
- 更新 README 和 docs index，统一入口。

## 本轮验证

```text
python3 -m unittest discover -s tests
Ran 8 tests
OK

python3 -m compileall agent api scripts tests
通过
```

当前 runtime doctor 真实结论为 blocked，具体原因记录在 `docs/readiness-report.md`。这不是隐藏的问题，而是下一轮应完成的 P0：使用 Python 3.10/3.11、安装依赖、构建 SQLite/向量索引并配置测试 key。
