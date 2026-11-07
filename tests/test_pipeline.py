"""端到端测试：检索、生成、校验、执行、评测。不依赖任何外部服务。"""
from __future__ import annotations

import unittest

from app.agent import METRIC_SQL, build_context, execute, rule_based_sql, validate
from app.catalog import load_catalog
from app.evals import normalize, run as run_eval
from app.retrieval import SchemaRetriever, rrf, tokenize


class RetrievalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()
        cls.retriever = SchemaRetriever(cls.catalog)

    def test_tokenize_keeps_latin_and_cjk(self) -> None:
        tokens = tokenize("各地区的GMV(gmv)")
        self.assertIn("gmv", tokens)
        self.assertTrue(any(len(t) == 2 for t in tokens))

    def test_metric_hit_for_synonym(self) -> None:
        metrics, _, _ = self.retriever.retrieve("销售额是多少")
        self.assertEqual(metrics[0].key, "gmv")

    def test_metric_hit_for_refund(self) -> None:
        metrics, _, _ = self.retriever.retrieve("退款率")
        self.assertEqual(metrics[0].key, "refund_rate")

    def test_table_hit_for_region(self) -> None:
        _, tables, _ = self.retriever.retrieve("各地区的成交额")
        self.assertIn("regions", [t.key for t in tables])

    def test_column_hit_for_order_date(self) -> None:
        _, _, columns = self.retriever.retrieve("按月看下单时间")
        self.assertIn("order_date", [c.key for c in columns])

    def test_rrf_prefers_both_lists(self) -> None:
        from app.domain import Hit
        a = Hit("table", "orders", 0.9, {})
        b = Hit("table", "products", 0.5, {})
        fused = rrf([[a, b], [a]], top_k=2)
        self.assertEqual(fused[0].key, "orders")


class GenerationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()
        cls.retriever = SchemaRetriever(cls.catalog)

    def _sql(self, question: str) -> str:
        ctx, kw = build_context(question, self.catalog, self.retriever)
        return rule_based_sql(question, self.catalog, ctx, kw)

    def test_gmv_total(self) -> None:
        sql = self._sql("总GMV是多少")
        self.assertIn(METRIC_SQL["gmv"], sql)
        self.assertIn("status = 'paid'", sql)
        self.assertNotIn("GROUP BY", sql)

    def test_group_by_region(self) -> None:
        sql = self._sql("各地区的GMV")
        self.assertIn("GROUP BY r.region_name", sql)

    def test_filter_only_does_not_group(self) -> None:
        sql = self._sql("华东地区的GMV")
        self.assertIn("r.region_name = '华东'", sql)
        self.assertNotIn("GROUP BY", sql)

    def test_rank_limits_to_ten(self) -> None:
        sql = self._sql("各商品销量排名")
        self.assertIn("LIMIT 10", sql)


class ValidatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()

    def test_reject_non_select(self) -> None:
        ok, msg = validate("DELETE FROM orders", self.catalog)
        self.assertFalse(ok)
        self.assertIn("SELECT", msg)

    def test_reject_multiple_statements(self) -> None:
        ok, _ = validate("SELECT 1 FROM orders; SELECT 2 FROM orders", self.catalog)
        self.assertFalse(ok)

    def test_reject_unknown_table(self) -> None:
        ok, msg = validate("SELECT * FROM secret_table", self.catalog)
        self.assertFalse(ok)
        self.assertIn("secret_table", msg)

    def test_accept_plain_select(self) -> None:
        ok, _ = validate("SELECT order_id FROM orders LIMIT 5", self.catalog)
        self.assertTrue(ok)


class ExecutionTest(unittest.TestCase):
    def test_execute_returns_rows(self) -> None:
        cols, rows = execute("SELECT COUNT(*) FROM orders")
        self.assertEqual(len(cols), 1)
        self.assertGreater(rows[0][0], 0)

    def test_readonly_connection(self) -> None:
        with self.assertRaises(Exception):
            execute("INSERT INTO regions(region_id, region_name) VALUES (99, 'x')")


class EvalTest(unittest.TestCase):
    def test_normalize_tolerates_float_precision(self) -> None:
        self.assertEqual(normalize([(1.0,)]), normalize([(1.004,)]))

    def test_eval_rate_above_threshold(self) -> None:
        summary = run_eval(verbose=False)
        self.assertGreaterEqual(summary["exec_rate"], 0.9, summary)


if __name__ == "__main__":
    unittest.main()
