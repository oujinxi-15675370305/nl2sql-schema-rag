"""端到端测试：检索、生成、校验、执行、评测。不依赖任何外部服务。"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from app.agent import (
    METRIC_SQL,
    QueryTimeout,
    build_context,
    execute,
    llm_sql,
    rule_based_sql,
    validate,
)
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

    def test_customer_count_joins_customers(self) -> None:
        # M4 前置：客户数走 customers 表，避免用订单数冒充客户数
        sql = self._sql("各地区的客户数")
        self.assertIn("COUNT(DISTINCT c.customer_id)", sql)
        self.assertIn("JOIN customers c", sql)

    def test_refund_rate_keeps_four_decimals(self) -> None:
        # 口径是 4 位小数（catalog / 期望 SQL 都是 4 位），外层 ROUND 不能压回 2 位：
        # 0.125 被二次舍入成 0.13，评测就会跟期望差一位（AC-5.1 扩用例时踩到）
        sql = self._sql("各品类的退款率")
        self.assertIn(", 4), 4)", sql)
        self.assertNotIn(", 4), 2)", sql)

    def test_sensitive_column_only_when_asked(self) -> None:
        # M4：只有明确点名才选敏感列；普通"各客户的GMV"不能顺手把手机号带出来
        self.assertNotIn("customer_phone", self._sql("各客户的GMV"))
        self.assertIn("customer_phone", self._sql("各客户的GMV和手机号"))


class PromptContextTest(unittest.TestCase):
    """给 LLM 的上下文：JOIN 键、表关系、指标依赖的列、口径规则都不能少。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()
        cls.retriever = SchemaRetriever(cls.catalog)

    def test_ddl_includes_join_keys_and_relations(self) -> None:
        ctx, _ = build_context("各地区的销量", self.catalog, self.retriever)
        ddl = ctx.ddl(self.catalog)
        self.assertIn("-- 表关系", ddl)
        self.assertIn("orders.region_id = regions.region_id", ddl)
        self.assertIn("order_items.order_id = orders.order_id", ddl)
        # 即使 region_id / order_id 没被列检索命中，也要出现在 DDL 里，否则模型会瞎编 JOIN
        self.assertIn("region_id", ddl)
        self.assertIn("order_id", ddl)

    def test_ddl_includes_metric_required_columns(self) -> None:
        ctx, _ = build_context("各品牌的客单价", self.catalog, self.retriever)
        ddl = ctx.ddl(self.catalog)
        self.assertIn("pay_amount", ddl)  # aov 的 requires 列
        self.assertIn("brand", ddl)

    def test_llm_prompt_carries_rules_relations_and_strips_fences(self) -> None:
        ctx, _ = build_context("各地区的销量", self.catalog, self.retriever)
        payload = {"choices": [{"message": {"content": "```sql\nSELECT 1\n```"}}]}

        class FakeResponse:
            def read(self) -> bytes:
                return json.dumps(payload).encode()

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> bool:
                return False

        with mock.patch.dict(os.environ, {"LLM_API_KEY": "test-key"}), \
                mock.patch("urllib.request.urlopen", return_value=FakeResponse()) as fake:
            sql = llm_sql("各地区的销量", ctx, self.catalog)
            body = json.loads(fake.call_args[0][0].data.decode())

        prompt = body["messages"][0]["content"]
        self.assertEqual(sql, "SELECT 1")
        self.assertIn("表关系", prompt)
        self.assertIn("o.status = 'paid'", prompt)
        self.assertIn("LIMIT 10", prompt)
        self.assertIn("各地区的销量", prompt)


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

    def test_query_timeout_interrupts_runaway_sql(self) -> None:
        # AC-1.6 反向用例：笛卡尔积跑不完，必须在预算内被中断，不能把线程挂住
        with self.assertRaises(QueryTimeout):
            execute("SELECT COUNT(*) FROM orders a, orders b, orders c, orders d", timeout_s=0.2)

    def test_zero_timeout_disables_the_guard(self) -> None:
        # AC-1.6：timeout_s=0 表示不限时，正常查询照常返回
        cols, rows = execute("SELECT COUNT(*) FROM orders", timeout_s=0)
        self.assertEqual(len(cols), 1)
        self.assertGreater(rows[0][0], 0)


class EvalTest(unittest.TestCase):
    def test_normalize_tolerates_float_precision(self) -> None:
        self.assertEqual(normalize([(1.0,)]), normalize([(1.004,)]))

    def test_eval_rate_above_threshold(self) -> None:
        summary = run_eval(verbose=False)
        self.assertGreaterEqual(summary["exec_rate"], 0.9, summary)


if __name__ == "__main__":
    unittest.main()
