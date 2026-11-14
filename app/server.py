"""HTTP 服务：静态页 + /api/ask、/api/schema、/api/eval。只用标准库。"""
from __future__ import annotations

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from app.agent import ask, build_context
from app.catalog import load_catalog
from app.domain import Catalog
from app.retrieval import SchemaRetriever
from app.session import SessionStore

WEB = Path(__file__).with_name("web")
CATALOG: Catalog = load_catalog()
RETRIEVER = SchemaRetriever(CATALOG)
SESSIONS = SessionStore()  # M3：进程内会话，重启即清空


def _json(payload: dict, status: int = 200) -> tuple[int, str, bytes]:
    return status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "nl2sql-schema-rag/0.1"

    def log_message(self, fmt: str, *args) -> None:  # 静音默认日志
        return

    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            status, ctype, body = _json({"ok": True, "tables": len(CATALOG.tables), "metrics": len(CATALOG.metrics)})
            return self._send(status, ctype, body)
        if path == "/api/schema":
            status, ctype, body = _json({
                "tables": [
                    {"name": t.name, "comment": t.comment,
                     "columns": [{"name": c.name, "type": c.type, "comment": c.comment} for c in t.columns]}
                    for t in CATALOG.tables
                ],
                "metrics": [{"name": m.name, "comment": m.comment, "sql": m.sql} for m in CATALOG.metrics],
            })
            return self._send(status, ctype, body)
        if path == "/api/eval":
            from app.evals import run
            summary = run(verbose=False)
            summary.pop("details", None)
            status, ctype, body = _json(summary)
            return self._send(status, ctype, body)

        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB / rel).resolve()
        if not str(target).startswith(str(WEB.resolve())) or not target.exists():
            return self._send(404, "text/plain; charset=utf-8", "not found".encode())
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, ctype, target.read_bytes())

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/ask":
            return self._send(404, "text/plain; charset=utf-8", "not found".encode())
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(*_json({"ok": False, "error": "请求体不是合法 JSON"}, 400))

        question = str(payload.get("question", "")).strip()
        if not question:
            return self._send(*_json({"ok": False, "error": "question 不能为空"}, 400))

        # M3：前端没带 session_id 就新开一个，带回来就接着上文
        session = SESSIONS.get(str(payload.get("session_id") or "").strip() or None)
        plan = ask(
            question,
            catalog=CATALOG,
            retriever=RETRIEVER,
            use_llm=payload.get("use_llm", True),
            session=session,
        )
        ctx, keywords = plan.context, plan.keywords
        body = {
            "question": question,
            "sql": plan.sql,
            "ok": plan.ok,
            "error": plan.error,
            "columns": plan.columns,
            "rows": [list(r) for r in plan.rows],
            "elapsed_ms": plan.elapsed_ms,
            "intent": plan.intent,
            "reply": plan.reply,
            "clarify_options": plan.clarify_options,
            "engine": plan.engine,
            "attempts": plan.attempts,
            "keywords": keywords,
            "session_id": plan.session_id,
            "rewritten_question": plan.rewritten_question,
            "inherited_slots": plan.inherited_slots,
            "context": {
                "metrics": [{"key": h.key, "score": round(h.score, 4)} for h in (ctx.metrics if ctx else [])],
                "tables": [{"key": h.key, "score": round(h.score, 4)} for h in (ctx.tables if ctx else [])],
                "columns": [{"key": h.key, "table": h.payload.get("table"), "score": round(h.score, 4)}
                            for h in (ctx.columns if ctx else [])],
            },
        }
        self._send(*_json(body))


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"NL2SQL Schema RAG 已启动： http://{host}:{port}")
    print("按 Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
