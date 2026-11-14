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


if __name__ == "__main__":
    unittest.main()
