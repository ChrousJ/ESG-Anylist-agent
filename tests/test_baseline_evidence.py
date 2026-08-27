import unittest

from scripts.run_evaluation import _parse_baseline_tool_evidence


class BaselineEvidenceTest(unittest.TestCase):
    def test_parses_sql_and_rag_tool_observations(self):
        sql, sources = _parse_baseline_tool_evidence([
            {"tool_name": "query_esg_database", "content": '[{"company_name":"比亚迪","year":2024,"scope_1_emissions":1539251.46}]'},
            {"tool_name": "search_esg_reports", "content": "[1] 比亚迪 2024 p.127 (score=0.98): 范围一排放1,539,251.46吨"},
        ])
        self.assertEqual(sql["row_count"], 1)
        self.assertEqual(sources[0]["company"], "比亚迪")
        self.assertEqual(sources[0]["year"], "2024")


if __name__ == "__main__":
    unittest.main()
