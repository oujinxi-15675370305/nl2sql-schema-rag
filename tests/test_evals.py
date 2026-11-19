"""M5 评测测试：用例覆盖、失败分类、多轮独立指标（AC-5.1 ~ AC-5.5）。"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from app.agent import ScriptedGenerator, ask, execute, validate
from app.catalog import load_catalog
from app.domain import SchemaContext, SQLPlan
from app.evals import CASES, MULTI_CASES, classify_failure, run
from app.slots import DIMENSION_HINTS, extract_keywords

TYPO_SQL = "SELECT COUNT(DISTINCT order_cnt) FROM orders WHERE status = 'paid' LIMIT 10"


def _slots_of(cases: list[dict], catalog) -> list[dict[str, list[str]]]:
    return [extract_keywords(case["question"], catalog) for case in cases]


class EvalCoverageTest(unittest.TestCase):
    """AC-5.1：数量、指标×维度、时间过滤要钉住——加指标/维度忘了补用例会直接红。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()
        cls.cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
        cls.slots = _slots_of(cls.cases, cls.catalog)

    def test_at_least_fifty_cases_with_unique_ids(self) -> None:
        self.assertGreaterEqual(len(self.cases), 50)
        ids = [case["id"] for case in self.cases]
        self.assertEqual(len(ids), len(set(ids)), "id 不能复用")
        asks = [c for c in self.cases if c.get("expected_intent", "ask") == "ask"]
        self.assertGreaterEqual(len(asks), 50, "问数用例不能少于 50 条")

    def test_intent_cases_cover_all_non_ask_kinds(self) -> None:
        # 意图门禁要有真样本：chat / clarify / out_of_scope / unsafe 各至少一条，
        # 且有一条 viewer 越权（证明权限路径也在评测里，而不是只在单测里）
        expected = {c.get("expected_intent", "ask") for c in self.cases}
        self.assertTrue({"chat", "clarify", "out_of_scope", "unsafe"} <= expected, expected)
        self.assertTrue(any(c.get("role") == "viewer" for c in self.cases), "缺 viewer 越权用例")
        for case in self.cases:
            if case.get("expected_intent", "ask") == "ask":
                self.assertIn("expected_sql", case, f"#{case['id']} 问数用例缺 expected_sql")
            else:
                self.assertNotIn("expected_sql", case, f"#{case['id']} 非问数用例不该有 expected_sql")

    def test_every_metric_and_dimension_appears(self) -> None:
        metrics = {m for s in self.slots for m in s["metric"]}
        dims = {d for s in self.slots for d in s["dimension"]}
        self.assertEqual(metrics, {m.name for m in self.catalog.metrics})
        self.assertEqual(dims, set(DIMENSION_HINTS))

    def test_metric_dimension_pairs_cover_all_but_customer(self) -> None:
        # 6 指标 × 6 维度里，customer 维度按客户分组语义特殊（客户数每行都是 1），
        # 按 5.x 的决定只留 1 条连通性用例；其余 30 个组合必须全覆盖。
        pairs = {(m, d) for s in self.slots for m in s["metric"] for d in s["dimension"]}
        expected = {(m.name, d) for m in self.catalog.metrics for d in DIMENSION_HINTS if d != "customer"}
        self.assertEqual(expected - pairs, set(), "指标×维度组合有缺口")
        self.assertTrue({p for p in pairs if p[1] == "customer"}, "customer 维度一条都没有")

    def test_time_filters_cover_at_least_five_kinds(self) -> None:
        # 时间口径要覆盖固定月份（month:YYYY-MM）和滚动窗口（last_30d），不只上月/本月/今年
        kinds = {t.split(":", 1)[0] for s in self.slots for t in s["time"]}
        self.assertGreaterEqual(len(kinds), 5, kinds)
        self.assertIn("month", kinds)
        self.assertIn("last_30d", kinds)


class MultiTurnCoverageTest(unittest.TestCase):
    """AC-5.5：多轮评测是独立文件、独立指标，至少 3 个会话 × 3 轮。"""

    def test_at_least_three_sessions_of_three_turns(self) -> None:
        sessions = json.loads(MULTI_CASES.read_text(encoding="utf-8"))["sessions"]
        self.assertGreaterEqual(len(sessions), 3)
        for spec in sessions:
            self.assertTrue(spec["id"])
            self.assertGreaterEqual(len(spec["turns"]), 3, spec["id"])

    def test_customer_and_rewrite_are_covered(self) -> None:
        # M4 的 P0（customer 标签漂移）和 M2/M3 的"改成华南"都要在多轮里留样本
        sessions = json.loads(MULTI_CASES.read_text(encoding="utf-8"))["sessions"]
        questions = [turn["question"] for spec in sessions for turn in spec["turns"]]
        self.assertTrue(any("客户" in q for q in questions), "缺 customer 维度/指标回环")
        self.assertTrue(any("改成" in q for q in questions), "缺「改成华南」改写")

    def test_multi_turn_details_record_inheritance(self) -> None:
        summary = run(verbose=False)
        details = {(d["session"], d["turn"]): d for d in summary["multi_turn_details"]}
        # 第二句自己给了时间，指标 + 维度从上一句继承
        self.assertEqual(details[("m1", 2)]["inherited_slots"], ["metric", "dimension"])
        # 第三句只换维度：指标 + 时间继续继承
        self.assertEqual(details[("m1", 3)]["inherited_slots"], ["metric", "time"])
        # 换了指标又没指代：不能把上文的地区分组套过来
        self.assertEqual(details[("m3", 2)]["inherited_slots"], [])
        # 澄清轮不记入会话：中间一句 clarify，第三句仍能接第一句
        self.assertEqual(details[("m5", 2)]["intent"], "clarify")
        self.assertTrue(details[("m5", 2)]["matched"])
        self.assertEqual(details[("m5", 3)]["inherited_slots"], ["metric", "dimension"])
        # 客户维度：第二句补时间，指标 + 维度继承（M4 的 P0 回归点）
        self.assertEqual(details[("m6", 2)]["inherited_slots"], ["metric", "dimension"])
        self.assertIn("GROUP BY c.customer_name", details[("m6", 2)]["sql"])
        self.assertEqual(details[("m6", 3)]["inherited_slots"], ["metric", "time"])
        # 改写说法：第二句只换筛选，指标继承
        self.assertEqual(details[("m7", 2)]["inherited_slots"], ["metric"])
        self.assertIn("华南", details[("m7", 2)]["sql"])


class ExpectedSqlTest(unittest.TestCase):
    """评测集自检：期望 SQL 必须能过校验并执行。

    手写 expected_sql 打错表名/列名时，先红在这一层，而不是在分数里表现为"模型答错"。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()
        cls.cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
        cls.sessions = json.loads(MULTI_CASES.read_text(encoding="utf-8"))["sessions"]

    def _check(self, sql: str, label: str) -> None:
        ok, message = validate(sql, self.catalog)
        self.assertTrue(ok, f"{label} 期望 SQL 不合法：{message}")
        query = sql if "limit" in sql.lower() else f"{sql} LIMIT 200"
        try:
            execute(query)
        except Exception as exc:  # 测试里只需要失败信息，不用细分异常类型
            self.fail(f"{label} 期望 SQL 执行失败：{exc}")

    def test_single_turn_expected_sql(self) -> None:
        for case in self.cases:
            if case.get("expected_intent", "ask") != "ask":
                continue
            self._check(case["expected_sql"], f"#{case['id']}")

    def test_multi_turn_expected_sql(self) -> None:
        for spec in self.sessions:
            for index, turn in enumerate(spec["turns"], start=1):
                if turn.get("expected_sql"):
                    self._check(turn["expected_sql"], f"{spec['id']}#{index}")


class FailureClassifyTest(unittest.TestCase):
    """AC-5.3：失败分类靠 ask() 记下的 failure_stage，不靠猜 error 前缀。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()

    def _plan(self, scripts: list[str], question: str = "多少单", **kwargs) -> SQLPlan:
        return ask(question, catalog=self.catalog, generator=ScriptedGenerator(scripts), **kwargs)

    def test_matched_plan_has_no_failure(self) -> None:
        self.assertEqual(classify_failure(self._plan(["SELECT COUNT(*) AS n FROM orders LIMIT 10"]), True), "")

    def test_field_error(self) -> None:
        plan = self._plan([TYPO_SQL])
        self.assertEqual(plan.failure_stage, "execute")
        self.assertEqual(classify_failure(plan, False), "field")

    def test_syntax_error(self) -> None:
        plan = self._plan(["SELECT FROM orders LIMIT 10"])
        self.assertEqual(plan.failure_stage, "execute")
        self.assertEqual(classify_failure(plan, False), "syntax")

    def test_validate_error(self) -> None:
        plan = self._plan(["SELECT * FROM secret_table LIMIT 1"])
        self.assertEqual(plan.failure_stage, "validate")
        self.assertEqual(classify_failure(plan, False), "validate")

    def test_timeout(self) -> None:
        runaway = "SELECT COUNT(*) FROM orders a, orders b, orders c, orders d"
        with mock.patch("app.agent.QUERY_TIMEOUT_S", 0.2):
            plan = self._plan([runaway], max_repair=2)
        self.assertEqual(plan.failure_stage, "timeout")
        self.assertEqual(classify_failure(plan, False), "timeout")

    def test_permission_beats_intent(self) -> None:
        # viewer 要手机号 → 路由判 unsafe，但失败原因要归到权限，不是笼统的"路由拦截"
        plan = ask("各客户的GMV和手机号", catalog=self.catalog, use_llm=False, role="viewer")
        self.assertEqual(plan.intent, "unsafe")
        self.assertEqual(classify_failure(plan, False), "permission")

    def test_routed_offtopic_or_chat(self) -> None:
        plan = ask("你好", catalog=self.catalog, use_llm=False)
        self.assertEqual(plan.intent, "chat")
        self.assertEqual(classify_failure(plan, False), "routed")

    def test_result_mismatch(self) -> None:
        # 跑成功但结果和期望不一致（这里故意把"各地区"当失败用例）
        plan = ask("各地区的GMV", catalog=self.catalog, use_llm=False)
        self.assertTrue(plan.ok)
        self.assertEqual(classify_failure(plan, False), "result")

    def test_retrieval_missed(self) -> None:
        # 检索没把问的指标带进 context → 归检索，而不是笼统的结果不一致
        plan = SQLPlan(question="x", ok=True, rows=[(1,)],
                       context=SchemaContext(), keywords={"metric": ["gmv"]})
        self.assertEqual(classify_failure(plan, False), "retrieval")


class EvalSummaryTest(unittest.TestCase):
    """AC-5.2 / AC-5.5：单轮、多轮、意图、失败分类在 summary 里各占独立字段。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.summary = run(verbose=False)

    def test_single_turn_summary(self) -> None:
        summary = self.summary
        self.assertGreaterEqual(summary["total"], 57)
        self.assertGreaterEqual(summary["ask_total"], 50)
        self.assertEqual(summary["exec_match"], summary["ask_total"])
        self.assertEqual(summary["exec_rate"], 1.0)
        self.assertEqual(summary["intent_rate"], 1.0)
        self.assertEqual(summary["failure_counts"], {})
        self.assertEqual(len(summary["details"]), summary["total"])
        self.assertTrue(all(d["failure"] == "" for d in summary["details"]))

    def test_multi_turn_is_separate_from_single_turn(self) -> None:
        summary = self.summary
        self.assertGreaterEqual(summary["multi_turn_total"], 21)
        self.assertEqual(summary["multi_turn_match"], summary["multi_turn_total"])
        self.assertEqual(summary["multi_turn_rate"], 1.0)
        self.assertEqual(summary["multi_turn_failure_counts"], {})
        # 两组用例不混算：多轮轮数不能并进单轮 total
        self.assertEqual(summary["total"], len(summary["details"]))
        self.assertEqual(summary["multi_turn_total"], len(summary["multi_turn_details"]))


if __name__ == "__main__":
    unittest.main()
