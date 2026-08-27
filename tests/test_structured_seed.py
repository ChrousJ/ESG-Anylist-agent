import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from data.seed_structured_db import build_database
from agent.nodes.sql_worker import _generate_sql_deterministic


class StructuredSeedTest(unittest.TestCase):
    def test_builds_coverage_and_verified_values(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "seed.db"
            summary = build_database(db, Path("data"), reset=True)
            self.assertEqual(summary["report_rows"], 90)
            self.assertEqual(summary["companies"], 30)
            self.assertEqual(summary["universal_rows"], 90)
            conn = sqlite3.connect(db)
            row = conn.execute("SELECT scope_1_emissions, scope_2_emissions, raw_scope_1, data_quality FROM esg_universal_metrics WHERE company_name='比亚迪' AND year=2024").fetchone()
            self.assertAlmostEqual(row[0], 1539251.46)
            self.assertAlmostEqual(row[1], 8562574.74)
            self.assertEqual(json.loads(row[2])["page"], "127")
            self.assertEqual(json.loads(row[3])["scope_1_emissions"], "verified")
            gwm = conn.execute("SELECT scope_1_emissions, scope_2_emissions FROM esg_universal_metrics WHERE company_name='长城汽车' AND year=2024").fetchone()
            self.assertEqual(gwm, (152033.5, 997331.85))
            icbc = conn.execute("SELECT green_finance_balance, data_quality FROM esg_banking_metrics WHERE company_name='工商银行' AND year=2024").fetchone()
            self.assertEqual(icbc[0], 60000.0)
            self.assertEqual(json.loads(icbc[1])["green_finance_balance"], "reported_lower_bound")
            conn.close()

    def test_deterministic_sql_supports_industry_wide_query(self):
        sql = _generate_sql_deterministic({
            "companies": [], "years": [2023], "metrics": ["scope_1_emissions"],
            "industry": "new_energy",
        })
        self.assertIn("u.industry = 'new_energy'", sql)
        self.assertNotIn("company_name IN ()", sql)


if __name__ == "__main__":
    unittest.main()
