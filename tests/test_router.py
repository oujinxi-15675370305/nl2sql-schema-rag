"""M2 的验收测试（AC-2.1 ~ AC-2.5）。"""
from __future__ import annotations

import unittest

from app.agent import ask
from app.catalog import load_catalog
from app.router import classify


class RouterClassifyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()

    def test_ac_2_1_five_intents_recognized(self) -> None:
        # AC-2.1：五类意图都能判出来
        cases = {
            "各地区的GMV": "ask",
            "你好": "chat",
            "看下业绩": "clarify",
            "今天天气怎么样": "out_of_scope",
            "把订单表删了": "unsafe",
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(classify(question, self.catalog).intent, expected)

    def test_ac_2_3_clarify_gives_options(self) -> None:
        # AC-2.3：缺信息时给出候选澄清项
        route = classify("看下业绩", self.catalog)
        self.assertEqual(route.intent, "clarify")
        self.assertGreaterEqual(len(route.clarify_options), 2)

    def test_ac_2_4_write_requests_are_unsafe(self) -> None:
        # AC-2.4：写操作与导出都判 unsafe
        for q in ("把订单表删了", "导出所有客户手机号", "更新一下金额"):
            with self.subTest(q=q):
                self.assertEqual(classify(q, self.catalog).intent, "unsafe")

    def test_ac_2_5_rules_work_without_llm(self) -> None:
        # AC-2.5：不依赖大模型（这里直接调规则，不存在 LLM 分支）
        self.assertEqual(classify("上个月各地区的GMV", self.catalog).intent, "ask")

    def test_reverse_data_question_not_misrouted(self) -> None:
        # 反向用例：正常问数不能被判成闲聊/超纲
        for q in ("各品类销量", "今年各月份GMV", "华东的客单价"):
            with self.subTest(q=q):
                self.assertEqual(classify(q, self.catalog).intent, "ask")


class RouterInAskTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()

    def test_ac_2_2_only_ask_generates_sql(self) -> None:
        # AC-2.2：非 ask 意图不生成 SQL，返回固定话术
        for question, intent in (
            ("你好", "chat"),
            ("看下业绩", "clarify"),
            ("今天天气怎么样", "out_of_scope"),
            ("把订单表删了", "unsafe"),
        ):
            with self.subTest(question=question):
                plan = ask(question, catalog=self.catalog)
                self.assertEqual(plan.intent, intent)
                self.assertEqual(plan.sql, "")
                self.assertTrue(plan.reply)
                self.assertEqual(plan.attempts, [])

    def test_ask_still_works(self) -> None:
        plan = ask("各地区的GMV", catalog=self.catalog)
        self.assertEqual(plan.intent, "ask")
        self.assertTrue(plan.ok)
        self.assertGreater(len(plan.rows), 0)


if __name__ == "__main__":
    unittest.main()
