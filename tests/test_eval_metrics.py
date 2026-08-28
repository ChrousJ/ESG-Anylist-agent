import unittest

from scripts.run_evaluation import (
    _annotate_expected_behavior, _compute_metrics, _generate_report,
    _packaged_numeric_support, _score_golden_facts,
)


class EvalMetricsTest(unittest.TestCase):
    def test_business_signal_metrics(self):
        results = [
            {
                "status": "success",
                "latency_ms": 100,
                "rescued": False,
                "tool_total_count": 1,
                "tool_error_rate": 0.0,
                "step_count": 4,
                "has_any_evidence": True,
                "has_sql_evidence": True,
                "has_rag_evidence": False,
                "has_disclosure_quality": True,
                "disclosure_quality_score": 80,
                "has_greenwashing_radar": True,
                "greenwashing_risk_count": 2,
            },
            {
                "status": "crashed",
                "latency_ms": 200,
                "rescued": False,
                "tool_total_count": 0,
                "step_count": 0,
                "has_any_evidence": False,
                "has_sql_evidence": False,
                "has_rag_evidence": False,
                "has_disclosure_quality": False,
                "disclosure_quality_score": None,
                "has_greenwashing_radar": False,
                "greenwashing_risk_count": 0,
            },
        ]

        metrics = _compute_metrics(results)

        self.assertEqual(metrics["completion_rate"], 50.0)
        self.assertEqual(metrics["evidence_presence_rate"], 50.0)
        self.assertEqual(metrics["disclosure_score_presence_rate"], 50.0)
        self.assertEqual(metrics["avg_disclosure_score"], 80)
        self.assertEqual(metrics["greenwashing_radar_presence_rate"], 50.0)
        self.assertEqual(metrics["greenwashing_nonzero_count"], 1)

    def test_report_marks_skipped_baseline_and_judge_as_na(self):
        lg_results = [{
            "case_id": "CASE-1", "status": "success", "is_degraded": False,
            "rescued": False, "node_trace_summary": [], "has_any_evidence": False,
            "has_disclosure_quality": False, "disclosure_quality_score": None,
            "has_greenwashing_radar": False, "greenwashing_risk_count": 0,
        }]
        lg_metrics = {
            "total": 1, "completed": 1, "completion_rate": 100.0,
            "rescued": 0, "rescue_rate": 0.0, "crashed": 0, "degraded": 0,
            "p95_latency_ms": 10, "p50_latency_ms": 10, "avg_latency_ms": 10,
            "evidence_presence_rate": 0.0, "evidence_presence_count": 0,
        }
        bl_metrics = {"total": 0, "completed": 0, "completion_rate": 0, "crashed": 0}
        report = _generate_report(
            lg_results, [], lg_metrics, bl_metrics, {}, {},
            [{"id": "CASE-1", "category": "knowledge", "query": "test"}],
            "2026-08-13T00:00:00+00:00", judge_enabled=False,
        )
        self.assertIn("Baseline: skipped for this run", report)
        self.assertIn("Judge: disabled for this run", report)
        self.assertIn("ReAct baseline was not run", report)
        self.assertNotIn("vs ReAct 0%", report)
        self.assertIn("| Judge Overall | N/A | N/A |", report)
        self.assertIn("| Crashed | 0 | N/A |", report)

    def test_expected_behavior_and_conditional_metrics(self):
        case = {
            "id": "C1", "category": "trend", "expected_class": "complex",
            "expected_evidence": True,
            "expected_entities": {"companies": ["比亚迪"], "years": [2024]},
        }
        result = {
            "status": "success", "analysis_full": "2024年范围一为1,539,251.46吨。",
            "query_class": "complex", "has_any_evidence": True,
            "sql_result_preview": {"rows_preview": [{"company_name": "比亚迪", "year": 2024, "scope_1_emissions": 1539251.46}]},
            "sources_preview": [],
            "judge_evidence": {"sql": {"result": {"rows_preview": [{"scope_1_emissions": 1539251.46}]}}},
            "latency_ms": 10, "rescued": False, "tool_total_count": 1, "tool_error_rate": 0,
            "step_count": 1, "has_sql_evidence": True, "has_rag_evidence": False,
            "has_disclosure_quality": False, "has_greenwashing_radar": False,
        }
        annotated = _annotate_expected_behavior(result, case)
        self.assertTrue(annotated["expected_class_match"])
        self.assertEqual(annotated["target_coverage"]["rate"], 100.0)
        self.assertEqual(annotated["numeric_support"]["support_rate"], 100.0)
        metrics = _compute_metrics([annotated])
        self.assertEqual(metrics["strict_success_rate"], 100.0)
        self.assertEqual(metrics["evidence_required_coverage_rate"], 100.0)

    def test_packaged_numeric_support_detects_unsupported_number(self):
        score = _packaged_numeric_support("排放量为123吨", {"source": "排放量为100吨"})
        self.assertEqual(score["support_rate"], 0.0)

    def test_golden_fact_score(self):
        result = {"sql_result_preview": {"rows_preview": [{"company_name": "比亚迪", "year": 2024, "scope_1_emissions": 1539251.46}]}}
        case = {"golden_facts": [{"company": "比亚迪", "year": 2024, "metric": "scope_1_emissions", "value": 1539251.46}]}
        score = _score_golden_facts(result, case)
        self.assertEqual(score["accuracy"], 100.0)

    def test_case_pass_rejects_no_data_evidence_leak(self):
        case = {"category": "missing_degradation", "expected_class": "complex", "expected_evidence": False, "expected_entities": {"companies": ["特斯拉"], "years": [2023]}}
        result = {"status": "success", "analysis_preview": "特斯拉不在当前覆盖范围内", "query_class": "", "has_any_evidence": True, "sources_preview": [{"company": "宁德时代", "year": 2023}], "sql_result_preview": {}, "judge_evidence": {}}
        annotated = _annotate_expected_behavior(result, case)
        self.assertFalse(annotated["no_data_safe"])
        self.assertFalse(annotated["case_pass"])

    def test_knowledge_case_in_clarify_bucket_passes_with_knowledge_answer(self):
        case = {"category": "clarify", "expected_class": "knowledge", "expected_evidence": False, "expected_entities": {}}
        result = {"status": "success", "analysis_preview": "ESG 是环境、社会和治理。", "query_class": "knowledge", "has_any_evidence": False, "sources_preview": [], "sql_result_preview": {}, "judge_evidence": {}}
        annotated = _annotate_expected_behavior(result, case)
        self.assertTrue(annotated["clarify_success"])
        self.assertTrue(annotated["case_pass"])

    def test_baseline_style_clarification_is_recognized(self):
        case = {"category": "clarify", "expected_class": "clarify", "expected_evidence": False, "expected_entities": {}}
        result = {"status": "success", "analysis_preview": "请问您想分析哪家公司或哪个ESG指标？", "query_class": "", "has_any_evidence": False, "sources_preview": [], "sql_result_preview": {}, "judge_evidence": {}}
        annotated = _annotate_expected_behavior(result, case)
        self.assertTrue(annotated["clarify_success"])
        self.assertTrue(annotated["case_pass"])

    def test_partial_missing_accepts_non_contiguous_disclosure_wording(self):
        case = {
            "id": "M", "category": "partial_missing", "expected_class": "complex",
            "expected_evidence": True,
            "expected_entities": {"companies": ["华友钴业"], "years": [2022], "metrics": ["scope_1_emissions", "scope_2_emissions"]},
        }
        result = {
            "status": "success", "query_class": "complex", "has_any_evidence": True,
            "analysis_full": "范围一和范围二的单独排放量数据未在证据中披露，仅提供合并总量。",
            "sql_result_preview": {},
            "sources_preview": [{"company": "华友钴业", "year": 2022}],
            "judge_evidence": {},
        }
        scored = _annotate_expected_behavior(result, case)
        self.assertTrue(scored["partial_missing_safe"])
        self.assertTrue(scored["case_pass"])


if __name__ == "__main__":
    unittest.main()
