import unittest

from agent.nodes.evaluator_d import _check_rag_target_coverage
from agent.nodes.rag_worker import _select_with_target_coverage


class EvidenceCoverageTest(unittest.TestCase):
    def test_reports_missing_company_year(self):
        state = {
            "entities": {"companies": ["比亚迪", "长城汽车"], "years": [2024]},
            "rag_result": {"chunks": [{"company_name": "比亚迪", "year": "2024"}]},
        }
        errors = _check_rag_target_coverage(state)
        self.assertEqual(errors[0]["type"], "RAG_TARGET_COVERAGE_MISSING")
        self.assertEqual(errors[0]["missing_targets"], [["长城汽车", 2024]])

    def test_coverage_aware_selection_keeps_each_target(self):
        chunks = [
            {"chunk_id": "a1", "company_name": "比亚迪", "year": "2024", "rerank_score": 0.99},
            {"chunk_id": "a2", "company_name": "比亚迪", "year": "2024", "rerank_score": 0.98},
            {"chunk_id": "b1", "company_name": "长城汽车", "year": "2024", "rerank_score": 0.70},
        ]
        selected = _select_with_target_coverage(chunks, [("比亚迪", 2024), ("长城汽车", 2024)], 2)
        self.assertEqual({c["company_name"] for c in selected}, {"比亚迪", "长城汽车"})


if __name__ == "__main__":
    unittest.main()

class IndustryWideTest(unittest.TestCase):
    def test_context_expands_industry_all_companies(self):
        import os
        from agent.nodes.context import context_node, _load_supported_company_industries
        _load_supported_company_industries.cache_clear()
        old = os.environ.get("OFFLINE_DETERMINISTIC_MODE")
        os.environ["OFFLINE_DETERMINISTIC_MODE"] = "true"
        try:
            out = context_node({"trace_id": "t", "user_query": "对比新能源行业所有公司2023年范围一碳排放", "history": []})
        finally:
            if old is None:
                os.environ.pop("OFFLINE_DETERMINISTIC_MODE", None)
            else:
                os.environ["OFFLINE_DETERMINISTIC_MODE"] = old
        self.assertTrue(out["entities"]["industry_wide"])
        self.assertEqual(len(out["entities"]["companies"]), 10)
        self.assertEqual(out["entities"]["compare_dimension"], "horizontal")


class CompositeMetricTest(unittest.TestCase):

    def test_disclosure_quality_expands_scope_one_and_two(self):
        from agent.nodes.context import _extract_metrics
        self.assertEqual(
            _extract_metrics("对比比亚迪和长城汽车2024年碳排放披露质量"),
            ["scope_1_emissions", "scope_2_emissions"],
        )
