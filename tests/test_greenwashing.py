import unittest

from agent.greenwashing import detect_greenwashing_risks, render_greenwashing_markdown


class GreenwashingTest(unittest.TestCase):
    def test_flags_strong_claim_without_evidence(self):
        state = {
            "rag_result": {
                "chunks": [
                    {
                        "text": "公司高度重视绿色低碳发展，全面推进绿色转型，积极履行社会责任。",
                        "company_name": "示例公司",
                        "year": 2024,
                        "page_num": 18,
                    }
                ]
            }
        }
        result = detect_greenwashing_risks(state)
        self.assertEqual(result["risk_count"], 1)
        self.assertEqual(result["risks"][0]["evidence_status"], "missing")
        self.assertIn("潜在绿漂风险雷达", render_greenwashing_markdown(result))

    def test_does_not_flag_claim_with_quant_evidence(self):
        state = {
            "rag_result": {
                "chunks": [
                    {"text": "公司推进绿色低碳发展，2024年碳排放下降12.5%，通过ISO认证。"}
                ]
            }
        }
        result = detect_greenwashing_risks(state)
        self.assertEqual(result["risk_count"], 0)


if __name__ == "__main__":
    unittest.main()
