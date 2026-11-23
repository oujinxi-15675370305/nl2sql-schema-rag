"""分析模式（Step 2）：Planner / Executor / Synthesizer 的离线测试。

这里**不碰真实模型**：`chat_fn` 和 `ask_fn` 都是脚本化的。重点钉住四件事：
1. 规划坏 JSON / 空步骤 / 超过 4 步时怎么降级；
2. 每步复用 ask 的形状（mode=single、session=None），失败/澄清不硬跑；
3. 综合结论必须引用步骤结果，失败步骤由代码补进“局限”，无证据时直接说无法判断；
4. `agent.ask(mode="analysis")` 的返回结构与回退单 SQL 的路径。
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from app.agent import ScriptedGenerator, ask
from app.analysis import (
    ChatResult,
    _allows_temperature,
    _parse_json,
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


class ExecutorTest(unittest.TestCase):
    def _ask(self, question: str, **kwargs):
        self.assertEqual(kwargs["mode"], "single")
        self.assertIsNone(kwargs["session"])
        self.assertEqual(kwargs["role"], "analyst")
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

    def test_planner_failure_sets_planner_ok_false(self) -> None:
        result = run_analysis(
            "问题", role="analyst", catalog=None,
            ask_fn=lambda *a, **k: None, chat_fn=ScriptedChat([None]),
        )
        self.assertFalse(result.planner_ok)
        self.assertEqual(result.steps, [])
        self.assertIn("规划失败", result.error)

    def test_all_steps_fail_yields_cannot_judge(self) -> None:
        def fake_ask(question, **kwargs):
            return SQLPlan(question=question, intent="ask", ok=False, error="boom")

        def chat(messages, model=None):
            return _reply(VALID_PLAN)

        result = run_analysis(
            "问题", role="analyst", catalog=None,
            ask_fn=fake_ask, chat_fn=chat,
        )
        self.assertEqual(result.evidence, [])
        self.assertIn("无法判断", result.answer)
        self.assertIn("[s1]", result.answer)


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


if __name__ == "__main__":
    unittest.main()
