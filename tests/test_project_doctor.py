import json
import tempfile
import unittest
from pathlib import Path

from scripts.project_doctor import collect_readiness, render_markdown


class ProjectDoctorTests(unittest.TestCase):
    def _minimal_repo(self, root: Path) -> None:
        for relative in (
            "README.md", "agent/graph.py", "agent/state.py", "api/main.py",
            "static/index.html", "docs/interview-guide.md",
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("ok", encoding="utf-8")
        report = root / "data/car/示例公司2024年ESG报告.pdf"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_bytes(b"%PDF-test")
        dataset = root / "eval/datasets/esg_eval_smoke.jsonl"
        dataset.parent.mkdir(parents=True, exist_ok=True)
        dataset.write_text(json.dumps({"id": "S-1", "query": "测试"}, ensure_ascii=False) + "\n", encoding="utf-8")

    def test_source_profile_can_be_ready_without_third_party_dependencies(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._minimal_repo(root)
            report = collect_readiness(root, profile="source", python_version=(3, 11, 9), environ={})
            self.assertEqual(report["status"], "ready")
            self.assertEqual(report["summary"]["failed"], 0)

    def test_unsupported_python_is_blocking(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._minimal_repo(root)
            report = collect_readiness(root, profile="source", python_version=(3, 14, 0), environ={})
            self.assertEqual(report["status"], "blocked")
            self.assertIn("<3.13", render_markdown(report))


if __name__ == "__main__":
    unittest.main()
