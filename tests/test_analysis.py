"""分析模式（Step 2）：Planner / Executor / Synthesizer 的离线测试。

这里**不碰真实模型**：`chat_fn` 和 `ask_fn` 都是脚本化的。重点钉住四件事：
1. 规划坏 JSON / 空步骤 / 超过 4 步时怎么降级；
2. 每步复用 ask 的形状（mode=single、session=None），失败/澄清不硬跑；
3. 综合结论必须引用步骤结果，失败步骤由代码补进“局限”，无证据时直接说无法判断；
4. `agent.ask(mode="analysis")` 的返回结构与回退单 SQL 的路径。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.agent import ScriptedGenerator, ask
from app.analysis import (
    ChatResult,
    DEFAULT_STEP_MAX_REPAIR,
    _allows_temperature,
    _analysis_endpoint,
    _can_replan,
    _parse_json,
    _repurchase_aggregation_error,
    _sql_warning,
    _step_repair,
    execute_steps,
    plan,
    run_analysis,
    synthesize,
)
from app.catalog import load_catalog
from app.domain import AnalysisResult, AnalysisStep, SQLPlan

VALID_PLAN = json.dumps({
    "subquestions": [
        {"id": "a", "question": "各地区的GMV", "purpose": "先看总量分布", "depends_on": []},
        {"id": "b", "question": "各地区的退款率", "purpose": "再对比退款", "depends_on": []},
    ],
    "answer_instructions": "先总量后退款率，数字都标来源",
}, ensure_ascii=False)


def _reply(text: str, prompt: int = 0, completion: int = 0) -> ChatResult:
    return ChatResult(text=text, prompt_tokens=prompt, completion_tokens=completion)


class ScriptedChat:
    """按顺序返回文本/None；callable 可以拿到 messages 做分支。"""

    def __init__(self, replies: list) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def __call__(self, messages, model=None):
        self.calls.append(messages)
        value = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        if callable(value):
            return value(messages)
        if value is None or isinstance(value, ChatResult):
            return value
        return _reply(str(value))


class PlannerTest(unittest.TestCase):
    def test_valid_json(self) -> None:
        out = plan("各地区的GMV和退款率", chat_fn=ScriptedChat([VALID_PLAN]))
        self.assertEqual(out.error, "")
        self.assertEqual([s["id"] for s in out.steps], ["s1", "s2"])
        self.assertIn("先总量", out.instructions)
        self.assertEqual(out.warning, "")

    def test_bad_json_retries_once(self) -> None:
        chat = ScriptedChat(["这不是 JSON", VALID_PLAN])
        out = plan("问题", chat_fn=chat)
        self.assertEqual(out.error, "")
        self.assertEqual(len(chat.calls), 2)
        self.assertEqual(len(out.steps), 2)

    def test_bad_json_twice_falls_back(self) -> None:
        out = plan("问题", chat_fn=ScriptedChat(["坏", "还是坏"]))
        self.assertIn("规划失败", out.error)
        self.assertIn("已重试 1 次", out.error)
        self.assertEqual(out.steps, [])

    def test_more_than_four_steps_is_truncated(self) -> None:
        data = {"subquestions": [
            {"id": f"x{i}", "question": f"子问题{i}", "purpose": ""} for i in range(1, 6)
        ]}
        out = plan("问题", chat_fn=ScriptedChat([json.dumps(data, ensure_ascii=False)]))
        self.assertEqual(len(out.steps), 4)
        self.assertIn("截断", out.warning)

    def test_empty_or_single_step_fails(self) -> None:
        for data in ({"subquestions": []},
                     {"subquestions": [{"question": "只有一个"}]}):
            out = plan("问题", chat_fn=ScriptedChat([json.dumps(data, ensure_ascii=False)]))
            self.assertIn("规划失败", out.error)

    def test_network_failure_is_not_retried(self) -> None:
        chat = ScriptedChat([None, VALID_PLAN])
        out = plan("问题", chat_fn=chat)
        self.assertEqual(out.steps, [])
        self.assertIn("模型请求失败", out.error)
        self.assertEqual(len(chat.calls), 1)

    def test_parse_json_tolerates_fences_and_prose(self) -> None:
        data = _parse_json('说明\n```json\n{"subquestions": []}\n```')
        self.assertEqual(data, {"subquestions": []})

    def test_reasoning_model_skips_temperature(self) -> None:
        self.assertTrue(_allows_temperature("deepseek-chat"))
        self.assertFalse(_allows_temperature("deepseek-reasoner"))
        self.assertFalse(_allows_temperature("deepseek-flash"))
        self.assertFalse(_allows_temperature("deepseek-v4-pro"))


class EndpointTest(unittest.TestCase):
    """分析模型可以接另一家 OpenAI 兼容服务（如千问）；未配置时完全沿用 LLM 的地址/key。"""

    def test_falls_back_to_llm_endpoint(self) -> None:
        env = {"LLM_BASE_URL": "https://deepseek.example/v1", "LLM_API_KEY": "llm-key"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(_analysis_endpoint(),
                             ("https://deepseek.example/v1", "llm-key"))

    def test_analysis_endpoint_overrides(self) -> None:
        env = {
            "LLM_BASE_URL": "https://deepseek.example/v1", "LLM_API_KEY": "llm-key",
            "ANALYSIS_BASE_URL": "https://dashscope.example/compatible-mode/v1",
            "ANALYSIS_API_KEY": "qwen-key",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(_analysis_endpoint(),
                             ("https://dashscope.example/compatible-mode/v1", "qwen-key"))

    def test_missing_key_is_empty(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_analysis_endpoint(), ("https://api.deepseek.com/v1", ""))


class SqlWarningTest(unittest.TestCase):
    """pay_amount 没过滤 paid 时必须有确定性警告；不能只靠综合器自己发现。"""

    def test_unfiltered_pay_amount_warns(self) -> None:
        sql = "SELECT r.region_name, SUM(o.pay_amount) FROM orders o JOIN regions r ON r.region_id = o.region_id GROUP BY r.region_name"
        self.assertIn("未过滤", _sql_warning(sql))

    def test_where_paid_does_not_warn(self) -> None:
        sql = "SELECT SUM(o.pay_amount) FROM orders o WHERE o.status = 'paid'"
        self.assertEqual(_sql_warning(sql), "")

    def test_case_paid_does_not_warn(self) -> None:
        sql = ("SELECT SUM(CASE WHEN o.status = 'paid' THEN o.pay_amount ELSE 0 END), "
               "SUM(CASE WHEN o.status = 'refunded' THEN 1 ELSE 0 END) FROM orders o")
        self.assertEqual(_sql_warning(sql), "")

    def test_pay_amount_join_items_warns_even_with_paid(self) -> None:
        sql = ("SELECT p.category, SUM(o.pay_amount) FROM orders o "
               "JOIN order_items i ON i.order_id = o.order_id "
               "JOIN products p ON p.product_id = i.product_id "
               "WHERE o.status = 'paid' GROUP BY p.category")
        warning = _sql_warning(sql)
        self.assertIn("不可跨组相加", warning)
        self.assertIn("一致性", warning)

    def test_item_amount_does_not_warn(self) -> None:
        self.assertEqual(_sql_warning("SELECT SUM(i.item_amount) FROM order_items i"), "")


class RepurchaseAggregationTest(unittest.TestCase):
    """#5 实测过的错误：HAVING 落在地区分组上，返回的是地区全部客户，不是复购客户。"""

    def test_region_level_having_is_rejected(self) -> None:
        sql = ("SELECT r.region_name, COUNT(DISTINCT o.customer_id) AS customer_count "
               "FROM orders o JOIN regions r ON r.region_id = o.region_id "
               "WHERE o.status = 'paid' GROUP BY r.region_name "
               "HAVING COUNT(DISTINCT o.order_id) >= 2")
        error = _repurchase_aggregation_error("按地区统计下单次数至少为两单的客户数", sql)
        self.assertIn("HAVING", error)
        self.assertIn("复购", error)

    def test_customer_level_having_is_allowed(self) -> None:
        sql = ("SELECT r.region_name, COUNT(*) AS customer_count FROM ("
               "SELECT o.region_id, o.customer_id FROM orders o WHERE o.status = 'paid' "
               "GROUP BY o.region_id, o.customer_id HAVING COUNT(DISTINCT o.order_id) >= 2"
               ") t JOIN regions r ON r.region_id = t.region_id GROUP BY r.region_name")
        self.assertEqual(_repurchase_aggregation_error("各地区复购客户数", sql), "")

    def test_non_repurchase_question_is_ignored(self) -> None:
        sql = ("SELECT r.region_name, COUNT(DISTINCT o.customer_id) FROM orders o "
               "JOIN regions r ON r.region_id = o.region_id WHERE o.status = 'paid' "
               "GROUP BY r.region_name HAVING COUNT(DISTINCT o.order_id) >= 2")
        self.assertEqual(_repurchase_aggregation_error("各地区的GMV", sql), "")

    def test_step_repair_default_and_override(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_step_repair(), DEFAULT_STEP_MAX_REPAIR)
        with mock.patch.dict(os.environ, {"ANALYSIS_STEP_MAX_REPAIR": "0"}, clear=True):
            self.assertEqual(_step_repair(), 0)
        with mock.patch.dict(os.environ, {"ANALYSIS_STEP_MAX_REPAIR": "9"}, clear=True):
            self.assertEqual(_step_repair(), 2)


class ExecutorTest(unittest.TestCase):
    def _ask(self, question: str, **kwargs):
        self.assertEqual(kwargs["mode"], "single")
        self.assertIsNone(kwargs["session"])
        self.assertEqual(kwargs["role"], "analyst")
        self.assertEqual(kwargs["max_repair"], DEFAULT_STEP_MAX_REPAIR)
        if "失败" in question:
            return SQLPlan(question=question, intent="ask", ok=False,
                           error="no such column: nope", engine="llm", elapsed_ms=7)
        if "澄清" in question:
            return SQLPlan(question=question, intent="clarify", reply="看哪个指标？")
        rows = [(i, f"r{i}") for i in range(30)]
        return SQLPlan(question=question, intent="ask", ok=True, sql="SELECT 1",
                       columns=["n", "name"], rows=rows, engine="llm", elapsed_ms=11)

    def test_two_success_one_failure_one_clarify(self) -> None:
        steps = execute_steps(
            [{"id": "s1", "question": "成功一", "purpose": "p1"},
             {"id": "s2", "question": "失败一", "purpose": "p2"},
             {"id": "s3", "question": "成功二", "purpose": "p3"},
             {"id": "s4", "question": "澄清一", "purpose": "p4"}],
            ask_fn=self._ask, catalog=None, retriever=None, role="analyst",
            preview_rows=3,
        )
        self.assertEqual([s.id for s in steps], ["s1", "s2", "s3", "s4"])
        self.assertTrue(steps[0].ok)
        self.assertEqual(steps[0].row_count, 30)
        self.assertEqual(len(steps[0].rows), 3)  # UI 预览截断
        self.assertFalse(steps[1].ok)
        self.assertEqual(steps[1].error, "no such column: nope")
        self.assertTrue(steps[2].ok)
        self.assertTrue(steps[3].skipped)
        self.assertEqual(steps[3].intent, "clarify")
        self.assertTrue(steps[3].error)

    def test_budget_exhausted_skips_without_calling_ask(self) -> None:
        calls = []

        def fake(question, **kwargs):
            calls.append(question)
            return SQLPlan(question=question, intent="ask", ok=True, engine="llm")

        steps = execute_steps(
            [{"question": "一"}, {"question": "二"}],
            ask_fn=fake, catalog=None, retriever=None, role="analyst",
            total_budget_s=-1,
        )
        self.assertEqual(calls, [])
        self.assertTrue(all(s.skipped for s in steps))
        self.assertTrue(all("预算" in s.error for s in steps))

    def test_single_step_exception_does_not_break_run(self) -> None:
        def fake(question, **kwargs):
            if "炸" in question:
                raise RuntimeError("boom")
            return SQLPlan(question=question, intent="ask", ok=True, engine="llm")

        steps = execute_steps(
            [{"question": "炸"}, {"question": "好"}],
            ask_fn=fake, catalog=None, retriever=None, role="analyst",
        )
        self.assertTrue(steps[0].skipped)
        self.assertIn("boom", steps[0].error)
        self.assertTrue(steps[1].ok)

    def test_executor_without_plan_marks_skipped(self) -> None:
        steps = execute_steps(
            [{"question": "一"}, {"question": "二"}],
            ask_fn=lambda *a, **k: None, catalog=None, retriever=None, role="analyst",
        )
        self.assertTrue(all(s.skipped for s in steps))
        self.assertTrue(all("没有返回结果" in s.error for s in steps))

    def test_step_warning_is_recorded(self) -> None:
        def fake(question, **kwargs):
            return SQLPlan(question=question, intent="ask", ok=True,
                           sql="SELECT SUM(o.pay_amount) FROM orders o",
                           columns=["gmv"], rows=[(1,)], engine="llm")

        steps = execute_steps(
            [{"question": "华东GMV"}],
            ask_fn=fake, catalog=None, retriever=None, role="analyst",
        )
        self.assertIn("未过滤", steps[0].warning)

    def test_region_level_repurchase_having_is_marked_invalid(self) -> None:
        sql = ("SELECT r.region_name, COUNT(DISTINCT o.customer_id) AS customer_count "
               "FROM orders o JOIN regions r ON r.region_id = o.region_id "
               "WHERE o.status = 'paid' GROUP BY r.region_name "
               "HAVING COUNT(DISTINCT o.order_id) >= 2")

        def fake(question, **kwargs):
            return SQLPlan(question=question, intent="ask", ok=True, sql=sql,
                           columns=["region_name", "customer_count"],
                           rows=[("华东", 28), ("华北", 33)], engine="llm")

        steps = execute_steps(
            [{"question": "按地区统计下单次数至少为两单的客户数"}],
            ask_fn=fake, catalog=None, retriever=None, role="analyst",
        )
        self.assertFalse(steps[0].ok)
        self.assertIn("HAVING", steps[0].error)
        self.assertIn("复购", steps[0].error)
        self.assertEqual(steps[0].sql, sql)  # SQL 仍保留，界面上能看到为什么不可用

    def test_customer_level_repurchase_having_stays_ok(self) -> None:
        sql = ("SELECT r.region_name, COUNT(*) AS customer_count FROM ("
               "SELECT o.region_id, o.customer_id FROM orders o WHERE o.status = 'paid' "
               "GROUP BY o.region_id, o.customer_id HAVING COUNT(DISTINCT o.order_id) >= 2"
               ") t JOIN regions r ON r.region_id = t.region_id GROUP BY r.region_name")

        def fake(question, **kwargs):
            return SQLPlan(question=question, intent="ask", ok=True, sql=sql,
                           columns=["region_name", "customer_count"],
                           rows=[("华东", 27)], engine="llm")

        steps = execute_steps(
            [{"question": "按地区统计下单次数至少为两单的客户数"}],
            ask_fn=fake, catalog=None, retriever=None, role="analyst",
        )
        self.assertTrue(steps[0].ok)
        self.assertEqual(steps[0].error, "")

    def test_budget_counts_from_started_at(self) -> None:
        calls = []

        def fake(question, **kwargs):
            calls.append(question)
            return SQLPlan(question=question, intent="ask", ok=True, engine="llm")

        steps = execute_steps(
            [{"question": "一"}, {"question": "二"}],
            ask_fn=fake, catalog=None, retriever=None, role="analyst",
            total_budget_s=1, started_at=time.monotonic() - 100,
        )
        self.assertEqual(calls, [])
        self.assertTrue(all(s.skipped for s in steps))


class SynthesizerTest(unittest.TestCase):
    def _steps(self) -> list[AnalysisStep]:
        return [
            AnalysisStep(id="s1", question="各地区GMV", ok=True,
                         columns=["region", "gmv"], rows=[("华东", 1.0)], row_count=1),
            AnalysisStep(id="s2", question="各地区退款率", ok=False, error="timeout"),
        ]

    def test_answer_must_cite_and_limitations_are_added(self) -> None:
        chat = ScriptedChat(["华东 GMV 最高 [s1]。"])
        answer, error, _ = synthesize("问题", self._steps(), chat_fn=chat)
        self.assertEqual(error, "")
        self.assertIn("[s1]", answer)
        self.assertIn("局限", answer)
        self.assertIn("[s2]", answer)  # 模型没写失败步骤，代码补上，不能隐藏

    def test_no_citation_is_rejected_after_retry(self) -> None:
        chat = ScriptedChat(["华东最高，但没标来源。", "还是没有来源。"])
        answer, error, _ = synthesize("问题", self._steps(), chat_fn=chat)
        self.assertEqual(answer, "")
        self.assertIn("未引用", error)
        self.assertEqual(len(chat.calls), 2)

    def test_no_evidence_does_not_call_model(self) -> None:
        def bomb(messages, model=None):
            raise AssertionError("没有证据时不该调模型")

        steps = [AnalysisStep(id="s1", question="查不到", error="timeout")]
        answer, error, usage = synthesize("问题", steps, chat_fn=bomb)
        self.assertIn("无法判断", answer)
        self.assertIn("[s1]", answer)
        self.assertEqual(error, "")
        self.assertEqual(usage, {})

    def test_invalid_reference_is_rejected(self) -> None:
        steps = [AnalysisStep(id="s1", question="q", ok=True, rows=[(1,)])]
        chat = ScriptedChat(["结论 [s9]。", "结论 [s9]。"])
        answer, error, _ = synthesize("问题", steps, chat_fn=chat)
        self.assertEqual(answer, "")
        self.assertIn("未引用", error)

    def test_model_failure_returns_error_not_fake_answer(self) -> None:
        chat = ScriptedChat([None])
        answer, error, _ = synthesize("问题", self._steps(), chat_fn=chat)
        self.assertEqual(answer, "")
        self.assertIn("模型请求失败", error)

    def test_warning_is_appended_even_if_model_ignores_it(self) -> None:
        steps = [AnalysisStep(id="s1", question="GMV", ok=True, rows=[(1,)],
                              warning="SQL 含 pay_amount 但未过滤 status='paid'")]
        answer, error, _ = synthesize("问题", steps, chat_fn=ScriptedChat(["结论 [s1]。"]))
        self.assertEqual(error, "")
        self.assertIn("口径提醒", answer)
        self.assertIn("[s1]", answer)


class RunAnalysisTest(unittest.TestCase):
    def test_happy_path_records_steps_evidence_and_usage(self) -> None:
        n = {"i": 0}

        def chat(messages, model=None):
            n["i"] += 1
            if "规划器" in messages[0]["content"]:
                return _reply(VALID_PLAN, prompt=10, completion=5)
            return _reply("华东 GMV 最高 [s1]。", prompt=20, completion=8)

        def fake_ask(question, **kwargs):
            if "退款率" in question:
                return SQLPlan(question=question, intent="ask", ok=False,
                               error="timeout", engine="llm")
            return SQLPlan(question=question, intent="ask", ok=True, sql="SELECT 1",
                           columns=["n"], rows=[(1,)], engine="llm", elapsed_ms=3)

        result = run_analysis(
            "各地区的GMV和退款率", role="analyst", catalog=None,
            ask_fn=fake_ask, chat_fn=chat,
        )
        self.assertTrue(result.planner_ok)
        self.assertEqual([s.id for s in result.steps], ["s1", "s2"])
        self.assertEqual(result.evidence, ["s1"])
        self.assertIn("[s1]", result.answer)
        self.assertIn("[s2]", result.answer)  # 失败步骤进局限
        self.assertEqual(result.error, "")
        self.assertEqual(result.usage["prompt_tokens"], 30)
        self.assertEqual(result.usage["completion_tokens"], 13)
        self.assertEqual(result.warning, "")

    def test_replans_once_when_all_steps_fail(self) -> None:
        counts = {"plan": 0, "ask": 0}

        def chat(messages, model=None):
            if "规划器" in messages[0]["content"]:
                counts["plan"] += 1
                return _reply(VALID_PLAN, prompt=10, completion=5)
            return _reply("重规划后的结论 [s1][s2]。", prompt=20, completion=8)

        def fake_ask(question, **kwargs):
            counts["ask"] += 1
            if counts["ask"] <= 2:
                return SQLPlan(question=question, intent="ask", ok=False,
                               error="first plan failed", engine="llm")
            return SQLPlan(question=question, intent="ask", ok=True, sql="SELECT 1",
                           columns=["n"], rows=[(1,)], engine="llm", elapsed_ms=3)

        result = run_analysis(
            "各地区的GMV和退款率", role="analyst", catalog=None,
            ask_fn=fake_ask, chat_fn=chat,
        )
        self.assertEqual(counts["plan"], 2)  # 首次 + 重规划各一次
        self.assertEqual(counts["ask"], 4)
        self.assertEqual(result.evidence, ["s1", "s2"])
        self.assertIn("重规划", result.warning)
        self.assertIn("重规划后的结论", result.answer)
        self.assertEqual(result.usage["prompt_tokens"], 40)  # 两次 Planner + 一次综合

    def test_all_steps_fail_keeps_first_plan_if_replan_not_better(self) -> None:
        plans = {"n": 0}

        def fake_ask(question, **kwargs):
            return SQLPlan(question=question, intent="ask", ok=False, error="boom")

        def chat(messages, model=None):
            if "规划器" in messages[0]["content"]:
                plans["n"] += 1
            return _reply(VALID_PLAN)

        result = run_analysis(
            "问题", role="analyst", catalog=None,
            ask_fn=fake_ask, chat_fn=chat,
        )
        self.assertEqual(plans["n"], 2)  # 试过一次，但没变得更好
        self.assertEqual(result.warning, "")  # 没替换计划就不提示“已重规划”
        self.assertEqual(result.evidence, [])
        self.assertIn("无法判断", result.answer)
        self.assertIn("[s1]", result.answer)

    def test_planner_failure_sets_planner_ok_false(self) -> None:
        result = run_analysis(
            "问题", role="analyst", catalog=None,
            ask_fn=lambda *a, **k: None, chat_fn=ScriptedChat([None]),
        )
        self.assertFalse(result.planner_ok)
        self.assertEqual(result.steps, [])
        self.assertIn("规划失败", result.error)

    def test_budget_exhausted_skips_steps_and_synthesis(self) -> None:
        def fake_ask(question, **kwargs):
            raise AssertionError("预算耗尽时不该执行步骤")

        result = run_analysis(
            "问题", role="analyst", catalog=None,
            ask_fn=fake_ask, chat_fn=ScriptedChat([VALID_PLAN]),
            total_budget_s=-1,
        )
        self.assertTrue(result.planner_ok)
        self.assertTrue(all(step.skipped for step in result.steps))
        self.assertEqual(result.answer, "")
        self.assertIn("预算", result.error)

    def test_replan_needs_a_model_call_of_budget_left(self) -> None:
        # 剩余预算不够一次模型调用时不重规划：重规划会把综合阶段的时间吃光，
        # 用户从“有结论但带局限”退化成“没有结论”。
        with mock.patch("app.analysis._model_timeout", return_value=30.0):
            self.assertTrue(_can_replan(0.0, 60.0))
            self.assertTrue(_can_replan(29.0, 60.0))
            self.assertFalse(_can_replan(31.0, 60.0))
            self.assertFalse(_can_replan(0.0, 10.0))
            self.assertFalse(_can_replan(0.0, -1.0))

    def test_replan_skipped_when_budget_is_too_tight(self) -> None:
        plans = {"n": 0}

        def chat(messages, model=None):
            if "规划器" in messages[0]["content"]:
                plans["n"] += 1
                return _reply(VALID_PLAN)
            return _reply("结论 [s1]。", prompt=5, completion=1)

        def fake_ask(question, **kwargs):
            return SQLPlan(question=question, intent="ask", ok=False, error="boom")

        with mock.patch("app.analysis._model_timeout", return_value=30.0):
            result = run_analysis(
                "各地区的GMV和退款率", role="analyst", catalog=None,
                ask_fn=fake_ask, chat_fn=chat, total_budget_s=10,
            )
        self.assertEqual(plans["n"], 1)  # 只规划一次
        self.assertEqual(result.warning, "")

    def test_replan_execution_pass_leaves_time_for_synthesis(self) -> None:
        budgets: list[float] = []

        def fake_execute(subquestions, **kwargs):
            budgets.append(kwargs["total_budget_s"])
            return [AnalysisStep(id="s1", question="q", error="boom")]

        with mock.patch("app.analysis.execute_steps", side_effect=fake_execute), \
                mock.patch("app.analysis._model_timeout", return_value=30.0):
            result = run_analysis(
                "各地区的GMV和退款率", role="analyst", catalog=None,
                ask_fn=lambda *a, **k: None, chat_fn=ScriptedChat([VALID_PLAN]),
                total_budget_s=60,
            )
        self.assertEqual(budgets, [60, 30])  # 第二轮把 30s 留给综合
        self.assertEqual(result.warning, "")  # 第二次没更好，不替换也不提示


class AgentAnalysisModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()

    def test_analysis_returns_steps_answer_evidence(self) -> None:
        result = AnalysisResult(
            steps=[AnalysisStep(id="s1", question="各地区的GMV", ok=True,
                                sql="SELECT 1", columns=["n"], rows=[(1,)], row_count=1)],
            answer="一共有 1 个地区 [s1]。",
            evidence=["s1"],
            usage={"prompt_tokens": 1, "completion_tokens": 2},
        )
        with mock.patch("app.analysis.run_analysis", return_value=result):
            plan = ask("各地区的GMV", catalog=self.catalog, mode="analysis")
        self.assertEqual(plan.mode, "analysis")
        self.assertEqual(plan.engine, "analysis")
        self.assertEqual(plan.intent, "ask")
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.answer, result.answer)
        self.assertEqual(plan.evidence, ["s1"])
        self.assertTrue(plan.ok)
        self.assertEqual(plan.usage["completion_tokens"], 2)

    def test_planner_failure_falls_back_to_single_sql(self) -> None:
        failed = AnalysisResult(planner_ok=False, error="规划失败：坏 JSON")
        generator = ScriptedGenerator(["SELECT COUNT(*) AS n FROM orders"])
        with mock.patch("app.analysis.run_analysis", return_value=failed):
            plan = ask("各地区的GMV", catalog=self.catalog, mode="analysis",
                       generator=generator)
        self.assertEqual(plan.mode, "single")
        self.assertIn("规划失败", plan.analysis_error)
        self.assertTrue(plan.ok)
        self.assertIn("COUNT", plan.sql)

    def test_offline_analysis_falls_back_without_model_call(self) -> None:
        with mock.patch("app.agent.llm_sql", side_effect=AssertionError("不该调模型")):
            plan = ask("各地区的GMV", catalog=self.catalog, mode="analysis", use_llm=False)
        self.assertEqual(plan.mode, "single")
        self.assertIn("离线", plan.analysis_error)
        self.assertTrue(plan.ok)
        self.assertEqual(plan.engine, "rule")

    def test_unknown_mode_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ask("各地区的GMV", catalog=self.catalog, mode="react")

    def test_analysis_clarify_uses_reply_as_answer(self) -> None:
        # 分析模式下也不给澄清问题硬跑步骤：mode 保持 analysis，answer 就是话术
        plan = ask("看下业绩", catalog=self.catalog, mode="analysis", use_llm=False)
        self.assertEqual(plan.mode, "analysis")
        self.assertEqual(plan.intent, "clarify")
        self.assertEqual(plan.answer, plan.reply)
        self.assertEqual(plan.steps, [])


class SemanticContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()

    def test_question_semantics_are_single_source(self) -> None:
        """占比/复购判定只放在 app.semantics：两个模块不能对同一句判出不同结论。"""
        from app.semantics import is_repurchase_question, is_share_question

        self.assertTrue(is_share_question("各地区销量占全国比例"))
        self.assertTrue(is_share_question("各品牌销售额占比"))
        self.assertFalse(is_share_question("各地区的GMV"))
        for question in ("复购客户数", "回头客有多少", "下过至少两单的客户数按地区",
                         "至少两单的客户", "两单以上的客户"):
            with self.subTest(question=question):
                self.assertTrue(is_repurchase_question(question))
        self.assertFalse(is_repurchase_question("有多少客户"))
        self.assertFalse(is_repurchase_question("GMV超过3万的客户按地区统计人数"))

    def test_semantic_metadata_and_repurchase_table(self) -> None:
        self.assertEqual(self.catalog.metric("gmv").grain, "order")
        self.assertTrue(self.catalog.metric("gmv").additive)
        self.assertIn("不能直接相加", self.catalog.metric("gmv").warning)
        self.assertFalse(self.catalog.metric("aov").additive)
        table = self.catalog.table("repurchase_customers")
        self.assertIsNotNone(table)
        self.assertEqual([c.name for c in table.columns], ["customer_id", "region_id", "order_count"])

    def test_semantic_fields_tolerate_handwritten_json(self) -> None:
        # 口径字段是手写 JSON：字符串 "false" / null 不能被当成"可加"或渲染成 "None"
        raw = {
            "tables": [{"name": "t", "columns": [{"name": "c", "type": "INTEGER"}]}],
            "metrics": [
                {"name": "ratio", "sql": "1", "additive": "false", "grain": None,
                 "warning": None},
                {"name": "plain", "sql": "2"},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            catalog = load_catalog(path)
        self.assertFalse(catalog.metric("ratio").additive)
        self.assertEqual(catalog.metric("ratio").grain, "")
        self.assertEqual(catalog.metric("ratio").warning, "")
        self.assertTrue(catalog.metric("plain").additive)

    def test_executor_passes_hint_separately_from_question(self) -> None:
        """执行约束必须走 ask(hint=...)，不能拼进子问题文本。

        拼进问题会让 ask 用这句做 extract_keywords / 检索 / 形状守卫：
        "占比口径…按品牌/品类拆分…" 会被当成新维度，同一句问题抽出不同的指标。
        """
        captured: list[tuple[str, dict]] = []

        def fake_ask(question, **kwargs):
            captured.append((question, kwargs))
            return SQLPlan(question=question, intent="ask", ok=True, sql="SELECT 1",
                           columns=["n"], rows=[(1,)], engine="llm")

        execute_steps(
            [{"question": "各品牌销售额占该地区比例", "hint": "占比口径：分子分母同源"}],
            ask_fn=fake_ask, catalog=self.catalog, retriever=None, role="analyst",
        )
        question, kwargs = captured[0]
        self.assertEqual(question, "各品牌销售额占该地区比例")  # 问题保持干净
        self.assertNotIn("占比口径", question)
        self.assertEqual(kwargs["hint"], "占比口径：分子分母同源")  # 约束走独立通道

    def test_planner_entry_attaches_hint_to_context_not_question(self) -> None:
        from app import agent as agent_mod

        captured: dict = {}

        def fake_llm_sql(question, ctx, catalog, feedback=None):
            captured["question"] = question
            captured["instructions"] = ctx.instructions
            return "SELECT 1"

        with mock.patch.object(agent_mod, "llm_sql", side_effect=fake_llm_sql):
            plan_obj = ask("各品牌销售额占该地区比例", catalog=self.catalog,
                           hint="占比口径：分子分母同源")
        self.assertEqual(plan_obj.question, "各品牌销售额占该地区比例")
        self.assertEqual(captured["question"], "各品牌销售额占该地区比例")
        self.assertEqual(captured["instructions"], ("占比口径：分子分母同源",))

    def test_instructions_block_is_omitted_when_empty(self) -> None:
        from app.agent import _instructions_block
        from app.domain import SchemaContext

        self.assertEqual(_instructions_block(SchemaContext()), "")
        block = _instructions_block(SchemaContext(instructions=("口径 A", "口径 B")))
        self.assertIn("不是用户问题的一部分", block)
        self.assertIn("- 口径 A", block)
        self.assertIn("- 口径 B", block)

    def test_conditional_rules_only_mention_tables_in_context(self) -> None:
        """prompt 只描述本次上下文里存在的对象：复购表不在 ctx 里就不许提它。"""
        from app.agent import _conditional_rules
        from app.domain import Hit, SchemaContext

        without = SchemaContext()
        with_table = SchemaContext(tables=[Hit(kind="table", key="repurchase_customers",
                                              score=0.0, payload={})])

        # 普通问题 + 表没进上下文：不提复购表
        self.assertNotIn("repurchase_customers", _conditional_rules("各地区的GMV", without))
        # 表在上下文里：才提
        self.assertIn("repurchase_customers",
                      _conditional_rules("各地区的GMV", with_table))
        # 占比规则只看问题
        self.assertIn("份额/占比", _conditional_rules("各地区销量占全国比例", without))
        self.assertNotIn("份额/占比", _conditional_rules("各地区的GMV", without))

    def test_conditional_rules_dedupe_share_hint(self) -> None:
        from app.agent import _conditional_rules
        from app.domain import SchemaContext
        from app.semantics import DEFAULT_SHARE_INSTRUCTION

        ctx = SchemaContext(instructions=(DEFAULT_SHARE_INSTRUCTION,))
        self.assertNotIn(DEFAULT_SHARE_INSTRUCTION,
                         _conditional_rules("各地区销量占全国比例", ctx, self.catalog))
        # 没有 hint 时仍然要补规则
        self.assertIn(DEFAULT_SHARE_INSTRUCTION,
                      _conditional_rules("各地区销量占全国比例", SchemaContext(), self.catalog))

    def test_replans_only_when_no_step_is_usable(self) -> None:
        from app.analysis import _needs_replan

        self.assertFalse(_needs_replan([]))
        self.assertTrue(_needs_replan([AnalysisStep(id="s1", question="q", error="boom"),
                                       AnalysisStep(id="s2", question="q", skipped=True)]))
        self.assertFalse(_needs_replan([AnalysisStep(id="s1", question="q", ok=True),
                                        AnalysisStep(id="s2", question="q", error="boom")]))
        # 全部成功（哪怕带口径警告）也不重规划：计划能用，不该白烧一次
        self.assertFalse(_needs_replan([AnalysisStep(id="s1", question="q", ok=True,
                                                     warning="口径警告")]))


if __name__ == "__main__":
    unittest.main()
