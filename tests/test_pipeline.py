"""端到端测试：检索、生成、校验、执行、评测。不依赖任何外部服务。"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from app.agent import (
    LLMGenerator,
    QueryTimeout,
    ScriptedGenerator,
    _shape_feedback,
    ask,
    build_context,
    execute,
    llm_sql,
    validate,
)
from app.catalog import load_catalog
from app.evals import normalize, run as run_eval
from app.fallback import METRIC_SQL, rule_based_sql
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


class FallbackSplitTest(unittest.TestCase):
    """规则 SQL 只在 app/fallback.py；agent.py 只留 LLM、校验、执行、重试。"""

    def test_agent_has_no_rule_sql(self) -> None:
        import app.agent as agent
        self.assertFalse(hasattr(agent, "rule_based_sql"))
        self.assertFalse(hasattr(agent, "METRIC_SQL"))
        self.assertFalse(hasattr(agent, "RuleGenerator"))

    def test_llm_generator_degrades_to_offline_fallback(self) -> None:
        from app.fallback import OfflineFallback
        self.assertIsInstance(LLMGenerator().fallback, OfflineFallback)
        self.assertEqual(OfflineFallback.last_engine, "rule")


class DimensionListingTest(unittest.TestCase):
    """没有指标的维度清单（"华东有哪些商品类别"）：不澄清，走 SELECT DISTINCT 生成。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()
        cls.retriever = SchemaRetriever(cls.catalog)

    def test_context_carries_orders_without_metric(self) -> None:
        # 没有指标时也要靠 orders 才能把地区连到商品；否则模型只能拿到孤立的维表
        ctx, _ = build_context("华东地区有哪些商品类别", self.catalog, self.retriever)
        names = ctx.table_names()
        for table in ("orders", "order_items", "products", "regions"):
            self.assertIn(table, names)

    def test_dimension_listing_plan_executes(self) -> None:
        sql = (
            "SELECT DISTINCT p.category FROM orders o "
            "JOIN order_items i ON i.order_id = o.order_id "
            "JOIN products p ON p.product_id = i.product_id "
            "JOIN regions r ON r.region_id = o.region_id "
            "WHERE o.status = 'paid' AND r.region_name = '华东'"
        )
        plan = ask("华东地区门店有哪些商品类别", catalog=self.catalog,
                   generator=ScriptedGenerator([sql]))
        self.assertEqual(plan.intent, "ask")
        self.assertTrue(plan.ok, plan.error)
        self.assertGreaterEqual(len(plan.rows), 1)
        self.assertEqual(plan.engine, "custom")


class ComplexSqlTest(unittest.TestCase):
    """复杂 SQL（CTE + 窗口函数）也要能过校验、执行；不只支持单层 GROUP BY。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()

    def test_cte_window_top_n_plan_executes(self) -> None:
        sql = (
            "WITH ranked AS ("
            "SELECT p.category, p.product_name, ROUND(SUM(o.pay_amount), 2) AS gmv, "
            "ROW_NUMBER() OVER (PARTITION BY p.category ORDER BY SUM(o.pay_amount) DESC) AS rn "
            "FROM orders o "
            "JOIN order_items i ON i.order_id = o.order_id "
            "JOIN products p ON p.product_id = i.product_id "
            "JOIN regions r ON r.region_id = o.region_id "
            "WHERE o.status = 'paid' AND r.region_name = '华东' "
            "GROUP BY p.category, p.product_name"
            ") SELECT category, product_name, gmv FROM ranked WHERE rn <= 2 "
            "ORDER BY category, gmv DESC"
        )
        plan = ask("华东每个品类销售额前两名商品", catalog=self.catalog,
                   generator=ScriptedGenerator([sql]))
        self.assertEqual(plan.intent, "ask")
        self.assertTrue(plan.ok, plan.error)
        self.assertEqual(len(plan.rows), 8)  # 4 个品类 × 前 2 名


class SqlShapeGuardTest(unittest.TestCase):
    """只换筛选条件时：多选筛选列 / 丢筛选 → 当 validate 失败，带 feedback 重写。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()

    def test_grouped_top_n_guard(self) -> None:
        bad = ("SELECT p.brand, p.product_name, SUM(o.pay_amount) AS gmv FROM orders o "
               "JOIN order_items i ON i.order_id = o.order_id "
               "JOIN products p ON p.product_id = i.product_id "
               "WHERE o.status = 'paid' GROUP BY p.brand, p.product_name "
               "ORDER BY gmv DESC LIMIT 10")
        self.assertIn("分组内", _shape_feedback(bad, {}, "每个品牌销售额前两名商品"))
        good = ("WITH ranked AS (SELECT p.brand, p.product_name, SUM(o.pay_amount) AS gmv, "
                "ROW_NUMBER() OVER (PARTITION BY p.brand ORDER BY SUM(o.pay_amount) DESC) AS rn "
                "FROM orders o JOIN order_items i ON i.order_id = o.order_id "
                "JOIN products p ON p.product_id = i.product_id "
                "WHERE o.status = 'paid' GROUP BY p.brand, p.product_name) "
                "SELECT brand, product_name, gmv FROM ranked WHERE rn <= 2")
        self.assertEqual(_shape_feedback(good, {}, "每个品牌销售额前两名商品"), "")

    def test_month_over_month_is_not_grouped_top_n(self) -> None:
        # “前一个月”是时间词，不是“前 N 名”；不能触发分组 Top-N 守卫
        sql = ("SELECT month, gmv, LAG(gmv) OVER (ORDER BY month) AS mom "
               "FROM (SELECT substr(o.order_date,1,7) AS month, SUM(o.pay_amount) AS gmv "
               "FROM orders o WHERE o.status = 'paid' GROUP BY month)")
        question = "今年每个月的GMV和环比（(当月-前一个月)/前一个月，保留4位小数）"
        self.assertEqual(_shape_feedback(sql, {}, question), "")

    def test_extra_filter_column_triggers_rewrite(self) -> None:
        bad = (
            "SELECT r.region_name, ROUND(SUM(o.pay_amount), 2) AS gmv "
            "FROM orders o JOIN regions r ON r.region_id = o.region_id "
            "WHERE o.status = 'paid' AND r.region_name = '华东'"
        )
        good = (
            "SELECT ROUND(SUM(o.pay_amount), 2) AS gmv "
            "FROM orders o JOIN regions r ON r.region_id = o.region_id "
            "WHERE o.status = 'paid' AND r.region_name = '华东'"
        )
        plan = ask("华东的GMV", catalog=self.catalog,
                   generator=ScriptedGenerator([bad, good]), max_repair=1)
        self.assertTrue(plan.ok, plan.error)
        self.assertEqual(len(plan.attempts), 2)
        self.assertEqual(plan.attempts[0]["stage"], "validate")
        self.assertIn("筛选列", plan.attempts[0]["error"])

    def test_missing_filter_triggers_rewrite(self) -> None:
        bad = "SELECT ROUND(SUM(o.pay_amount), 2) AS gmv FROM orders o WHERE o.status = 'paid'"
        good = (
            "SELECT ROUND(SUM(o.pay_amount), 2) AS gmv FROM orders o "
            "JOIN regions r ON r.region_id = o.region_id "
            "WHERE o.status = 'paid' AND r.region_name = '华东'"
        )
        plan = ask("华东的GMV", catalog=self.catalog,
                   generator=ScriptedGenerator([bad, good]), max_repair=1)
        self.assertTrue(plan.ok, plan.error)
        self.assertEqual(len(plan.attempts), 2)
        self.assertIn("保留", plan.attempts[0]["error"])


class PromptContextTest(unittest.TestCase):
    """给 LLM 的上下文：JOIN 键、表关系、指标依赖的列、口径规则都不能少。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()
        cls.retriever = SchemaRetriever(cls.catalog)

    def test_repurchase_table_only_for_repurchase_queries(self) -> None:
        normal, _ = build_context("有多少客户", self.catalog, self.retriever)
        self.assertNotIn("repurchase_customers", [h.key for h in normal.tables])
        repeat, _ = build_context("复购客户数", self.catalog, self.retriever)
        self.assertIn("repurchase_customers", [h.key for h in repeat.tables])

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
        self.assertIn("SELECT DISTINCT", prompt)
        self.assertIn("ROW_NUMBER", prompt)
        self.assertIn("有哪些", prompt)
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

    def test_accept_cte_select(self) -> None:
        # 复杂问题（分组 Top-N / 多步聚合）会用 WITH；CTE 名不能当成未知表
        sql = (
            "WITH ranked AS ("
            "SELECT p.category, SUM(i.quantity) AS units "
            "FROM orders o JOIN order_items i ON i.order_id = o.order_id "
            "JOIN products p ON p.product_id = i.product_id "
            "GROUP BY p.category"
            ") SELECT category FROM ranked WHERE units > 0"
        )
        ok, msg = validate(sql, self.catalog)
        self.assertTrue(ok, msg)

    def test_reject_cte_with_write(self) -> None:
        ok, _ = validate("WITH x AS (SELECT 1) DELETE FROM orders", self.catalog)
        self.assertFalse(ok)


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
