"""M3 的验收测试（AC-3.1 ~ AC-3.6）。每条对应 docs/SPEC.md 的 AC 编号。"""
from __future__ import annotations

import unittest

from app.agent import ask
from app.catalog import load_catalog
from app.domain import Catalog
from app.router import classify
from app.session import MAX_TURNS, Session, SessionStore, inherit_slots, rewrite_question
from app.slots import extract_keywords

LAST_MONTH = "date('now','start of month','-1 month')"
THIS_MONTH = "date('now','start of month')"


class SessionTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog: Catalog = load_catalog()

    def follow(self, session: Session, question: str):
        return ask(question, catalog=self.catalog, use_llm=False, session=session)


class SessionRecordTest(SessionTestBase):
    def test_ac_3_1_turn_records_question_and_slots(self) -> None:
        # AC-3.1：会话保存"问题 + 解析出的槽位"
        session = Session()
        plan = self.follow(session, "上个月各地区的GMV")

        self.assertEqual(plan.intent, "ask")
        self.assertEqual(plan.session_id, session.id)
        self.assertEqual(len(session), 1)
        turn = session.last()
        self.assertEqual(turn.question, "上个月各地区的GMV")
        self.assertEqual(turn.slots["metric"], ["gmv"])
        self.assertEqual(turn.slots["dimension"], ["region"])
        self.assertEqual(turn.slots["time"], ["last_month"])

    def test_ac_3_1_non_ask_turns_do_not_pollute_context(self) -> None:
        # 反向用例：闲聊/超纲不记成上下文，否则下一句"那上个月呢"会接空槽位
        session = Session()
        self.follow(session, "各地区的GMV")
        self.assertEqual(self.follow(session, "你好").intent, "chat")
        self.assertEqual(len(session), 1)

        plan = self.follow(session, "那上个月呢")
        self.assertEqual(plan.intent, "ask")
        self.assertIn("metric", plan.inherited_slots)


class SlotInheritanceTest(SessionTestBase):
    def test_ac_3_2_omitted_metric_is_inherited(self) -> None:
        # AC-3.2：省略指标时从上文继承；维度也一起带上
        session = Session()
        self.follow(session, "各地区的GMV")
        plan = self.follow(session, "那上个月呢")

        self.assertEqual(plan.intent, "ask")
        self.assertEqual(plan.keywords["metric"], ["gmv"])
        self.assertEqual(plan.keywords["dimension"], ["region"])
        self.assertEqual(plan.keywords["time"], ["last_month"])
        self.assertIn("metric", plan.inherited_slots)
        self.assertIn(LAST_MONTH, plan.sql)
        self.assertIn("GROUP BY r.region_name", plan.sql)

    def test_ac_3_2_explicit_time_overrides_previous(self) -> None:
        # AC-3.2：显式给出的时间覆盖上文
        session = Session()
        self.follow(session, "上个月的GMV")
        plan = self.follow(session, "那这个月呢")

        self.assertEqual(plan.keywords["time"], ["this_month"])
        self.assertIn(THIS_MONTH, plan.sql)
        self.assertNotIn("-1 month", plan.sql)

    def test_ac_3_2_explicit_dimension_overrides_previous(self) -> None:
        # AC-3.2：显式给出的维度覆盖上文，不能两个维度混着分组
        session = Session()
        self.follow(session, "各地区的GMV")
        plan = self.follow(session, "那各品类呢")

        self.assertIn("p.category AS 品类", plan.sql)
        self.assertNotIn("r.region_name AS", plan.sql)

    def test_ac_3_2_total_clears_dimension(self) -> None:
        # 反向用例：显式说"总"就是要求不分组，不能被上文继承成"分地区"
        session = Session()
        self.follow(session, "各地区的GMV")
        plan = self.follow(session, "总GMV是多少")

        self.assertEqual(plan.keywords["dimension"], [])
        self.assertNotIn("dimension", plan.inherited_slots)
        self.assertNotIn("GROUP BY", plan.sql)

    def test_ac_3_2_filters_merge_by_kind(self) -> None:
        # 筛选按类型覆盖："换成华南"只换地区，上文的品类筛选还在
        previous = {"metric": ["gmv"], "dimension": [], "filter": ["region:华东", "category:家电"], "time": []}
        current = {"metric": [], "dimension": [], "filter": ["region:华南"], "time": []}
        merged, inherited = inherit_slots("换成华南", current, previous)

        self.assertEqual(merged["filter"], ["category:家电", "region:华南"])
        self.assertIn("filter", inherited)
        self.assertEqual(merged["metric"], ["gmv"])

    def test_ac_3_2_no_previous_slots_means_no_inheritance(self) -> None:
        # 反向用例：没有上文就原样返回，不能凭空补槽位
        current = extract_keywords("那上个月呢", self.catalog)
        merged, inherited = inherit_slots("那上个月呢", current, None)
        self.assertEqual(merged, current)
        self.assertEqual(inherited, [])


class RewriteTest(SessionTestBase):
    def test_ac_3_3_switch_region(self) -> None:
        # AC-3.3：说"换成华南"能改掉地区筛选
        session = Session()
        self.follow(session, "华东地区的GMV")
        plan = self.follow(session, "换成华南")

        self.assertEqual(plan.intent, "ask")
        self.assertIn("r.region_name = '华南'", plan.sql)
        self.assertNotIn("华东", plan.sql)
        self.assertTrue(plan.ok)

    def test_ac_3_3_switch_time(self) -> None:
        # AC-3.3：说"那上个月呢"能改掉时间，其余照旧
        session = Session()
        self.follow(session, "今年各地区的GMV")
        plan = self.follow(session, "那上个月呢")

        self.assertIn(LAST_MONTH, plan.sql)
        self.assertNotIn("start of year", plan.sql)
        self.assertIn("GROUP BY r.region_name", plan.sql)

    def test_ac_3_3_rewritten_question_is_self_contained(self) -> None:
        # 改写后的问句要能被重新抽出同一组槽位（生成器/大模型只看这一句）
        session = Session()
        self.follow(session, "上个月各地区的GMV")
        plan = self.follow(session, "那华南呢")

        self.assertIn("承接上文", plan.rewritten_question)
        self.assertIn("GMV", plan.rewritten_question)
        self.assertEqual(extract_keywords(plan.rewritten_question, self.catalog), plan.keywords)

    def test_rewrite_question_without_inheritance_keeps_text(self) -> None:
        # 反向用例：没有继承就不改写问句，避免把原问题弄脏
        question = "各地区的GMV"
        slots = extract_keywords(question, self.catalog)
        self.assertEqual(rewrite_question(question, slots, []), question)


class NewTopicTest(SessionTestBase):
    def test_ac_3_4_changed_metric_starts_new_topic(self) -> None:
        # AC-3.4：换了指标又没有指代 → 不继承上文的月份
        session = Session()
        self.follow(session, "上个月的GMV")
        plan = self.follow(session, "退款率是多少")

        self.assertEqual(plan.intent, "ask")
        self.assertEqual(plan.inherited_slots, [])
        self.assertEqual(plan.keywords["time"], [])
        self.assertNotIn("-1 month", plan.sql)
        self.assertIn("refund_rate", plan.sql)

    def test_ac_3_4_offtopic_is_not_rescued_by_context(self) -> None:
        # 反向用例：上文是问数，也不能把"今天天气"救成 ask
        session = Session()
        self.follow(session, "各地区的GMV")
        plan = self.follow(session, "今天天气怎么样")

        self.assertEqual(plan.intent, "out_of_scope")
        self.assertEqual(plan.sql, "")
        self.assertTrue(plan.reply)

    def test_ac_3_4_same_metric_still_inherits(self) -> None:
        # 与 AC-3.4 对照：同指标接着说，才算承接
        session = Session()
        self.follow(session, "上个月的GMV")
        plan = self.follow(session, "华东地区的GMV")

        self.assertIn("time", plan.inherited_slots)
        self.assertIn(LAST_MONTH, plan.sql)


class SessionLimitTest(SessionTestBase):
    def test_ac_3_5_oldest_turn_is_evicted(self) -> None:
        # AC-3.5：超过上限淘汰最旧的
        session = Session(max_turns=MAX_TURNS)
        for i in range(MAX_TURNS + 3):
            session.add(f"问题{i}", {"metric": ["gmv"]})

        self.assertEqual(len(session), MAX_TURNS)
        self.assertEqual(session.turns[0].question, "问题3")
        self.assertEqual(session.turns[-1].question, f"问题{MAX_TURNS + 2}")

    def test_ac_3_5_session_lives_in_memory_only(self) -> None:
        # AC-3.5：不落盘——换个 store 就拿不到上一次的会话
        store = SessionStore()
        session = store.get("abc")
        session.add("上个月的GMV", {"metric": ["gmv"]})
        self.assertIs(store.get("abc"), session)
        self.assertGreaterEqual(len(store), 1)

        fresh = SessionStore()
        self.assertEqual(len(fresh.get("abc")), 0)

    def test_ac_3_5_store_evicts_oldest_session(self) -> None:
        # 反向用例：狂刷 session_id 不能把内存打满
        store = SessionStore(max_sessions=2)
        store.get("a")
        store.get("b")
        store.get("c")
        self.assertEqual(len(store), 2)


class RoutingOrderTest(SessionTestBase):
    def test_ac_3_6_classify_takes_slots_without_extracting(self) -> None:
        # AC-3.6：classify 只认传进来的槽位，不自己再抽一遍
        slots = {"metric": ["gmv"], "dimension": [], "filter": [], "time": ["last_month"]}
        self.assertEqual(classify("那上个月呢", self.catalog, slots).intent, "ask")

        # 反向用例：显式给空指标时，即使问句里写着指标也要澄清（证明没有偷偷重抽）
        empty = {"metric": [], "dimension": [], "filter": [], "time": []}
        self.assertEqual(classify("上个月的GMV", self.catalog, empty).intent, "clarify")

    def test_ac_3_6_inheritance_runs_before_routing(self) -> None:
        # AC-3.6：顺序是 session → 继承 → classify；先路由会被判 clarify
        session = Session()
        self.follow(session, "各地区的GMV")
        plan = self.follow(session, "那上个月呢")
        self.assertEqual(plan.intent, "ask")
        self.assertTrue(plan.ok)

    def test_ac_3_6_followup_without_session_asks_clarify(self) -> None:
        # 反向用例：没有会话就该澄清，不能凭字符串猜上文
        plan = ask("那上个月呢", catalog=self.catalog, use_llm=False)
        self.assertEqual(plan.intent, "clarify")
        self.assertEqual(plan.sql, "")
        self.assertEqual(plan.session_id, "")


if __name__ == "__main__":
    unittest.main()
