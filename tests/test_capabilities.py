import unittest

from agent.capabilities import get_capabilities


class CapabilitiesTest(unittest.TestCase):
    def test_capabilities_shape(self):
        data = get_capabilities()
        self.assertIn("coverage", data)
        self.assertIn("agent_capabilities", data)
        self.assertGreaterEqual(data["coverage"]["reports"]["report_count"], 1)
        self.assertGreaterEqual(data["coverage"]["metric_dictionary"]["table_count"], 1)


if __name__ == "__main__":
    unittest.main()
