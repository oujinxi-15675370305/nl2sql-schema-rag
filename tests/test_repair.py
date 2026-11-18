"""M1 的验收测试。每条对应 docs/SPEC.md 的 AC 编号。"""
from __future__ import annotations

import unittest
from unittest import mock

from app.agent import RuleGenerator, ScriptedGenerator, ask
from app.catalog import load_catalog

GOOD_SQL = "SELECT COUNT(DISTINCT order_id) FROM orders WHERE status = 'paid' LIMIT 10"
TYPO_SQL = "SELECT COUNT(DISTINCT order_cnt) FROM orders WHERE status = 'paid' LIMIT 10"


class RepairTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()

    def test_ac_1_1_feedback_is_passed_to_second_attempt(self) -> None:
        # AC-1.1：第一次失败后，第二次生成能收到错误信息
        gen = ScriptedGenerator([TYPO_SQL, GOOD_SQL])
        plan = ask("多少单", catalog=self.catalog, retriever=None, generator=gen, max_repair=1)
        self.assertEqual(len(gen.calls), 2)
        self.assertIsNone(gen.calls[0])
        self.assertIsNotNone(gen.calls[1])
        self.assertIn("order_cnt", gen.calls[1])
        self.assertTrue(plan.ok)
        self.assertEqual(len(plan.attempts), 2)

    def test_ac_1_2_repaired_result_equals_one_shot(self) -> None:
        # AC-1.2：修复成功的结果与"一次写对"完全一致
        one_shot = ask("多少单", catalog=self.catalog, generator=ScriptedGenerator([GOOD_SQL]))
        repaired = ask("多少单", catalog=self.catalog, generator=ScriptedGenerator([TYPO_SQL, GOOD_SQL]),
                       max_repair=1)
        self.assertEqual(one_shot.rows, repaired.rows)
        self.assertEqual(one_shot.columns, repaired.columns)

    def test_ac_1_2_repaired_plan_has_no_stale_error(self) -> None:
        # 回归：修好之后 ok=True 的返回值里不能还带着上一轮的报错
        plan = ask("多少单", catalog=self.catalog, generator=ScriptedGenerator([TYPO_SQL, GOOD_SQL]),
                   max_repair=1)
        self.assertTrue(plan.ok)
        self.assertEqual(plan.error, "")
        self.assertEqual(plan.attempts[-1]["error"], "")

    def test_ac_1_3_exhausted_keeps_last_error_and_attempts(self) -> None:
        # AC-1.3：重试用完仍失败 → ok=False，error 是最后一次的报错，attempts 全量记录
        gen = ScriptedGenerator([TYPO_SQL, TYPO_SQL, TYPO_SQL])
        plan = ask("多少单", catalog=self.catalog, generator=gen, max_repair=2)
        self.assertFalse(plan.ok)
        self.assertIn("order_cnt", plan.error)
        self.assertEqual(len(plan.attempts), 3)
        self.assertFalse(any(a["ok"] for a in plan.attempts))

    def test_ac_1_4_repair_cannot_bypass_validator(self) -> None:
        # AC-1.4：即使"修好"的 SQL 是危险语句，也必须被校验拦下
        gen = ScriptedGenerator(["DELETE FROM orders", "SELECT * FROM orders LIMIT 5"])
        plan = ask("多少单", catalog=self.catalog, generator=gen, max_repair=1)
        self.assertTrue(plan.attempts[0]["error"])
        self.assertIn("SELECT", plan.attempts[0]["error"])
        self.assertTrue(plan.ok)

    def test_ac_1_5_zero_repair_runs_once(self) -> None:
        # AC-1.5：max_repair=0 时只尝试一次
        gen = ScriptedGenerator([TYPO_SQL, GOOD_SQL])
        plan = ask("多少单", catalog=self.catalog, generator=gen, max_repair=0)
        self.assertEqual(len(gen.calls), 1)
        self.assertFalse(plan.ok)

    def test_query_timeout_is_not_retried(self) -> None:
        # AC-1.6：超时不重试同一句（否则一次请求烧掉 3 倍预算），attempts 只记一次
        runaway = "SELECT COUNT(*) FROM orders a, orders b, orders c, orders d"
        with mock.patch("app.agent.QUERY_TIMEOUT_S", 0.2):
            plan = ask("多少单", catalog=self.catalog,
                       generator=ScriptedGenerator([runaway]), max_repair=2)
        self.assertFalse(plan.ok)
        self.assertIn("超时", plan.error)
        self.assertEqual(len(plan.attempts), 1)

    def test_engine_reports_which_generator_ran(self) -> None:
        # 排查"配了 key 却一直走规则"：返回值要说清这次是谁生成的
        self.assertEqual(ask("多少单", catalog=self.catalog, generator=RuleGenerator()).engine, "rule")
        scripted = ask("多少单", catalog=self.catalog, generator=ScriptedGenerator([GOOD_SQL]))
        self.assertEqual(scripted.engine, "custom")

    def test_ac_1_4_reverse_unknown_table_is_reported(self) -> None:
        # 反向用例：编造表名必须报错且不执行
        gen = ScriptedGenerator(["SELECT * FROM secret_table LIMIT 1"])
        plan = ask("多少单", catalog=self.catalog, generator=gen, max_repair=0)
        self.assertFalse(plan.ok)
        self.assertIn("secret_table", plan.error)
        self.assertEqual(plan.rows, [])

    def test_attempts_record_stage_and_repair_clears_failure_stage(self) -> None:
        # M5（AC-5.3 前置）：每次尝试记录 stage；修复成功后不能留下失败阶段
        plan = ask("多少单", catalog=self.catalog,
                   generator=ScriptedGenerator([TYPO_SQL, GOOD_SQL]), max_repair=1)
        self.assertEqual([a["stage"] for a in plan.attempts], ["execute", "ok"])
        self.assertTrue(plan.ok)
        self.assertEqual(plan.failure_stage, "")

    def test_failure_stage_for_validate(self) -> None:
        # 表名校验失败发生在 validate，不该算成执行错
        plan = ask("多少单", catalog=self.catalog,
                   generator=ScriptedGenerator(["SELECT * FROM secret_table LIMIT 1"]), max_repair=0)
        self.assertEqual(plan.attempts[0]["stage"], "validate")
        self.assertEqual(plan.failure_stage, "validate")

    def test_failure_stage_for_timeout(self) -> None:
        # 超时单独记 timeout：超时和执行语法错误要能分开统计
        runaway = "SELECT COUNT(*) FROM orders a, orders b, orders c, orders d"
        with mock.patch("app.agent.QUERY_TIMEOUT_S", 0.2):
            plan = ask("多少单", catalog=self.catalog,
                       generator=ScriptedGenerator([runaway]), max_repair=2)
        self.assertEqual(plan.attempts[0]["stage"], "timeout")
        self.assertEqual(plan.failure_stage, "timeout")
        self.assertEqual(len(plan.attempts), 1)


if __name__ == "__main__":
    unittest.main()
