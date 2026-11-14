"""关键词抽取 → 检索 → 生成 SQL → 校验 → 执行。

没配 LLM_API_KEY 就走规则生成，配了就走大模型，规则结果兜底。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.request
from pathlib import Path

from app.catalog import load_catalog
from app.domain import Catalog, SchemaContext, SQLPlan
from app.retrieval import SchemaRetriever
from app.router import classify, reply_for
from app.session import Session, inherit_slots, rewrite_question
from app.slots import extract_keywords

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "demo.sqlite"
QUERY_TIMEOUT_S = 5.0  # 单条 SQL 的执行预算，超了就让 sqlite 中断

# ---------- 上下文 ----------
def build_context(
    question: str,
    catalog: Catalog,
    retriever: SchemaRetriever,
    keywords: dict | None = None,
) -> tuple[SchemaContext, dict]:
    """`keywords` 传了就用它（M3 里是继承后的槽位），避免再抽一遍。"""
    slots = keywords if keywords is not None else extract_keywords(question, catalog)
    metrics, tables, columns = retriever.retrieve(question)
    ctx = SchemaContext(metrics=metrics, tables=tables, columns=columns)
    # 关键词命中的指标置顶
    if slots["metric"]:
        ctx.metrics.sort(key=lambda h: (h.key not in slots["metric"], -h.score))
    return ctx, slots


# ---------- SQL 生成（离线规则 + 可选 LLM） ----------
def _need_join(keywords: dict, metric: str) -> dict[str, bool]:
    dims = set(keywords["dimension"])
    filters = keywords["filter"]
    need_items = metric == "units" or "product" in dims or any(f.startswith("category") for f in filters)
    need_products = "product" in dims or "category" in dims or "brand" in dims or any(
        f.startswith("category") for f in filters
    )
    need_regions = "region" in dims or any(f.startswith("region") for f in filters)
    return {"items": need_items, "products": need_products, "regions": need_regions}


METRIC_SQL = {
    "gmv": "SUM(o.pay_amount)",
    "order_count": "COUNT(DISTINCT o.order_id)",
    "aov": "ROUND(SUM(o.pay_amount) * 1.0 / COUNT(DISTINCT o.order_id), 2)",
    "refund_rate": "ROUND(SUM(CASE WHEN o.status = 'refunded' THEN 1 ELSE 0 END) * 1.0 / COUNT(DISTINCT o.order_id), 4)",
    "units": "SUM(i.quantity)",
}


def _time_clause(hint: str) -> str:
    if hint == "last_month":
        return "o.order_date >= date('now','start of month','-1 month') AND o.order_date < date('now','start of month')"
    if hint == "this_month":
        return "o.order_date >= date('now','start of month')"
    if hint == "this_year":
        return "o.order_date >= date('now','start of year')"
    if hint == "last_year":
        return "o.order_date >= date('now','start of year','-1 year') AND o.order_date < date('now','start of year')"
    if hint == "last_30d":
        return "o.order_date >= date('now','-30 day')"
    if hint.startswith("month:"):
        value = hint.split(":", 1)[1]
        return f"substr(o.order_date, 1, 7) = '{value}'"
    return ""


def _group_expr(dim: str) -> tuple[str, str]:
    return {
        "region": ("r.region_name", "地区"),
        "category": ("p.category", "品类"),
        "brand": ("p.brand", "品牌"),
        "month": ("substr(o.order_date, 1, 7)", "月份"),
        "product": ("p.product_name", "商品"),
    }[dim]


def rule_based_sql(question: str, catalog: Catalog, ctx: SchemaContext, keywords: dict) -> str:
    # FIXME: 只支持单指标 + 单维度。像「各品牌客单价」这种能跑，但「各品牌客单价和销量对比」会退化。
    # M3：多轮继承在 ask() 里做完再传进来，这里只按槽位拼 SQL，不关心槽位是抽的还是继承的。
    metric = keywords["metric"][0] if keywords["metric"] else None
    if metric is None:
        metric = ctx.metrics[0].key if ctx.metrics else "gmv"
    expr = METRIC_SQL.get(metric, "SUM(o.pay_amount)")
    joins = _need_join(keywords, metric)

    select_parts: list[str] = []
    group_expr = None
    alias = None
    dims = [d for d in keywords["dimension"] if d in ("region", "category", "brand", "month", "product")]
    if dims:
        group_expr, alias = _group_expr(dims[0])
        select_parts.append(f"{group_expr} AS {alias}")

    metric_alias = {"gmv": "gmv", "order_count": "order_cnt", "aov": "aov",
                    "refund_rate": "refund_rate", "units": "units"}[metric]
    select_parts.append(f"ROUND({expr}, 2) AS {metric_alias}")

    from_clause = "orders o"
    if joins["items"]:
        from_clause += " JOIN order_items i ON i.order_id = o.order_id"
    if joins["products"]:
        if not joins["items"]:
            from_clause += " JOIN order_items i ON i.order_id = o.order_id"
        from_clause += " JOIN products p ON p.product_id = i.product_id"
    if joins["regions"]:
        from_clause += " JOIN regions r ON r.region_id = o.region_id"

    where: list[str] = []
    if metric != "refund_rate":
        where.append("o.status = 'paid'")
    for f in keywords["filter"]:
        kind, value = f.split(":", 1)
        if kind == "region":
            if not joins["regions"]:
                from_clause += " JOIN regions r ON r.region_id = o.region_id"
            where.append(f"r.region_name = '{value}'")
        elif kind == "category":
            where.append(f"p.category = '{value}'")
    if keywords["time"]:
        clause = _time_clause(keywords["time"][0])
        if clause:
            where.append(clause)

    sql = f"SELECT {', '.join(select_parts)}\nFROM {from_clause}"
    if where:
        sql += "\nWHERE " + "\n  AND ".join(where)
    if group_expr:
        sql += f"\nGROUP BY {group_expr}\nORDER BY {metric_alias} DESC"
    limit = 10 if re.search(r"(排名|top|最多|最高|前\d+)", question, re.I) else 20
    sql += f"\nLIMIT {limit}"
    return sql


def llm_sql(
    question: str, ctx: SchemaContext, catalog: Catalog, feedback: str | None = None
) -> str | None:
    """有 key 才走；返回 None 表示回退到规则。feedback 是上一次失败的原因。"""
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        return None
    base = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.getenv("LLM_MODEL", "deepseek-chat")
    prompt = (
        "你是数据仓库的 SQL 生成器。只能使用下面给出的表和列，禁止编造字段。\n"
        f"可用 schema：\n{ctx.ddl(catalog)}\n\n"
        f"问题：{question}\n"
    )
    if feedback:
        prompt += f"\n上一次生成的 SQL 有问题，请修正后重新生成：\n{feedback}\n"
    prompt += "只输出一条 SELECT 语句，不要解释，不要 markdown。"
    body = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0}
    ).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        text = data["choices"][0]["message"]["content"].strip()
        text = re.sub(r"^```(?:sql)?|```$", "", text, flags=re.M).strip()
        return text
    except Exception:
        return None


# ---------- 生成器（可替换，便于测试与接入不同模型） ----------
class RuleGenerator:
    """离线规则生成器。不看 feedback：规则改不了自己，靠兜底不是靠重试。

    拿到的是 ask() 改写过的自包含问句（继承槽位已补进文本），所以这里照常抽一次即可。
    """

    last_engine = "rule"

    def __call__(self, question: str, ctx: SchemaContext, catalog: Catalog,
                 feedback: str | None = None) -> str:
        return rule_based_sql(question, catalog, ctx, extract_keywords(question, catalog))


class LLMGenerator:
    """大模型生成器，失败时把错误带回去重写。没配 key 时退回规则。"""

    def __init__(self, fallback: RuleGenerator | None = None) -> None:
        self.fallback = fallback or RuleGenerator()
        self.last_engine = "rule"

    def __call__(self, question: str, ctx: SchemaContext, catalog: Catalog,
                 feedback: str | None = None) -> str:
        sql = llm_sql(question, ctx, catalog, feedback)
        # 让上层能看出这次到底走了模型还是退回了规则（排查 key/超时全靠它）
        self.last_engine = "llm" if sql else "rule"
        return sql if sql else self.fallback(question, ctx, catalog, feedback)


class ScriptedGenerator:
    """测试/演示用：按顺序吐出预设 SQL，用来模拟"第一次写错、第二次改对"。"""

    def __init__(self, scripts: list[str]) -> None:
        self.scripts = list(scripts)
        self.calls: list[str | None] = []

    def __call__(self, question: str, ctx: SchemaContext, catalog: Catalog,
                 feedback: str | None = None) -> str:
        self.calls.append(feedback)
        idx = min(len(self.calls) - 1, len(self.scripts) - 1)
        return self.scripts[idx]


# ---------- 校验与执行 ----------
FORBIDDEN = ("insert", "update", "delete", "drop", "alter", "create", "attach", "pragma", "vacuum")


def validate(sql: str, catalog: Catalog, max_rows: int = 200) -> tuple[bool, str]:
    s = sql.strip().rstrip(";")
    low = s.lower()
    if not low.startswith("select"):
        return False, "只允许 SELECT"
    if ";" in s:
        return False, "只允许单条语句"
    for word in FORBIDDEN:
        if re.search(rf"\b{word}\b", low):
            return False, f"禁止的关键字：{word}"
    known = {t.name for t in catalog.tables}
    used = set(re.findall(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", low))
    unknown = used - known
    if unknown:
        return False, f"未知的表：{', '.join(sorted(unknown))}"
    if "limit" not in low:
        return True, f"未带 LIMIT，执行时按 {max_rows} 行截断"
    return True, ""


class QueryTimeout(TimeoutError):
    """SQL 跑过了执行预算，被 sqlite 的 progress handler 中断。"""


def execute(
    sql: str,
    db_path: Path | None = None,
    max_rows: int = 200,
    timeout_s: float | None = None,
) -> tuple[list[str], list[tuple]]:
    """只读执行，带语句级超时。

    sqlite 的 `timeout=` 只是等锁，管不住慢查询；这里挂 progress handler，
    每 1 万条 VM 指令看一眼表，超预算就返回非 0 让 sqlite 中断。
    """
    path = Path(db_path or DB_PATH)
    if not path.exists():
        raise FileNotFoundError(f"演示库不存在：{path}，先运行 python scripts/seed_demo_db.py")
    limit = QUERY_TIMEOUT_S if timeout_s is None else timeout_s
    deadline = time.monotonic() + limit if limit and limit > 0 else None

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        conn.execute("PRAGMA query_only = ON")
        if deadline is not None:
            conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
        try:
            cur = conn.execute(sql)
            rows = cur.fetchmany(max_rows)
            cols = [d[0] for d in cur.description] if cur.description else []
            return cols, rows
        except sqlite3.OperationalError as exc:
            if deadline is not None and "interrupt" in str(exc).lower():
                raise QueryTimeout(f"查询超时（>{limit:g} 秒），已中断") from exc
            raise
    finally:
        conn.close()


def ask(
    question: str,
    *,
    catalog: Catalog | None = None,
    retriever: SchemaRetriever | None = None,
    generator: object | None = None,
    use_llm: bool = True,
    max_repair: int = 2,
    session: Session | None = None,
) -> SQLPlan:
    """AC-1.1：失败带报错重试；AC-1.4：每次尝试都要过校验；AC-1.5：max_repair=0 只跑一次。

    M3 的顺序（AC-3.6）：**session → 槽位继承 → classify → 生成**，不能先路由：
    "那上个月呢"字面上没有指标，先路由会被判成 clarify。
    """
    started = time.time()
    catalog = catalog or load_catalog()
    retriever = retriever or SchemaRetriever(catalog)
    gen = generator if generator is not None else (LLMGenerator() if use_llm else RuleGenerator())

    # AC-3.1 / AC-3.2：取上文槽位，补齐这句缺的
    previous = session.last_slots() if session is not None else None
    slots, inherited = inherit_slots(question, extract_keywords(question, catalog), previous)
    rewritten = rewrite_question(question, slots, inherited)
    ctx, keywords = build_context(rewritten, catalog, retriever, keywords=slots)
    plan = SQLPlan(
        question=question,
        context=ctx,
        keywords=keywords,
        session_id=session.id if session is not None else "",
        rewritten_question=rewritten,
        inherited_slots=inherited,
    )
    plan.attempts = []

    route = classify(question, catalog, slots)  # AC-2.1 / AC-2.5 / AC-3.6：槽位由上面传进来
    plan.intent = route.intent
    if route.intent != "ask":  # AC-2.2：只有 ask 生成 SQL
        plan.reply = reply_for(route)
        plan.clarify_options = route.clarify_options
        plan.elapsed_ms = int((time.time() - started) * 1000)
        return plan

    if session is not None:
        session.add(question, slots)  # AC-3.1：只把真正问数的一轮记成上下文

    feedback: str | None = None
    for attempt in range(max(0, max_repair) + 1):
        # 交给生成器的是改写后的自包含问句（M3）：模型和规则都不该自己猜上文
        sql = gen(rewritten, ctx, catalog, feedback)  # AC-1.1：第二轮能拿到 feedback
        attempt_log: dict = {"n": attempt + 1, "sql": sql, "ok": False, "error": ""}
        plan.sql = sql

        ok, message = validate(sql, catalog)  # AC-1.4：修复后的 SQL 也要校验
        timed_out = False
        if ok:
            try:
                cols, rows = execute(sql)
                plan.columns, plan.rows = cols, rows
                plan.ok = True
                plan.error = ""  # 修好之后不能把上一轮的报错留在返回值里
                attempt_log["ok"] = True
                plan.attempts.append(attempt_log)
                break
            except QueryTimeout as exc:
                message = f"执行失败：{exc}"
                timed_out = True
            except Exception as exc:
                message = f"执行失败：{exc}"
        plan.ok = False
        plan.error = message
        attempt_log["error"] = message
        plan.attempts.append(attempt_log)
        if timed_out:
            break  # 同一句重跑只会再烧满一次预算，等模型改完再说
        feedback = message

    plan.engine = getattr(gen, "last_engine", "custom")
    plan.elapsed_ms = int((time.time() - started) * 1000)
    return plan
