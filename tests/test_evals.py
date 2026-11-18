"""M5 评测测试：用例覆盖、失败分类、多轮独立指标（AC-5.1 ~ AC-5.5）。"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from app.agent import ScriptedGenerator, ask
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

    def test_multi_turn_details_record_inheritance(self) -> None:
        summary = run(verbose=False)
        details = {(d["session"], d["turn"]): d for d in summary["multi_turn_details"]}
        # 第二句自己给了时间，指标 + 维度从上一句继承
        self.assertEqual(details[("m1", 2)]["inherited_slots"], ["metric", "dimension"])
        # 第三句只换维度：指标 + 时间继续继承
        self.assertEqual(details[("m1", 3)]["inherited_slots"], ["metric", "time"])
        # 换了指标又没指代：不能把上文的地区分组套过来
        self.assertEqual(details[("m3", 2)]["inherited_slots"], [])


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
        self.assertGreaterEqual(summary["total"], 50)
        self.assertEqual(summary["exec_match"], summary["total"])
        self.assertEqual(summary["exec_rate"], 1.0)
        self.assertEqual(summary["intent_rate"], 1.0)
        self.assertEqual(summary["failure_counts"], {})
        self.assertTrue(all(d["failure"] == "" for d in summary["details"]))

    def test_multi_turn_is_separate_from_single_turn(self) -> None:
        summary = self.summary
        self.assertGreaterEqual(summary["multi_turn_total"], 9)
        self.assertEqual(summary["multi_turn_match"], summary["multi_turn_total"])
        self.assertEqual(summary["multi_turn_rate"], 1.0)
        # 两组用例不混算：多轮轮数不能并进单轮 total
        self.assertEqual(summary["total"], len(summary["details"]))
        self.assertEqual(summary["multi_turn_total"], len(summary["multi_turn_details"]))


if __name__ == "__main__":
    unittest.main()
