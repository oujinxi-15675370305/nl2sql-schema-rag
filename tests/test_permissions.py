"""M4 的验收测试（AC-4.1 ~ AC-4.4）。

覆盖三条泄漏路径：`error/attempts[].error`、`attempts[].sql`、
以及非 ask/检索之前的 `context`（prompt 的 DDL 从 context 来）。
"""
from __future__ import annotations

import json
import re
import unittest
from dataclasses import replace

from app.agent import ScriptedGenerator, ask, execute, validate
from app.catalog import load_catalog
from app.domain import Catalog, Metric
from app.permissions import (
    ANALYST,
    AUDIT_LOG,
    ROLES,
    VIEWER,
    AuditLog,
    denied_terms,
    hidden_columns,
    mask_rows,
    mask_value,
    masked_column_names,
    redact_sql,
    visible_catalog,
)
from app.session import Session

PHONE_SQL = "SELECT c.customer_phone FROM customers c LIMIT 5"
PHONE_RE = re.compile(r"^\d{3}\*{4}\d{4}$")


class RoleCatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog: Catalog = load_catalog()

    def test_ac_4_1_viewer_catalog_has_no_sensitive_column(self) -> None:
        # AC-4.1：列过滤在 catalog 层做，viewer 的 catalog 里根本没有手机号列
        viewer = visible_catalog(self.catalog, VIEWER)
        analyst = visible_catalog(self.catalog, ANALYST)
        viewer_columns = {c.name for t in viewer.tables for c in t.columns}
        analyst_columns = {c.name for t in analyst.tables for c in t.columns}

        self.assertNotIn("customer_phone", viewer_columns)
        self.assertIn("customer_phone", analyst_columns)  # analyst 可见但掩码

    def test_ac_4_1_metrics_requiring_hidden_column_are_dropped(self) -> None:
        # 反向（防未来漏洞）：指标依赖隐藏列时，不能靠 ddl 里的口径 SQL 把列名带出去
        leaky = Metric(name="phone_count", comment="", sql="COUNT(customer_phone)",
                       requires=("customers.customer_phone",))
        extended = replace(self.catalog, metrics=self.catalog.metrics + (leaky,))
        self.assertIsNone(visible_catalog(extended, VIEWER).metric("phone_count"))
        self.assertIsNotNone(visible_catalog(extended, ANALYST).metric("phone_count"))

    def test_ac_4_2_denied_terms_cover_synonyms(self) -> None:
        # AC-4.2：列名和口语同义词都算"请求了无权列"
        terms = denied_terms(self.catalog, VIEWER)
        for term in ("customer_phone", "手机号", "电话", "联系方式"):
            self.assertIn(term, terms)
        self.assertEqual(denied_terms(self.catalog, ANALYST), ())  # analyst 不拦

    def test_unknown_role_is_rejected(self) -> None:
        # 反向用例：未知角色不静默降级成 analyst
        with self.assertRaises(ValueError):
            ask("有多少客户", role="guest")


class ColumnWhitelistTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog: Catalog = load_catalog()

    def test_ac_4_1_validate_rejects_hidden_column_without_naming_it(self) -> None:
        # AC-4.1：列白名单；错误话术本身不能回显列名（错误会回传、也会喂给模型）
        ok, message = validate(PHONE_SQL, self.catalog, denied=hidden_columns(self.catalog, VIEWER))
        self.assertFalse(ok)
        self.assertIn("无权访问", message)
        self.assertNotIn("customer_phone", message)

    def test_ac_4_1_reverse_visible_column_passes(self) -> None:
        ok, message = validate("SELECT customer_name FROM customers LIMIT 5",
                               visible_catalog(self.catalog, VIEWER))
        self.assertTrue(ok, message)

    def test_ac_4_1_generated_sql_is_redacted_everywhere(self) -> None:
        # 泄漏路径 1/2：error 与 attempts[].sql 都不能出现隐藏列名
        plan = ask("各客户的GMV", role=VIEWER, generator=ScriptedGenerator([PHONE_SQL]), max_repair=0)

        self.assertFalse(plan.ok)
        self.assertEqual(len(plan.attempts), 1)
        self.assertNotIn("customer_phone", plan.error)
        self.assertNotIn("customer_phone", plan.sql)
        self.assertNotIn("customer_phone", json.dumps(plan.attempts, ensure_ascii=False))
        self.assertIn("***", plan.sql)

    def test_ac_4_1_reverse_analyst_is_not_blocked(self) -> None:
        # 反向用例：同一个生成器，analyst 能过白名单（不是把 column 一律拦死）
        plan = ask("各客户的GMV", role=ANALYST, generator=ScriptedGenerator([PHONE_SQL]), max_repair=0)
        self.assertTrue(plan.ok)
        self.assertEqual(plan.masked_columns, ["customer_phone"])

    def test_ac_4_1_viewer_select_star_is_rejected(self) -> None:
        # 回归：* 不含列名，白名单匹配不到它；viewer 不能靠它把隐藏列带出来
        for sql in ("SELECT * FROM customers LIMIT 2", "SELECT c.* FROM customers c LIMIT 2"):
            with self.subTest(sql=sql):
                plan = ask("各客户的GMV", role=VIEWER, generator=ScriptedGenerator([sql]), max_repair=0)
                self.assertFalse(plan.ok)
                self.assertIn("明确指定字段", plan.error)
                self.assertEqual(plan.rows, [])
                self.assertNotIn("customer_phone", json.dumps(plan.attempts, ensure_ascii=False))

    def test_ac_4_1_reverse_count_star_and_analyst_star(self) -> None:
        # 反向用例：count(*) 不是"查所有列"；analyst 的 * 仍可用，只是手机号会被掩码
        viewer = ask("各客户的GMV", role=VIEWER,
                     generator=ScriptedGenerator(["SELECT COUNT(*) FROM orders"]), max_repair=0)
        self.assertTrue(viewer.ok)

        analyst = ask("各客户的GMV", role=ANALYST,
                      generator=ScriptedGenerator(["SELECT * FROM customers LIMIT 2"]), max_repair=0)
        self.assertTrue(analyst.ok)
        self.assertIn("customer_phone", analyst.columns)
        self.assertIn("customer_phone", analyst.masked_columns)
        index = analyst.columns.index("customer_phone")
        self.assertRegex(analyst.rows[0][index], PHONE_RE)


class ViewerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog: Catalog = load_catalog()

    def test_ac_4_1_viewer_sql_and_context_have_no_phone(self) -> None:
        # 泄漏路径 3：context / prompt 的 DDL 都不能有隐藏列
        plan = ask("各客户的GMV", role=VIEWER, use_llm=False)
        self.assertEqual(plan.intent, "ask")
        self.assertTrue(plan.ok)
        self.assertNotIn("customer_phone", plan.sql)
        self.assertNotIn("customer_phone", plan.columns)
        self.assertIsNotNone(plan.context)
        ddl = plan.context.ddl(visible_catalog(self.catalog, VIEWER))
        self.assertNotIn("customer_phone", ddl)
        self.assertNotIn("customer_phone", [h.key for h in plan.context.columns])

    def test_ac_4_1_viewer_non_ask_has_no_context(self) -> None:
        # 非 ask 不检索、不返回 schema
        plan = ask("你好", role=VIEWER)
        self.assertEqual(plan.intent, "chat")
        self.assertIsNone(plan.context)
        self.assertEqual(plan.attempts, [])

    def test_ac_4_2_phone_requests_are_unsafe(self) -> None:
        # AC-4.2：请求无权列 → unsafe + 说明，不静默改列
        for question in ("各客户的GMV和手机号", "看下客户的电话", "给我手机号"):
            with self.subTest(question=question):
                plan = ask(question, role=VIEWER)
                self.assertEqual(plan.intent, "unsafe")
                self.assertEqual(plan.sql, "")
                self.assertTrue(plan.reply)
                self.assertNotIn("customer_phone", plan.reply)

    def test_ac_4_2_analyst_phone_request_is_ask(self) -> None:
        # 对照：analyst 不拦，走掩码（AC-4.3）
        plan = ask("各客户的GMV和手机号", role=ANALYST, use_llm=False)
        self.assertEqual(plan.intent, "ask")
        self.assertTrue(plan.ok)


class MaskingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog: Catalog = load_catalog()

    def test_ac_4_3_analyst_rows_are_masked(self) -> None:
        # AC-4.3：掩码在服务端结果行上做
        plan = ask("各客户的GMV和手机号", role=ANALYST, use_llm=False)
        self.assertTrue(plan.ok)
        self.assertEqual(plan.masked_columns, ["customer_phone"])
        index = plan.columns.index("customer_phone")
        self.assertTrue(plan.rows)

        _, raw_rows = execute(plan.sql)
        for raw_row, masked_row in zip(raw_rows, plan.rows):
            self.assertRegex(masked_row[index], PHONE_RE)
            # 掩码后的值必须和真实手机号不同，且真实值不能被原样返回
            self.assertNotEqual(masked_row[index], raw_row[index])
            self.assertNotIn(raw_row[index], str(masked_row))

    def test_ac_4_3_masking_is_not_done_in_sql(self) -> None:
        # 反向用例：掩码不能写进 SQL（列名要保留、口径不能变）
        plan = ask("各客户的GMV和手机号", role=ANALYST, use_llm=False)
        self.assertNotIn("****", plan.sql)
        self.assertNotIn("substr", plan.sql)

    def test_ac_4_3_raw_rows_are_untouched(self) -> None:
        # 证明掩码发生在 ask() 结果行上，而不是 SQL/执行器里
        plan = ask("各客户的GMV和手机号", role=ANALYST, use_llm=False)
        cols, raw_rows = execute(plan.sql)
        index = cols.index("customer_phone")
        self.assertNotRegex(raw_rows[0][index], PHONE_RE)

    def test_ac_4_3_reverse_viewer_has_no_masked_columns(self) -> None:
        # 反向用例：viewer 的普通问数结果里没有敏感列，也就没有"被掩码"的列
        plan = ask("各客户的GMV", role=VIEWER, use_llm=False)
        self.assertEqual(plan.masked_columns, [])
        self.assertNotIn("customer_phone", plan.columns)

    def test_mask_value_and_rows(self) -> None:
        self.assertEqual(mask_value("13800000047"), "138****0047")
        self.assertEqual(mask_value(None), "")
        self.assertEqual(mask_value("12"), "***")
        rows, hit = mask_rows(["a", "customer_phone"], [(1, "13800000047")], ("customer_phone",))
        self.assertEqual(rows, [(1, "138****0047")])
        self.assertEqual(hit, ["customer_phone"])
        # 没命中就不改行
        self.assertEqual(mask_rows(["a"], [(1,)], ("customer_phone",)), ([(1,)], []))

    def test_ac_4_3_alias_and_expression_are_rejected(self) -> None:
        # 回归：改名/函数包裹会让按列名的掩码失效，必须在校验阶段拒
        for sql in (
            "SELECT c.customer_phone AS p FROM customers c LIMIT 2",
            "SELECT c.customer_phone p FROM customers c LIMIT 2",
            "SELECT substr(c.customer_phone,1,11) AS p FROM customers c LIMIT 2",
            "SELECT c.customer_phone || '-' AS p FROM customers c LIMIT 2",
        ):
            with self.subTest(sql=sql):
                plan = ask("各客户的GMV", role=ANALYST,
                           generator=ScriptedGenerator([sql]), max_repair=0)
                self.assertFalse(plan.ok)
                self.assertIn("敏感字段", plan.error)
                self.assertNotIn("customer_phone", plan.error)
                self.assertEqual(plan.rows, [])

    def test_ac_4_3_value_backstop_masks_even_when_column_name_is_lost(self) -> None:
        # 兜底：即使列名丢了（极端 SQL），结果里的手机号形状也不能原样返回
        rows, hit = mask_rows(["p"], [("13800000001",), ("客户A",)], ("customer_phone",))
        self.assertEqual(rows, [("138****0001",), ("客户A",)])
        self.assertEqual(hit, [])


class AuditTest(unittest.TestCase):
    def setUp(self) -> None:
        AUDIT_LOG.clear()

    def test_ac_4_4_denied_request_is_audited(self) -> None:
        # AC-4.4：越权请求进审计（内存环形缓冲）
        ask("给我手机号", role=VIEWER)
        entries = AUDIT_LOG.entries()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.role, VIEWER)
        self.assertEqual(entry.question, "给我手机号")
        self.assertIn("无权访问", entry.reason)
        self.assertNotIn("customer_phone", entry.sql)

    def test_ac_4_4_generated_sql_denial_is_audited_with_redacted_sql(self) -> None:
        # 生成器自己写隐藏列也算越权尝试；审计里同样不能留列名
        ask("各客户的GMV", role=VIEWER, generator=ScriptedGenerator([PHONE_SQL]), max_repair=0)
        entries = AUDIT_LOG.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].role, VIEWER)
        self.assertIn("无权访问", entries[0].reason)
        self.assertIn("***", entries[0].sql)
        self.assertNotIn("customer_phone", entries[0].sql)

    def test_ac_4_4_reverse_normal_query_is_not_audited(self) -> None:
        # 反向用例：正常问数不该刷审计
        ask("各地区的GMV", role=VIEWER, use_llm=False)
        self.assertEqual(len(AUDIT_LOG), 0)

    def test_ac_4_4_ring_buffer_caps_entries(self) -> None:
        log = AuditLog(max_entries=3)
        for i in range(5):
            log.record(role=VIEWER, question=f"q{i}", reason="r")
        entries = log.entries()
        self.assertEqual(len(entries), 3)
        self.assertEqual([e.question for e in entries], ["q2", "q3", "q4"])


class RoleSessionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog: Catalog = load_catalog()

    def test_m3_inheritance_still_works_for_viewer(self) -> None:
        # role 不进 Session（decisions）：analyst 开的头，viewer 接着说也能接上
        session = Session()
        ask("各地区的GMV", role=ANALYST, use_llm=False, session=session)
        plan = ask("那上个月呢", role=VIEWER, use_llm=False, session=session)

        self.assertEqual(plan.intent, "ask")
        self.assertEqual(plan.role, VIEWER)
        self.assertIn("metric", plan.inherited_slots)
        self.assertIn("start of month", plan.sql)
        self.assertNotIn("customer_phone", plan.sql)
        self.assertFalse(hasattr(session, "role"))

    def test_viewer_turn_does_not_leak_sensitive_slots(self) -> None:
        # 槽位只描述业务维度/指标，不含列名；继承后也不会把 phone 带进 SQL
        session = Session()
        ask("各客户的GMV和手机号", role=ANALYST, use_llm=False, session=session)
        plan = ask("那这个月呢", role=VIEWER, use_llm=False, session=session)

        self.assertEqual(plan.intent, "ask")
        self.assertNotIn("customer_phone", plan.sql)
        self.assertEqual(plan.masked_columns, [])


class RedactionTest(unittest.TestCase):
    def test_redact_sql_only_hits_identifiers(self) -> None:
        sql = "SELECT c.customer_phone FROM customers c WHERE c.customer_phone IS NOT NULL"
        redacted = redact_sql(sql, ("customer_phone",))
        self.assertNotIn("customer_phone", redacted)
        self.assertEqual(redacted.count("***"), 2)
        # 不误伤同名前缀的列名
        self.assertEqual(redact_sql("SELECT customer_phonex", ("customer_phone",)), "SELECT customer_phonex")

    def test_masked_columns_are_role_scoped(self) -> None:
        self.assertEqual(masked_column_names(ANALYST), ("customer_phone",))
        self.assertEqual(masked_column_names(VIEWER), ())
        self.assertEqual(set(ROLES), {ANALYST, VIEWER})


if __name__ == "__main__":
    unittest.main()
