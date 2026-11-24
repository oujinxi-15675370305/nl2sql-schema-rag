"""HTTP 契约测试：`/api/ask` 的字段要和前端对得上。

前端按 `intent` 分支渲染话术，所以这里把非 ask 意图的 JSON 形状钉住；
不然前端再漏一次 `reply` 也不会有人发现。
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from app.server import Handler
from app.domain import AnalysisStep, SQLPlan


class AskApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except OSError as exc:  # 沙箱里绑不了端口就跳过，不让 CI 挂
            raise unittest.SkipTest(f"无法绑定本地端口：{exc}") from exc
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def _post(self, question: str, **extra: object) -> tuple[int, dict]:
        return self._post_path("/api/ask", {"question": question, **extra})

    def _post_path(self, path: str, payload: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())

    def _get(self, path: str) -> tuple[int, dict]:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())

    def test_chat_contract(self) -> None:
        # 非 ask：有 intent/reply，没有 SQL，也不该有 attempts
        status, data = self._post("你好")
        self.assertEqual(status, 200)
        self.assertEqual(data["intent"], "chat")
        self.assertFalse(data["ok"])
        self.assertEqual(data["sql"], "")
        self.assertTrue(data["reply"])
        self.assertEqual(data["attempts"], [])

    def test_clarify_contract(self) -> None:
        _, data = self._post("看下业绩")
        self.assertEqual(data["intent"], "clarify")
        self.assertGreaterEqual(len(data["clarify_options"]), 2)
        self.assertEqual(data["sql"], "")

    def test_ask_contract(self) -> None:
        _, data = self._post("各地区的GMV")
        self.assertEqual(data["intent"], "ask")
        self.assertTrue(data["ok"])
        self.assertTrue(data["sql"].lower().startswith("select"))
        self.assertIn(data["engine"], ("rule", "llm", "custom"))
        self.assertGreaterEqual(len(data["attempts"]), 1)
        # M5：attempts[].stage 与 failure_stage 进 HTTP 契约（失败分类靠它们）
        self.assertEqual(data["failure_stage"], "")
        self.assertEqual(data["attempts"][-1]["stage"], "ok")

    def test_analysis_mode_contract(self) -> None:
        # Step 2：mode=analysis 返回 steps/answer/evidence；快路径的字段仍然在
        plan = SQLPlan(
            question="各地区的GMV", ok=True, intent="ask", engine="analysis",
            mode="analysis", elapsed_ms=12,
            steps=[AnalysisStep(
                id="s1", question="各地区的GMV", purpose="看分布", sql="SELECT 1",
                columns=["n"], rows=[(1,)], row_count=1, ok=True,
                warning="GMV 口径提醒", engine="llm", elapsed_ms=5,
            )],
            answer="一共有 1 行 [s1]。", evidence=["s1"],
            usage={"prompt_tokens": 3, "completion_tokens": 4},
        )
        with mock.patch("app.server.ask", return_value=plan):
            status, data = self._post("各地区的GMV", mode="analysis")
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "analysis")
        self.assertTrue(data["analysis_model"])
        self.assertEqual(data["answer"], "一共有 1 行 [s1]。")
        self.assertEqual(data["evidence"], ["s1"])
        self.assertEqual(data["steps"][0]["id"], "s1")
        self.assertEqual(data["steps"][0]["rows"], [[1]])
        self.assertTrue(data["steps"][0]["ok"])
        self.assertEqual(data["steps"][0]["warning"], "GMV 口径提醒")
        self.assertEqual(data["usage"]["completion_tokens"], 4)

    def test_single_mode_response_has_no_analysis_keys(self) -> None:
        # 快路径结构不变：不传 mode 时没有 mode/steps/evidence；Step 3 的 answer/conclusion 默认空
        _, data = self._post("各地区的GMV", use_llm=False)
        self.assertNotIn("mode", data)
        self.assertNotIn("steps", data)
        self.assertNotIn("evidence", data)
        self.assertNotIn("analysis_model", data)
        self.assertEqual(data["answer"], "")
        self.assertFalse(data["conclusion"])

    def test_conclusion_must_be_bool(self) -> None:
        # 防止字符串 "false" 被 bool() 当成 true，意外多调一次结论模型
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("各地区的GMV", conclusion="false")
        self.assertEqual(ctx.exception.code, 400)

    def test_conclusion_contract(self) -> None:
        plan = SQLPlan(
            question="各地区的GMV", ok=True, intent="ask", engine="llm",
            columns=["region_name", "gmv"], rows=[("华东", 1)],
            answer="华东的 GMV 为 1。", conclusion=True,
            conclusion_usage={"prompt_tokens": 2, "completion_tokens": 3},
        )
        with mock.patch("app.server.ask", return_value=plan) as ask_mock:
            _, data = self._post("各地区的GMV", conclusion=True)
        self.assertTrue(data["conclusion"])
        self.assertEqual(data["answer"], "华东的 GMV 为 1。")
        self.assertEqual(data["conclusion_usage"]["completion_tokens"], 3)
        self.assertTrue(ask_mock.call_args.kwargs["conclusion"])

    def test_analysis_offline_falls_back_to_single(self) -> None:
        # 离线/规则模式没有 Planner：明确回退单 SQL，并把原因带回来，不假装分析成功
        _, data = self._post("各地区的GMV", mode="analysis", use_llm=False)
        self.assertEqual(data["mode"], "single")
        self.assertEqual(data["steps"], [])
        self.assertIn("离线", data["analysis_error"])
        self.assertTrue(data["sql"])
        self.assertEqual(data["engine"], "rule")

    def test_unknown_mode_is_rejected(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("各地区的GMV", mode="react")
        self.assertEqual(ctx.exception.code, 400)

    def test_demo_repair_contract(self) -> None:
        # M6：离线演示"写错列名 → 带报错改对"，前端时间线用同一份 attempts 结构
        _, data = self._post_path("/api/demo/repair")
        self.assertTrue(data["ok"])
        self.assertEqual(data["question"], "各地区的GMV")
        self.assertEqual(data["session_id"], "")  # 演示不写会话
        self.assertEqual(len(data["attempts"]), 2)
        self.assertFalse(data["attempts"][0]["ok"])
        self.assertEqual(data["attempts"][0]["stage"], "execute")
        self.assertIn("no such column", data["attempts"][0]["error"])
        self.assertEqual(data["attempts"][1]["stage"], "ok")
        self.assertEqual(data["failure_stage"], "")
        self.assertEqual(len(data["rows"]), 4)
        self.assertTrue(data["sql"].startswith("SELECT"))

    def test_demo_repair_rejects_unknown_role(self) -> None:
        # 反向用例：演示接口同样不静默降级未知角色
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post_path("/api/demo/repair", {"role": "guest"})
        self.assertEqual(ctx.exception.code, 400)

    def test_session_round_trip(self) -> None:
        # AC-3.1 / AC-3.3：带上 session_id 就能接着上文，多轮不断
        _, first = self._post("各地区的GMV")
        session_id = first["session_id"]
        self.assertTrue(session_id)
        self.assertEqual(first["inherited_slots"], [])

        _, second = self._post("那上个月呢", session_id=session_id)
        self.assertEqual(second["session_id"], session_id)
        self.assertEqual(second["intent"], "ask")
        self.assertIn("metric", second["inherited_slots"])
        self.assertIn("承接上文", second["rewritten_question"])
        self.assertIn("start of month", second["sql"])

    def test_given_session_id_is_reused(self) -> None:
        # 前端自己生成 id 也能用：服务端不覆盖显式传入的 session_id
        _, data = self._post("各地区的GMV", session_id="web-abc-123")
        self.assertEqual(data["session_id"], "web-abc-123")

    def test_followup_without_session_asks_clarify(self) -> None:
        # 反向用例：没有会话就老老实实澄清，不能凭字符串猜上文
        _, data = self._post("那上个月呢")
        self.assertEqual(data["intent"], "clarify")
        self.assertEqual(data["sql"], "")
        self.assertEqual(data["inherited_slots"], [])

    def test_empty_question_is_rejected(self) -> None:
        # 反向用例：空问题返回 400，不进路由
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/ask",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)

    def test_role_and_masking_contract(self) -> None:
        # AC-4.3：analyst 拿到的是掩码值，JSON 里带 role 和 masked_columns
        _, data = self._post("各客户的GMV和手机号", role="analyst")
        self.assertEqual(data["role"], "analyst")
        self.assertEqual(data["masked_columns"], ["customer_phone"])
        self.assertTrue(data["rows"])
        self.assertRegex(data["rows"][0][1], r"^\d{3}\*{4}\d{4}$")

    def test_viewer_sensitive_request_contract(self) -> None:
        # AC-4.2：viewer 请求敏感列 → unsafe + 话术，整份 JSON 不出现列名（AC-4.1）
        _, data = self._post("各客户的GMV和手机号", role="viewer")
        self.assertEqual(data["intent"], "unsafe")
        self.assertEqual(data["sql"], "")
        self.assertTrue(data["reply"])
        self.assertIsNone(data["context"])
        self.assertNotIn("customer_phone", json.dumps(data, ensure_ascii=False))

    def test_viewer_ask_carries_no_sensitive_column(self) -> None:
        _, data = self._post("各客户的GMV", role="viewer")
        self.assertTrue(data["ok"])
        self.assertNotIn("customer_phone", data["sql"])
        self.assertEqual(data["masked_columns"], [])

    def test_non_ask_has_no_context(self) -> None:
        # 非 ask 不返回 schema，既省检索也避免泄漏
        _, data = self._post("你好")
        self.assertIsNone(data["context"])

    def test_unknown_role_is_rejected(self) -> None:
        # 反向用例：未知角色 400，不静默降级成 analyst
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("各地区的GMV", role="guest")
        self.assertEqual(ctx.exception.code, 400)

    def test_schema_endpoint_is_trimmed_by_role(self) -> None:
        # AC-4.1：schema 浏览也按角色裁剪
        _, viewer = self._get("/api/schema?role=viewer")
        self.assertEqual(viewer["role"], "viewer")
        self.assertNotIn("customer_phone", json.dumps(viewer, ensure_ascii=False))
        _, analyst = self._get("/api/schema?role=analyst")
        self.assertIn("customer_phone", json.dumps(analyst, ensure_ascii=False))

    def test_audit_endpoint_lists_denied_attempts(self) -> None:
        # AC-4.4：越权尝试可从只读入口看到，且审计里不带隐藏列名
        from app.permissions import AUDIT_LOG
        AUDIT_LOG.clear()
        self._post("给我手机号", role="viewer")
        _, data = self._get("/api/audit")
        self.assertEqual(data["total"], 1)
        entry = data["entries"][0]
        self.assertEqual(entry["role"], "viewer")
        self.assertIn("无权访问", entry["reason"])
        self.assertNotIn("customer_phone", json.dumps(data, ensure_ascii=False))

    def test_eval_endpoint_exposes_multi_turn_and_failure_summary(self) -> None:
        # AC-5.5：/api/eval 只去掉明细，多轮与失败分类的汇总字段要留着
        _, data = self._get("/api/eval")
        self.assertNotIn("details", data)
        self.assertNotIn("multi_turn_details", data)
        self.assertIn("multi_turn_rate", data)
        self.assertGreaterEqual(data["multi_turn_total"], 9)
        self.assertIn("failure_counts", data)
        self.assertEqual(data["exec_rate"], 1.0)

    def test_last_llm_eval_reads_artifact(self) -> None:
        # AC-5.7：页面展示的"上次模型评测"来自只读 artifact，不在页面现场调模型
        _, data = self._get("/api/eval/llm/last")
        self.assertTrue(data["available"])
        self.assertEqual(data["engine"], "llm")
        self.assertTrue(data["model"])
        self.assertTrue(data["generated_at"])
        self.assertEqual(data["summary"]["fallbacks"], 0)

    def test_last_llm_eval_missing_artifact_is_hidden(self) -> None:
        # 反向用例：没有 artifact 时接口仍 200，但 available=False（前端据此隐藏）
        with mock.patch("app.server.LLM_RESULT", Path("/nonexistent/llm_result.json")):
            status, data = self._get("/api/eval/llm/last")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"available": False})

    def test_last_llm_eval_corrupt_artifact_is_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "llm_result.json"
            path.write_text("{not json", encoding="utf-8")
            with mock.patch("app.server.LLM_RESULT", path):
                _, data = self._get("/api/eval/llm/last")
        self.assertEqual(data, {"available": False})

    def test_last_llm_eval_never_calls_model(self) -> None:
        # 反向用例：把模型层和评测 runner 都换成炸弹，只读接口仍必须正常返回
        with mock.patch("app.agent.llm_sql", side_effect=AssertionError("不应调用模型")), \
                mock.patch("app.evals.run", side_effect=AssertionError("不应跑评测")):
            status, data = self._get("/api/eval/llm/last")
        self.assertEqual(status, 200)
        self.assertTrue(data["available"])


if __name__ == "__main__":
    unittest.main()
