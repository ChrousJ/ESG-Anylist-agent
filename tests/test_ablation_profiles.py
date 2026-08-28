import unittest

from agent.graph import ABLATION_PROFILES, evaluator_d_bypass_node, evaluator_o_bypass_node


class AblationProfileTests(unittest.TestCase):
    def test_profile_set_is_explicit(self):
        self.assertEqual(ABLATION_PROFILES, {"full", "no_evaluators", "eval_d_only", "eval_o_only"})

    def test_evaluator_d_bypass_has_no_replan_signal(self):
        out = evaluator_d_bypass_node({"trace_id": "t", "node_trace": []})
        self.assertEqual(out["eval_d_status"], "pass")
        self.assertEqual(out["eval_d_errors"], [])

    def test_evaluator_o_bypass_has_no_correction_signal(self):
        out = evaluator_o_bypass_node({"trace_id": "t", "node_trace": [], "eval_o_retry_count": 2})
        self.assertEqual(out["eval_o_status"], "pass")
        self.assertEqual(out["eval_o_retry_count"], 0)

    def test_grounded_future_year_is_not_entity_hallucination(self):
        from agent.nodes.evaluator_o import _check_entity_hallucination
        state = {
            "entities": {"years": [2024]},
            "analysis": "证据摘录提到项目入围2025年度科技计划。",
            "rag_result": {"chunks": [{"text": "项目入围2025年度科技计划"}]},
        }
        self.assertEqual(_check_entity_hallucination(state), [])

    def test_scope_annotation_accepts_scope_emission_wording(self):
        from agent.nodes.evaluator_o import _check_scope_annotation
        state = {
            "analysis": "范围一排放口径说明：两家公司组织边界不同，不可直接比较。范围二排放口径说明：统计范围不同。",
            "scope_consistency": {
                "checked": True,
                "per_metric": {
                    "scope_1_emissions": {"action": "flagged"},
                    "scope_2_emissions": {"action": "flagged"},
                },
            },
        }
        self.assertEqual(_check_scope_annotation(state), [])


if __name__ == "__main__":
    unittest.main()
