"""M2 的验收测试（AC-2.1 ~ AC-2.5）。

M3 之后 `classify()` 不再自己抽槽位，测试按生产顺序先抽再判（见 `route()`）。
"""
from __future__ import annotations

import unittest

from app.agent import ask
from app.catalog import load_catalog
from app.domain import Catalog
from app.router import Route, classify
from app.slots import extract_keywords


def route(question: str, catalog: Catalog) -> Route:
    """生产路径：槽位先算好，再交给 classify（classify 内部不再抽取）。"""
    return classify(question, catalog, extract_keywords(question, catalog))


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
                self.assertEqual(route(question, self.catalog).intent, expected)

    def test_ac_2_3_clarify_gives_options(self) -> None:
        # AC-2.3：缺信息时给出候选澄清项
        r = route("看下业绩", self.catalog)
        self.assertEqual(r.intent, "clarify")
        self.assertGreaterEqual(len(r.clarify_options), 2)
        self.assertTrue(any("指标" in o for o in r.clarify_options))

    def test_ac_2_3_metric_synonyms_are_not_misrouted(self) -> None:
        # 回归：指标用的是同义词时必须判 ask，不能因为词表对不上就要求澄清
        for q in ("查一下上个月卖了多少", "查一下华东的订单量", "查一下退货率", "看一下各地区的交易额"):
            with self.subTest(q=q):
                self.assertEqual(route(q, self.catalog).intent, "ask")

    def test_ac_2_3_unresolved_metric_asks_clarify(self) -> None:
        # 反向用例：抽不到指标就澄清，不能默默挑一个指标去算
        # （"有多少客户"以前是这里的主力样例；M4 前置加了 customer_count 指标后它已经是 ask）
        for q in ("这个月表现怎么样", "看下业绩", "经营情况"):
            with self.subTest(q=q):
                r = route(q, self.catalog)
                self.assertEqual(r.intent, "clarify")
                self.assertGreaterEqual(len(r.clarify_options), 2)

    def test_customer_questions_route_to_ask(self) -> None:
        # M4 前置：客户数成为正式指标，客户相关问题不再一律澄清
        for q in ("有多少客户", "客户数量", "各地区的客户数", "每个客户的GMV"):
            with self.subTest(q=q):
                self.assertEqual(route(q, self.catalog).intent, "ask")

    def test_ac_2_4_attributive_write_words_are_not_unsafe(self) -> None:
        # 反向用例："被删除的订单""更新时间"是读查询，不能拦成 unsafe
        for q in ("更新时间分布", "被删除的订单有多少"):
            with self.subTest(q=q):
                self.assertNotEqual(route(q, self.catalog).intent, "unsafe")

    def test_ac_2_4_rewrite_phrase_is_not_unsafe(self) -> None:
        # 回归："改成华南"是 M3 的改写问句；只有"把/将 … 改成"才是改数据
        for q in ("改成华南", "帮我改成华南"):
            with self.subTest(q=q):
                self.assertNotEqual(route(q, self.catalog).intent, "unsafe")
        for q in ("把金额改成100", "把订单状态改为已退款"):
            with self.subTest(q=q):
                self.assertEqual(route(q, self.catalog).intent, "unsafe")

    def test_ac_2_4_deleted_attribute_is_not_unsafe(self) -> None:
        # 回归：正则回溯到单字"删"时不能绕过"删除的"豁免（"已删除的订单"）
        self.assertNotEqual(route("已删除的订单有多少", self.catalog).intent, "unsafe")

    def test_ac_2_1_chat_requires_whole_sentence(self) -> None:
        # 回归：带客套前缀的数据问题不能被判成闲聊
        for q in ("你好", "谢谢", "在吗"):
            with self.subTest(q=q):
                self.assertEqual(route(q, self.catalog).intent, "chat")
        for q in ("好的，各地区的GMV", "你好，各地区的GMV", "which gmv"):
            with self.subTest(q=q):
                self.assertEqual(route(q, self.catalog).intent, "ask")

    def test_ac_2_1_empty_input(self) -> None:
        # 反向用例：空输入/纯标点走闲聊话术，不能抛异常也不能去生成 SQL
        for q in ("", "   ", "？？"):
            with self.subTest(q=q):
                self.assertEqual(route(q, self.catalog).intent, "chat")

    def test_schema_question_is_out_of_scope(self) -> None:
        # 问表结构本身不是问数，且话术指向 /api/schema
        r = route("订单表结构是什么样", self.catalog)
        self.assertEqual(r.intent, "out_of_scope")
        self.assertIn("schema", r.reply)

    def test_ac_2_4_write_requests_are_unsafe(self) -> None:
        # AC-2.4：写操作与导出都判 unsafe
        for q in ("把订单表删了", "导出所有客户手机号", "更新一下金额"):
            with self.subTest(q=q):
                self.assertEqual(route(q, self.catalog).intent, "unsafe")

    def test_ac_2_5_rules_work_without_llm(self) -> None:
        # AC-2.5：不依赖大模型（这里直接调规则，不存在 LLM 分支）
        self.assertEqual(route("上个月各地区的GMV", self.catalog).intent, "ask")

    def test_reverse_data_question_not_misrouted(self) -> None:
        # 反向用例：正常问数不能被判成闲聊/超纲
        for q in ("各品类销量", "今年各月份GMV", "华东的客单价"):
            with self.subTest(q=q):
                self.assertEqual(route(q, self.catalog).intent, "ask")


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
