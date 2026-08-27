import unittest

from agent.graph import no_data_response_node


class NoDataSafetyTests(unittest.TestCase):
    def test_terminal_no_data_clears_retrieval_evidence(self):
        state = {
            "trace_id": "t",
            "terminal_response_mode": "coverage_gap",
            "terminal_response_reason": "未覆盖",
            "entities": {"companies": ["特斯拉"], "years": [2023], "metrics": ["scope_1_emissions"]},
            "sources": [{"type": "rag", "company": "宁德时代", "year": 2023}],
            "sql_result": [{"company_name": "宁德时代", "year": 2023}],
            "sql_query_executed": "SELECT ...",
            "rag_result": {"chunks": [{"company_name": "宁德时代", "year": 2023}]},
        }
        result = no_data_response_node(state)
        self.assertEqual(result["sources"], [])
        self.assertIsNone(result["sql_result"])
        self.assertEqual(result["sql_query_executed"], "")
        self.assertEqual(result["rag_result"]["chunks"], [])
        self.assertIn("未覆盖", result["analysis"])


if __name__ == "__main__":
    unittest.main()
