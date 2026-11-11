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

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "demo.sqlite"

# ---------- 关键词 ----------
REGIONS = ("华东", "华南", "华北", "西南")
CATEGORIES = ("家电", "数码", "服饰", "食品")
METRIC_HINTS = {
    "gmv": ("成交", "销售额", "营收", "gmv", "交易额", "卖了多少"),
    "order_count": ("多少单", "订单数", "单量", "订单量"),
    "aov": ("客单价", "平均订单", "均价"),
    "refund_rate": ("退款率", "退货率"),
    "units": ("销量", "件数", "卖了多少件"),
}
DIMENSION_HINTS = {
    "region": ("地区", "区域", "大区", "各省"),
    "category": ("品类", "类目", "分类"),
    "brand": ("品牌",),
    "month": ("每月", "按月", "月份", "月度"),
    "product": ("商品", "产品", "单品"),
}


def extract_keywords(question: str, catalog: Catalog) -> dict[str, list[str]]:
    q = question.lower()
    out: dict[str, list[str]] = {"metric": [], "dimension": [], "filter": [], "time": []}
    for metric, hints in METRIC_HINTS.items():
        if any(h.lower() in q for h in hints):
            out["metric"].append(metric)
    for metric in catalog.metrics:
        if any(s.lower() in q for s in metric.synonyms):
            if metric.name not in out["metric"]:
                out["metric"].append(metric.name)
    group_intent = bool(re.search(r"(各|每|按|分|排名|top|分别|各地|各类|哪些)", question))
    if group_intent:
        for dim, hints in DIMENSION_HINTS.items():
            if any(h in question for h in hints):
                out["dimension"].append(dim)
    for region in REGIONS:
        if region in question:
            out["filter"].append(f"region:{region}")
    for cat in CATEGORIES:
        if cat in question:
            out["filter"].append(f"category:{cat}")
    if "上个月" in question or "上月" in question:
        out["time"].append("last_month")
    if "本月" in question or "这个月" in question:
        out["time"].append("this_month")
    if "今年" in question:
        out["time"].append("this_year")
    if "去年" in question:
        out["time"].append("last_year")
    if "最近30天" in question or "近30天" in question:
        out["time"].append("last_30d")
    m = re.search(r"(20\d{2})-(\d{1,2})", question)
    if m:
        out["time"].append(f"month:{m.group(1)}-{int(m.group(2)):02d}")
    return out


# ---------- 上下文 ----------
def build_context(question: str, catalog: Catalog, retriever: SchemaRetriever) -> tuple[SchemaContext, dict]:
    keywords = extract_keywords(question, catalog)
    metrics, tables, columns = retriever.retrieve(question)
    ctx = SchemaContext(metrics=metrics, tables=tables, columns=columns)
    # 关键词命中的指标置顶
    if keywords["metric"]:
        ctx.metrics.sort(key=lambda h: (h.key not in keywords["metric"], -h.score))
    return ctx, keywords


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
    # TODO: 多轮追问没做，每轮都当独立问题处理。
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
    """离线规则生成器。不看 feedback：规则改不了自己，靠兜底不是靠重试。"""

    def __call__(self, question: str, ctx: SchemaContext, catalog: Catalog,
                 feedback: str | None = None) -> str:
        return rule_based_sql(question, catalog, ctx, extract_keywords(question, catalog))


class LLMGenerator:
    """大模型生成器，失败时把错误带回去重写。没配 key 时退回规则。"""

    def __init__(self, fallback: RuleGenerator | None = None) -> None:
        self.fallback = fallback or RuleGenerator()

    def __call__(self, question: str, ctx: SchemaContext, catalog: Catalog,
                 feedback: str | None = None) -> str:
        sql = llm_sql(question, ctx, catalog, feedback)
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


def execute(sql: str, db_path: Path | None = None, max_rows: int = 200) -> tuple[list[str], list[tuple]]:
    path = Path(db_path or DB_PATH)
    if not path.exists():
        raise FileNotFoundError(f"演示库不存在：{path}，先运行 python scripts/seed_demo_db.py")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        conn.execute("PRAGMA query_only = ON")
        cur = conn.execute(sql)
        rows = cur.fetchmany(max_rows)
        cols = [d[0] for d in cur.description] if cur.description else []
        return cols, rows
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
) -> SQLPlan:
    """AC-1.1：失败带报错重试；AC-1.4：每次尝试都要过校验；AC-1.5：max_repair=0 只跑一次。"""
    started = time.time()
    catalog = catalog or load_catalog()
    retriever = retriever or SchemaRetriever(catalog)
    gen = generator if generator is not None else (LLMGenerator() if use_llm else RuleGenerator())

    ctx, keywords = build_context(question, catalog, retriever)
    plan = SQLPlan(question=question, context=ctx, keywords=keywords)
    plan.attempts = []

    feedback: str | None = None
    for attempt in range(max(0, max_repair) + 1):
        sql = gen(question, ctx, catalog, feedback)  # AC-1.1：第二轮能拿到 feedback
        attempt_log: dict = {"n": attempt + 1, "sql": sql, "ok": False, "error": ""}
        plan.sql = sql

        ok, message = validate(sql, catalog)  # AC-1.4：修复后的 SQL 也要校验
        if ok:
            try:
                cols, rows = execute(sql)
                plan.columns, plan.rows = cols, rows
                plan.ok = True
                attempt_log["ok"] = True
                plan.attempts.append(attempt_log)
                break
            except Exception as exc:
                message = f"执行失败：{exc}"
        plan.ok = False
        plan.error = message
        attempt_log["error"] = message
        plan.attempts.append(attempt_log)
        feedback = message

    plan.elapsed_ms = int((time.time() - started) * 1000)
    return plan
