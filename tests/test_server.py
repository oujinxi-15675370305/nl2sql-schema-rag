"""HTTP 契约测试：`/api/ask` 的字段要和前端对得上。

前端按 `intent` 分支渲染话术，所以这里把非 ask 意图的 JSON 形状钉住；
不然前端再漏一次 `reply` 也不会有人发现。
"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from app.server import Handler


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
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/ask",
            data=json.dumps({"question": question, **extra}).encode(),
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


if __name__ == "__main__":
    unittest.main()
