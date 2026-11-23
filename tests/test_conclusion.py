"""Step 3 自动结论：只测提示词/校验/接线，不碰真实模型。"""
from __future__ import annotations

import unittest
from unittest import mock

from app.agent import ask
from app.catalog import load_catalog
from app.conclusion import ChatResult, summarize

GOOD_SQL = "SELECT COUNT(*) AS n FROM orders WHERE status = 'paid'"


class _Chat:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, messages, model=None):
        self.calls.append(messages)
        value = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        if value is None or isinstance(value, ChatResult):
            return value
        return ChatResult(text=str(value), prompt_tokens=1, completion_tokens=2)


class SummarizeTest(unittest.TestCase):
    def test_basic_answer(self) -> None:
        chat = _Chat(["已支付订单共 666 单。"])
        answer, error, usage = summarize("一共多少单", ["n"], [(666,)], chat_fn=chat)
        self.assertEqual(error, "")
        self.assertEqual(answer, "已支付订单共 666 单。")
        self.assertEqual(usage["completion_tokens"], 2)
        prompt = chat.calls[0][-1]["content"]
        self.assertIn("666", prompt)
        self.assertIn("一共多少单", prompt)

    def test_no_rows_does_not_call_model(self) -> None:
        def bomb(messages, model=None):
            raise AssertionError("空结果不该调模型")

        answer, error, usage = summarize("多少单", ["n"], [], chat_fn=bomb)
        self.assertEqual(answer, "")
        self.assertIn("没有结果", error)
        self.assertEqual(usage, {})

    def test_model_failure(self) -> None:
        answer, error, _ = summarize("多少单", ["n"], [(1,)], chat_fn=_Chat([None]))
        self.assertEqual(answer, "")
        self.assertIn("模型请求失败", error)

    def test_empty_too_short_and_too_long(self) -> None:
        for text, expected in (("", "过短"), ("短", "过短"), ("长" * 601, "超过")):
            answer, error, _ = summarize("多少单", ["n"], [(1,)],
                                         chat_fn=_Chat([text]))
            self.assertEqual(answer, "")
            self.assertIn(expected, error)

    def test_code_fence_is_stripped(self) -> None:
        answer, error, _ = summarize("多少单", ["n"], [(1,)],
                                     chat_fn=_Chat(["```\n共 1 单。\n```"]))
        self.assertEqual(error, "")
        self.assertEqual(answer, "共 1 单。")

    def test_truncated_rows_are_marked(self) -> None:
        chat = _Chat(["共 60 行。"])
        rows = [(i,) for i in range(60)]
        answer, error, _ = summarize("有多少", ["n"], rows, chat_fn=chat)
        self.assertEqual(error, "")
        prompt = chat.calls[0][-1]["content"]
        self.assertIn("共 60 行", prompt)
        self.assertIn("截断", prompt)


class AskConclusionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()

    def test_disabled_by_default_does_not_call_summarize(self) -> None:
        with mock.patch("app.agent.llm_sql", return_value=GOOD_SQL), \
                mock.patch("app.conclusion.summarize",
                           side_effect=AssertionError("默认不该生成结论")):
            plan = ask("多少单", catalog=self.catalog)
        self.assertTrue(plan.ok)
        self.assertEqual(plan.answer, "")
        self.assertFalse(plan.conclusion)

    def test_enabled_calls_summarize_and_keeps_sql_result(self) -> None:
        with mock.patch("app.agent.llm_sql", return_value=GOOD_SQL), \
                mock.patch("app.conclusion.summarize",
                           return_value=("已支付订单共 666 单。", "", {"prompt_tokens": 3,
                                                                   "completion_tokens": 4})):
            plan = ask("多少单", catalog=self.catalog, conclusion=True)
        self.assertTrue(plan.ok)
        self.assertTrue(plan.conclusion)
        self.assertEqual(plan.answer, "已支付订单共 666 单。")
        self.assertEqual(plan.rows[0][0], 666)
        self.assertEqual(plan.conclusion_usage["completion_tokens"], 4)

    def test_conclusion_failure_does_not_fail_sql(self) -> None:
        with mock.patch("app.agent.llm_sql", return_value=GOOD_SQL), \
                mock.patch("app.conclusion.summarize",
                           return_value=("", "结论生成失败：模型请求失败", {})):
            plan = ask("多少单", catalog=self.catalog, conclusion=True)
        self.assertTrue(plan.ok)
        self.assertTrue(plan.conclusion)
        self.assertEqual(plan.answer, "")
        self.assertIn("模型请求失败", plan.conclusion_error)

    def test_rule_fallback_skips_conclusion(self) -> None:
        with mock.patch("app.conclusion.summarize",
                        side_effect=AssertionError("规则 fallback 不该调结论模型")):
            plan = ask("多少单", catalog=self.catalog, use_llm=False, conclusion=True)
        self.assertTrue(plan.ok)
        self.assertEqual(plan.engine, "rule")
        self.assertFalse(plan.conclusion)
        self.assertIn("规则 fallback", plan.conclusion_error)

    def test_empty_result_skips_conclusion(self) -> None:
        empty_sql = "SELECT order_id FROM orders WHERE 1 = 0"
        with mock.patch("app.agent.llm_sql", return_value=empty_sql), \
                mock.patch("app.conclusion.summarize",
                           side_effect=AssertionError("空结果不该调结论模型")):
            plan = ask("多少单", catalog=self.catalog, conclusion=True)
        self.assertTrue(plan.ok)
        self.assertFalse(plan.conclusion)
        self.assertIn("没有结果", plan.conclusion_error)


if __name__ == "__main__":
    unittest.main()
