"""Step 3：单 SQL 成功后的可选自动结论（默认关闭）。

只做一件事：把 columns/rows 交给模型写成 1–3 句中文结论，**只陈述结果，不解释原因**。
- 不参与 SQL 生成/校验/权限链路，不改变 `ok/sql/rows`；
- 规则 fallback、空结果、SQL 失败都不调用模型；
- 结论失败只写 `conclusion_error`，不把原本成功的查询变成失败；
- 提示词只给结果，不给 schema，不允许再写 SQL。
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass

MAX_ROWS = 50
MAX_ANSWER_CHARS = 600
DEFAULT_TIMEOUT_S = 30.0

SYSTEM = (
    "你是数据结论助手。只依据给出的 SQL 结果写 1–3 句中文结论。\n"
    "硬性要求：\n"
    "- 只陈述结果里能直接看到的事实，不解释原因，不推测业务动机。\n"
    "- 不要用“因为/由于”解释原因；结果为空或 NULL 时直接说“结果为空/NULL”。\n"
    "- 数字必须来自给出的结果；没有的数字不要补。\n"
    "- 结果不足以回答时说“当前结果不足以判断”，不要编造。\n"
    "- 不要写 SQL、不要 markdown、不要表格。"
)


@dataclass
class ChatResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


def _allows_temperature(model: str) -> bool:
    low = model.lower()
    return not any(token in low for token in ("reasoner", "flash", "v4-pro", "qwq", "thinking"))


def _chat(messages: list[dict], *, model: str | None = None,
          timeout: float | None = None) -> ChatResult | None:
    key = os.getenv("LLM_API_KEY", "").strip()
    if not key:
        return None
    base = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    name = model or os.getenv("LLM_MODEL", "").strip() or "deepseek-chat"
    body: dict = {"model": name, "messages": messages}
    if _allows_temperature(name):
        body["temperature"] = 0
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or DEFAULT_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode())
        text = (data["choices"][0]["message"].get("content") or "").strip()
        if not text:
            return None
        usage = data.get("usage") or {}
        return ChatResult(
            text=text,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )
    except Exception:
        return None


def _cell(value: object, limit: int = 80) -> str:
    text = "NULL" if value is None else str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _result_text(columns: list[str], rows: list[tuple]) -> str:
    lines = ["列：" + ", ".join(columns)]
    lines.append(f"结果（共 {len(rows)} 行，最多展示前 {MAX_ROWS} 行）：")
    for row in rows[:MAX_ROWS]:
        lines.append(" | ".join(_cell(value) for value in row))
    if len(rows) > MAX_ROWS:
        lines.append("注意：结果被截断；未展示的行不能用于排名、汇总或因果推断。")
    return "\n".join(lines)


def summarize(
    question: str,
    columns: list[str],
    rows: list[tuple],
    *,
    chat_fn=None,
    model: str | None = None,
) -> tuple[str, str, dict[str, int]]:
    """返回 (answer, error, usage)；失败时 answer 为空，SQL 结果仍然可用。"""
    if not rows:
        return "", "没有结果可总结", {}
    if not columns:
        return "", "没有列名可总结", {}
    chat = chat_fn or _chat
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content":
            f"问题：{question}\n\n{_result_text(columns, rows)}\n\n"
            "请只写 1–3 句结论，不要解释原因。"},
    ]
    reply = chat(messages, model=model)
    if reply is None:
        return "", "结论生成失败：模型请求失败", {}
    answer = reply.text.strip()
    answer = re.sub(r"^\s*```(?:\w+)?\s*|\s*```\s*$", "", answer).strip()
    if len(answer) < 5:
        return "", "结论为空或过短", {}
    if len(answer) > MAX_ANSWER_CHARS:
        return "", f"结论超过 {MAX_ANSWER_CHARS} 字，未展示", {}
    usage = {"prompt_tokens": reply.prompt_tokens, "completion_tokens": reply.completion_tokens}
    return answer, "", usage
