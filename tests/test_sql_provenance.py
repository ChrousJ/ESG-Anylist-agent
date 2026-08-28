import unittest
import pandas as pd

from agent.nodes.sql_worker import _build_sql_provenance_sources


class SqlProvenanceTests(unittest.TestCase):
    def test_recovers_restated_report_page(self):
        df = pd.DataFrame([{
            "company_name": "宁德时代", "year": 2023,
            "scope_1_emissions": 765338.97, "scope_2_emissions": 1477835.08,
        }])
        sources = _build_sql_provenance_sources(df, {"metrics": ["scope_1_emissions", "scope_2_emissions"]})
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["file"], "宁德时代2024年ESG报告.pdf")
        self.assertEqual(sources[0]["page"], "121")
        self.assertEqual(set(sources[0]["metrics"]), {"scope_1_emissions", "scope_2_emissions"})
        self.assertEqual(sources[0]["organizational_boundary"], "battery production bases")


if __name__ == "__main__":
    unittest.main()
