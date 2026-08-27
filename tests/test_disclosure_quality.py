import unittest

from agent.disclosure_quality import score_disclosure_quality, render_disclosure_quality_markdown


class DisclosureQualityTest(unittest.TestCase):
    def test_high_quality_multiyear_score(self):
        rows = [
            {"company_name": "比亚迪", "year": 2022, "scope_1_emissions": 10.0, "data_quality": '{"scope_1_emissions":"normal"}', "confidence_scores": '{"scope_1_emissions":0.95}'},
            {"company_name": "比亚迪", "year": 2023, "scope_1_emissions": 11.0, "data_quality": '{"scope_1_emissions":"normal"}', "confidence_scores": '{"scope_1_emissions":0.95}'},
            {"company_name": "比亚迪", "year": 2024, "scope_1_emissions": 9.0, "data_quality": '{"scope_1_emissions":"normal"}', "confidence_scores": '{"scope_1_emissions":0.95}'},
        ]
        state = {
            "entities": {"metrics": ["scope_1_emissions"], "years": [2022, 2023, 2024]},
            "sql_result": rows,
            "sql_query_executed": "SELECT ...",
            "sources": [{"type": "rag", "page": "12", "excerpt": "范围一排放 10 吨"}],
            "rag_result": {"chunks": [{"text": "范围一排放 10 吨", "page_num": 12}]},
            "scope_consistency": {"checked": True, "consistent": True},
        }
        result = score_disclosure_quality(state)
        self.assertGreaterEqual(result["score"], 80)
        self.assertIn(result["band"], {"A", "B"})
        self.assertIn("披露质量评分", render_disclosure_quality_markdown(result))

    def test_missing_data_creates_risk_flags(self):
        rows = [
            {"company_name": "比亚迪", "year": 2022, "scope_1_emissions": None},
            {"company_name": "比亚迪", "year": 2023, "scope_1_emissions": None},
        ]
        result = score_disclosure_quality({
            "entities": {"metrics": ["scope_1_emissions"], "years": [2022, 2023]},
            "sql_result": rows,
            "sources": [],
            "rag_result": {"chunks": []},
        })
        self.assertLess(result["score"], 55)
        self.assertTrue(any(r["type"] == "incomplete_disclosure" for r in result["risk_flags"]))


if __name__ == "__main__":
    unittest.main()
