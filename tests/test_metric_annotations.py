import json
import tempfile
import unittest
from pathlib import Path

from data.seed_structured_db import load_verified_annotations
from scripts.generate_gold_eval_dataset import build, load_annotations
from scripts.validate_metric_annotations import validate


class MetricAnnotationTests(unittest.TestCase):
    def test_annotation_layer_has_expected_coverage(self):
        mapping = load_verified_annotations()
        count = sum(len(metrics) for metrics in mapping.values())
        self.assertEqual(count, 56)
        self.assertAlmostEqual(mapping[("宁德时代", 2023)]["scope_1_emissions"]["value"], 765338.97)
        raw = mapping[("宁德时代", 2023)]["scope_1_emissions"]["raw"]
        self.assertEqual(raw["organizational_boundary"], "battery production bases")
        self.assertTrue(raw["needs_second_reviewer"])

    def test_gold_dataset_is_deterministic_and_fact_rich(self):
        data = load_annotations(Path("data/annotations/verified_metrics_v1.jsonl"))
        cases = build(data)
        self.assertEqual(len(cases), 30)
        self.assertEqual(sum(len(c.get("golden_facts", [])) for c in cases), 104)
        ids = [c["id"] for c in cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_validator_rejects_duplicate_metric_keys(self):
        source = Path("data/annotations/verified_metrics_v1.jsonl")
        first = json.loads(source.read_text(encoding="utf-8").splitlines()[0])
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "dupe.jsonl"
            path.write_text(json.dumps(first, ensure_ascii=False) + "\n" + json.dumps(first, ensure_ascii=False) + "\n", encoding="utf-8")
            report = validate(path, data_dir=Path("data"))
            self.assertEqual(report["status"], "fail")
            self.assertTrue(any(e["type"] == "duplicate_key" for e in report["errors"]))


if __name__ == "__main__":
    unittest.main()
