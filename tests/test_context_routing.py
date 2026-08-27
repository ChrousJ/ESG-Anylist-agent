import os
import unittest

from agent.nodes.context import context_node


class ContextRoutingTests(unittest.TestCase):
    def test_incomplete_business_query_becomes_clarify(self):
        old = os.environ.get("OFFLINE_DETERMINISTIC_MODE")
        os.environ["OFFLINE_DETERMINISTIC_MODE"] = "true"
        try:
            result = context_node({"trace_id": "t", "user_query": "对比一下碳排放", "history": []})
        finally:
            if old is None:
                os.environ.pop("OFFLINE_DETERMINISTIC_MODE", None)
            else:
                os.environ["OFFLINE_DETERMINISTIC_MODE"] = old
        self.assertEqual(result["query_class"], "clarify")
        self.assertTrue(result["need_clarify"])

    def test_scope_definition_is_knowledge(self):
        old = os.environ.get("OFFLINE_DETERMINISTIC_MODE")
        os.environ["OFFLINE_DETERMINISTIC_MODE"] = "true"
        try:
            result = context_node({"trace_id": "t", "user_query": "范围一范围二范围三碳排放分别是什么？", "history": []})
        finally:
            if old is None:
                os.environ.pop("OFFLINE_DETERMINISTIC_MODE", None)
            else:
                os.environ["OFFLINE_DETERMINISTIC_MODE"] = old
        self.assertEqual(result["query_class"], "knowledge")
        self.assertFalse(result["need_clarify"])


if __name__ == "__main__":
    unittest.main()
