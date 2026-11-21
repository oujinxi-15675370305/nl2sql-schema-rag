"""HTTP 服务：静态页 + /api/ask、/api/schema、/api/eval。只用标准库。"""
from __future__ import annotations

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app import demo
from app.agent import ScriptedGenerator, ask
from app.catalog import load_catalog
from app.domain import Catalog, SQLPlan
from app.evals import LLM_RESULT
from app.permissions import AUDIT_LOG, DEFAULT_ROLE, ROLES, visible_catalog
from app.retrieval import SchemaRetriever
from app.session import SessionStore

WEB = Path(__file__).with_name("web")
CATALOG: Catalog = load_catalog()
# M4：两个角色各一份裁剪后的 catalog + 检索索引，启动时建好（成本可忽略，别每次请求重建）
CATALOGS: dict[str, Catalog] = {role: visible_catalog(CATALOG, role) for role in ROLES}
RETRIEVERS: dict[str, SchemaRetriever] = {role: SchemaRetriever(c) for role, c in CATALOGS.items()}
SESSIONS = SessionStore()  # M3：进程内会话，重启即清空


def _role_of(raw: object) -> str:
    role = str(raw or DEFAULT_ROLE).strip()
    return role if role in ROLES else ""


def _json(payload: dict, status: int = 200) -> tuple[int, str, bytes]:
    return status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode()


def _llm_result_payload(path: Path | None = None) -> dict:
    """只读 `eval/llm_result.json`：有快照就返回，没有/坏了返回 `available=False`。

    这里**绝不调用模型**：页面加载时只读文件；要更新快照得显式跑
    `python -m scripts.record_llm_eval`（批处理，不进页面点击）。
    """
    target = path or LLM_RESULT
    if not target.exists():
        return {"available": False}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"available": False}
    if not isinstance(data, dict) or not isinstance(data.get("summary"), dict):
        return {"available": False}
    return {**data, "available": True}


def _plan_payload(plan: SQLPlan, question: str) -> dict:
    """`/api/ask` 和 `/api/demo/repair` 共用一份响应结构，前端只写一套渲染。"""
    ctx, keywords = plan.context, plan.keywords
    return {
        "question": question,
        "role": plan.role,
        "sql": plan.sql,
        "ok": plan.ok,
        "error": plan.error,
        "failure_stage": plan.failure_stage,
        "columns": plan.columns,
        "rows": [list(r) for r in plan.rows],
        "masked_columns": plan.masked_columns,
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
        # 非 ask 不返回 schema（AC-4.1，也省一次检索）
        "context": None if ctx is None else {
            "metrics": [{"key": h.key, "score": round(h.score, 4)} for h in ctx.metrics],
            "tables": [{"key": h.key, "score": round(h.score, 4)} for h in ctx.tables],
            "columns": [{"key": h.key, "table": h.payload.get("table"), "score": round(h.score, 4)}
                        for h in ctx.columns],
        },
    }


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
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            status, ctype, body = _json({"ok": True, "tables": len(CATALOG.tables),
                                          "metrics": len(CATALOG.metrics), "roles": list(ROLES)})
            return self._send(status, ctype, body)
        if path == "/api/schema":
            # M4：schema 也按角色裁剪，viewer 看不到敏感列
            role = _role_of(parse_qs(parsed.query).get("role", [DEFAULT_ROLE])[0])
            if not role:
                return self._send(*_json({"ok": False, "error": "未知角色"}, 400))
            catalog = CATALOGS[role]
            status, ctype, body = _json({
                "role": role,
                "tables": [
                    {"name": t.name, "comment": t.comment,
                     "columns": [{"name": c.name, "type": c.type, "comment": c.comment} for c in t.columns]}
                    for t in catalog.tables
                ],
                "metrics": [{"name": m.name, "comment": m.comment, "sql": m.sql} for m in catalog.metrics],
            })
            return self._send(status, ctype, body)
        if path == "/api/audit":
            # AC-4.4：越权尝试的只读出口（内存环形缓冲，不落盘）
            entries = [entry.as_dict() for entry in AUDIT_LOG.entries()]
            return self._send(*_json({"total": len(entries), "entries": entries}))
        if path == "/api/eval":
            from app.evals import run
            summary = run(verbose=False)
            summary.pop("details", None)
            summary.pop("multi_turn_details", None)  # 只留汇总；multi_turn_rate 等字段要保留
            status, ctype, body = _json(summary)
            return self._send(status, ctype, body)
        if path == "/api/eval/llm/last":
            # 只读最近一次模型评测的快照：不扫题库、不调模型（P1）
            return self._send(*_json(_llm_result_payload()))

        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB / rel).resolve()
        if not str(target).startswith(str(WEB.resolve())) or not target.exists():
            return self._send(404, "text/plain; charset=utf-8", "not found".encode())
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, ctype, target.read_bytes())

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in ("/api/ask", "/api/demo/repair"):
            return self._send(404, "text/plain; charset=utf-8", "not found".encode())
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(*_json({"ok": False, "error": "请求体不是合法 JSON"}, 400))

        # M4：角色按请求传，不进会话；未知角色直接 400，不静默降级
        role = _role_of(payload.get("role"))
        if not role:
            return self._send(*_json(
                {"ok": False, "error": f"未知角色：{payload.get('role')!r}（可选 {' / '.join(ROLES)}）"}, 400))

        # 演示：脚本化生成器复现"写错列名 → 带报错改对"，离线截图/录屏用（不写会话）
        if path == "/api/demo/repair":
            plan = ask(
                demo.QUESTION,
                catalog=CATALOG,
                retriever=RETRIEVERS[role],
                generator=ScriptedGenerator(list(demo.ATTEMPTS)),
                use_llm=False,
                role=role,
            )
            return self._send(*_json(_plan_payload(plan, demo.QUESTION)))

        question = str(payload.get("question", "")).strip()
        if not question:
            return self._send(*_json({"ok": False, "error": "question 不能为空"}, 400))

        # M3：前端没带 session_id 就新开一个，带回来就接着上文
        session = SESSIONS.get(str(payload.get("session_id") or "").strip() or None)
        plan = ask(
            question,
            catalog=CATALOG,  # ask 内部按角色裁剪；这里传全量，隐藏列清单才算得出来
            retriever=RETRIEVERS[role],
            use_llm=payload.get("use_llm", True),
            session=session,
            role=role,
        )
        self._send(*_json(_plan_payload(plan, question)))


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
