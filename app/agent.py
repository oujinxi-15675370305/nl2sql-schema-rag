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
from typing import Iterable

from app import permissions
from app.catalog import load_catalog
from app.domain import Catalog, Hit, SchemaContext, SQLPlan
from app.retrieval import SchemaRetriever
from app.router import classify, reply_for
from app.session import Session, inherit_slots, rewrite_question
from app.slots import extract_keywords

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "demo.sqlite"
QUERY_TIMEOUT_S = 5.0  # 单条 SQL 的执行预算，超了就让 sqlite 中断

# ---------- 上下文 ----------
def _required_tables(catalog: Catalog, slots: dict) -> list[str]:
    """指标 / 维度 / 筛选一定用到的表。

    "家电品类的成交额" 这类问题，列检索容易把 orders 挤掉（top_tables=4，库里有 5 张表），
    但 gmv 的口径 SQL 依赖 orders.pay_amount——表不在 context 里，模型只能改用 item_amount。
    这里把必需表补进来，再用它们替换掉得分最低的无关表，保持 4 张表的上限。
    """
    required: list[str] = []
    for key in slots.get("metric", []):
        metric = catalog.metric(key)
        if metric:
            for ref in metric.requires:
                table = ref.split(".", 1)[0]
                if table not in required:
                    required.append(table)
    # 所有指标都要么过滤 orders.status、要么按订单去重/关联，orders 基本是必需表
    if slots.get("metric"):
        required.append("orders")
    dims = set(slots.get("dimension", []))
    filters = " ".join(slots.get("filter", []))
    if dims & {"category", "brand", "product"} or "category:" in filters:
        required.extend(["products", "order_items"])
    if "region" in dims or "region:" in filters:
        required.append("regions")
    if "customer" in dims:
        required.append("customers")
    return list(dict.fromkeys(t for t in required if catalog.table(t)))


def build_context(
    question: str,
    catalog: Catalog,
    retriever: SchemaRetriever,
    keywords: dict | None = None,
) -> tuple[SchemaContext, dict]:
    """`keywords` 传了就用它（M3 里是继承后的槽位），避免再抽一遍。"""
    slots = keywords if keywords is not None else extract_keywords(question, catalog)
    metrics, tables, columns = retriever.retrieve(question)
    required = _required_tables(catalog, slots)
    held = {h.key for h in tables}
    for name in required:
        if name in held:
            continue
        # 补表时优先挤掉得分最低、且不在必需列表里的表，保持最多 4 张
        if len(tables) >= 4:
            droppable = [(i, h.score) for i, h in enumerate(tables) if h.key not in required]
            if droppable:
                drop_index = min(droppable, key=lambda item: item[1])[0]
                tables.pop(drop_index)
        table = catalog.table(name)
        tables.append(Hit(kind="table", key=name, score=0.0,
                          payload={"comment": table.comment if table else ""}))
        held.add(name)
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
    need_customers = metric == "customer_count" or "customer" in dims
    return {"items": need_items, "products": need_products, "regions": need_regions,
            "customers": need_customers}


METRIC_SQL = {
    "gmv": "SUM(o.pay_amount)",
    "order_count": "COUNT(DISTINCT o.order_id)",
    "aov": "ROUND(SUM(o.pay_amount) * 1.0 / COUNT(DISTINCT o.order_id), 2)",
    "refund_rate": "ROUND(SUM(CASE WHEN o.status = 'refunded' THEN 1 ELSE 0 END) * 1.0 / COUNT(DISTINCT o.order_id), 4)",
    "units": "SUM(i.quantity)",
    "customer_count": "COUNT(DISTINCT c.customer_id)",
}

# 指标输出精度：退款率是小数，按 catalog 口径保留 4 位；
# 统一套的外层 ROUND 不能把它再压成 2 位（0.125 → 0.13，评测会跟期望差一位）。
METRIC_DECIMALS = {"refund_rate": 4}


def _requested_sensitive(question: str, catalog: Catalog) -> list[tuple[str, str]]:
    """问题里明确点名的敏感列（按传入的 catalog 判断可见性）。

    只有用户真的说"手机号/电话"时才把该列放进 SELECT；不主动带敏感列，
    这样"各客户的 GMV"在 analyst/viewer 下是同一个结果。
    """
    hits: list[tuple[str, str]] = []
    low = question.lower()
    for table in catalog.tables:
        for column in table.columns:
            if not column.sensitive:
                continue
            terms = (column.name, *column.synonyms)
            if any(term.lower() in low for term in terms):
                hits.append((table.name, column.name))
    return hits


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
        "customer": ("c.customer_name", "客户"),
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
    dims = [d for d in keywords["dimension"]
            if d in ("region", "category", "brand", "month", "product", "customer")]
    if dims:
        group_expr, alias = _group_expr(dims[0])
        select_parts.append(f"{group_expr} AS {alias}")

    # M4：显式点名敏感列才选（可见性由调用方传入的 catalog 决定，viewer 这里根本看不到 phone）
    for table_name, column in _requested_sensitive(question, catalog):
        if table_name == "customers":
            select_parts.append(f"c.{column}")
            joins["customers"] = True

    metric_alias = {"gmv": "gmv", "order_count": "order_cnt", "aov": "aov",
                    "refund_rate": "refund_rate", "units": "units",
                    "customer_count": "customer_cnt"}[metric]
    decimals = METRIC_DECIMALS.get(metric, 2)
    select_parts.append(f"ROUND({expr}, {decimals}) AS {metric_alias}")

    from_clause = "orders o"
    if joins["items"]:
        from_clause += " JOIN order_items i ON i.order_id = o.order_id"
    if joins["products"]:
        if not joins["items"]:
            from_clause += " JOIN order_items i ON i.order_id = o.order_id"
        from_clause += " JOIN products p ON p.product_id = i.product_id"
    if joins["regions"]:
        from_clause += " JOIN regions r ON r.region_id = o.region_id"
    if joins["customers"]:
        from_clause += " JOIN customers c ON c.customer_id = o.customer_id"

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
        "你是数据仓库的 SQL 生成器。只能使用下面给出的表和列，禁止编造字段或 JOIN 条件。\n"
        f"可用 schema：\n{ctx.ddl(catalog)}\n\n"
        "口径与格式（必须遵守）：\n"
        "- 只输出一条 SELECT 语句，不要解释，不要 markdown。\n"
        "- 指标口径以 schema 里给出的 metric SQL 为准。\n"
        "- GMV/成交额/销售额一律用 SUM(o.pay_amount)，不要用 order_items.item_amount 或 quantity*unit_price。\n"
        "- 成交额/订单数/客单价/销量/客户数只统计 o.status = 'paid'；退款率不要加这个过滤。\n"
        "- 日期列是 TEXT 'YYYY-MM-DD'；按月分组用 substr(o.order_date, 1, 7)，不要用 strftime。\n"
        "- 本月/上个月/今年/去年/最近30天用 date('now', ...) 计算，不要把 'now' 当字符串 substr：\n"
        "  本月 o.order_date >= date('now','start of month')；\n"
        "  上个月 >= date('now','start of month','-1 month') 且 < date('now','start of month')；\n"
        "  今年 >= date('now','start of year')；去年 >= date('now','start of year','-1 year') 且 < date('now','start of year')；\n"
        "  最近30天 >= date('now','-30 day')；\n"
        "  只有指定月份（YYYY-MM）才用 substr(o.order_date,1,7) = 'YYYY-MM'。\n"
        "- 只 SELECT 问题要求的维度列和指标列，不要额外列。\n"
        "- 如果问题只是换筛选条件（例如「换成华南」），不要额外 SELECT 被筛选的列，除非问题要求按它分组。\n"
        "- 问题里有 排名/Top/最多/前N 时：ORDER BY 指标 DESC LIMIT 10；否则不要 ORDER BY、不要 LIMIT。\n"
        "- JOIN 只能使用 schema 里列出的表关系。\n\n"
        "示例：\n"
        "问题：各地区的GMV\n"
        "SQL：SELECT r.region_name, ROUND(SUM(o.pay_amount), 2) AS gmv "
        "FROM orders o JOIN regions r ON r.region_id = o.region_id "
        "WHERE o.status = 'paid' GROUP BY r.region_name\n\n"
        f"问题：{question}\n"
    )
    if feedback:
        prompt += f"\n上一次生成的 SQL 有问题，请修正后重新生成：\n{feedback}\n"
    prompt += "只输出 SQL。"
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


def validate(
    sql: str,
    catalog: Catalog,
    max_rows: int = 200,
    denied: Iterable[str] = (),
    masked: Iterable[str] = (),
) -> tuple[bool, str]:
    """只读、单语句、表白名单；M4 起再加列白名单（AC-4.1）与掩码列约束（AC-4.3）。

    `denied` 是当前角色不可见的列名：命中即拒，且**话术里不带列名**，
    否则"校验不通过"本身就成了泄漏通道（错误信息会被回传、被回显给模型）。

    `masked` 是当前角色"可见但要掩码"的列名：只允许裸列投影。改名、函数包裹、
    拼接都会让结果列名/列值绕过按列名的掩码，所以在这里直接拒。
    """
    s = sql.strip().rstrip(";")
    low = s.lower()
    if not low.startswith("select"):
        return False, "只允许 SELECT"
    if ";" in s:
        return False, "只允许单条语句"
    for word in FORBIDDEN:
        if re.search(rf"\b{word}\b", low):
            return False, f"禁止的关键字：{word}"

    denied = tuple(n for n in denied if n)
    masked = tuple(n for n in masked if n)
    # `*` 不含列名，白名单匹配不到；viewer 用 select * 就能把隐藏列整列带出来
    if denied and (
        re.search(r"\bselect\s+(?:distinct\s+)?\*", low)
        or re.search(r"\b[a-z_][a-z0-9_]*\.\*", low)
    ):
        return False, "不允许用 * 查询，请明确指定字段"
    for name in denied:
        if re.search(rf"\b{re.escape(name.lower())}\b", low):
            return False, "引用了无权访问的列"
    for name in masked:
        pat = re.escape(name.lower())
        # 改别名：客户手机号 AS p
        if re.search(rf"\b{pat}\b\s+as\s+[a-z_][a-z0-9_]*", low):
            return False, "敏感字段不能改别名，请直接查询"
        # 无 AS 的别名：客户手机号 p（排除 FROM/IS 这类关键字当"别名"）
        if re.search(
            rf"\b{pat}\b\s+(?!(?:from|where|group|order|limit|join|on|is|not|and|or|having|union|"
            rf"collate|desc|asc)\b)[a-z_][a-z0-9_]*",
            low,
        ):
            return False, "敏感字段不能改别名，请直接查询"
        # 函数包裹 / 拼接：substr(customer_phone,...)、customer_phone || '-'
        if re.search(rf"[a-z_][a-z0-9_]*\s*\([^)]*\b{pat}\b", low):
            return False, "敏感字段不能参与函数或表达式"
        if re.search(rf"\b{pat}\b\s*(?:\|\||\+|-|\*|/)", low):
            return False, "敏感字段不能参与函数或表达式"

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
    role: str = permissions.DEFAULT_ROLE,
) -> SQLPlan:
    """AC-1.1：失败带报错重试；AC-1.4：每次尝试都要过校验；AC-1.5：max_repair=0 只跑一次。

    顺序（AC-3.6）：
    **role → 按角色裁剪 catalog → session 槽位继承 → classify → 检索/build_context → 生成**。
    不能先路由："那上个月呢"字面上没有指标，先路由会被判成 clarify。
    列过滤必须在检索之前，否则隐藏列会进 context / prompt（AC-4.1）。
    """
    started = time.time()
    full_catalog = catalog or load_catalog()
    hidden = permissions.hidden_columns(full_catalog, role)  # 未知角色在这里抛 ValueError
    catalog = permissions.visible_catalog(full_catalog, role)  # AC-4.1：不可见列根本不在 catalog 里
    retriever = retriever or SchemaRetriever(catalog)
    gen = generator if generator is not None else (LLMGenerator() if use_llm else RuleGenerator())
    masked_names = permissions.masked_column_names(role)

    # AC-3.1 / AC-3.2：取上文槽位，补齐这句缺的（槽位用裁剪后的 catalog 抽）
    previous = session.last_slots() if session is not None else None
    slots, inherited = inherit_slots(question, extract_keywords(question, catalog), previous)
    rewritten = rewrite_question(question, slots, inherited)
    plan = SQLPlan(
        question=question,
        keywords=slots,
        session_id=session.id if session is not None else "",
        rewritten_question=rewritten,
        inherited_slots=inherited,
        role=role,
    )
    plan.attempts = []

    denied = permissions.denied_terms(full_catalog, role)
    route = classify(question, catalog, slots, denied_terms=denied)  # AC-2.1 / AC-4.2
    plan.intent = route.intent
    if route.intent != "ask":  # AC-2.2：只有 ask 生成 SQL
        plan.reply = reply_for(route)
        plan.clarify_options = route.clarify_options
        plan.context = None  # 非 ask 不检索、也不返回 schema（AC-4.1）
        if route.intent == "unsafe":  # AC-4.4：越权/写操作尝试进审计
            permissions.AUDIT_LOG.record(
                role=role, question=question, session_id=plan.session_id, reason=route.reason
            )
        plan.elapsed_ms = int((time.time() - started) * 1000)
        return plan

    # 只有问数才检索，且检索用的是裁剪后的 catalog（AC-4.1）
    ctx, keywords = build_context(rewritten, catalog, retriever, keywords=slots)
    plan.context = ctx
    plan.keywords = keywords

    if session is not None:
        session.add(question, slots)  # AC-3.1：只把真正问数的一轮记成上下文

    feedback: str | None = None
    for attempt in range(max(0, max_repair) + 1):
        # 交给生成器的是改写后的自包含问句（M3）：模型和规则都不该自己猜上文
        raw_sql = gen(rewritten, ctx, catalog, feedback)  # AC-1.1：第二轮能拿到 feedback
        # AC-4.1：回显/日志用打码后的 SQL，执行和校验仍用原句
        sql = permissions.redact_sql(raw_sql, hidden) if raw_sql else raw_sql
        # M5：stage 让失败分类有确定的信号，不用再靠 error 前缀猜（AC-5.3）
        attempt_log: dict = {"n": attempt + 1, "sql": sql, "ok": False, "error": "",
                             "stage": "validate"}
        plan.sql = sql

        ok, message = validate(raw_sql, catalog, denied=hidden, masked=masked_names)  # AC-1.4 / AC-4.1 / AC-4.3
        timed_out = False
        if ok:
            attempt_log["stage"] = "execute"  # 校验放行后，失败只可能发生在执行
            try:
                cols, rows = execute(raw_sql)
                rows, masked = permissions.mask_rows(cols, rows, masked_names)  # AC-4.3
                plan.columns, plan.rows = cols, rows
                plan.masked_columns = masked
                plan.ok = True
                plan.error = ""  # 修好之后不能把上一轮的报错留在返回值里
                plan.failure_stage = ""  # 同理：ok=True 时不能留着上一次的失败阶段
                attempt_log["ok"] = True
                attempt_log["stage"] = "ok"
                plan.attempts.append(attempt_log)
                break
            except QueryTimeout as exc:
                message = f"执行失败：{exc}"
                timed_out = True
                attempt_log["stage"] = "timeout"
            except Exception as exc:
                message = f"执行失败：{exc}"
        elif hidden and "无权访问" in message:
            # AC-4.4：生成的 SQL 引用了不可见列，也算一次越权尝试
            permissions.AUDIT_LOG.record(
                role=role,
                question=question,
                session_id=plan.session_id,
                reason="生成的 SQL 引用了无权访问的列",
                sql=sql,
            )
            message = "引用了无权访问的列，请只使用可见字段"  # 不回传列名
        plan.ok = False
        plan.error = message
        plan.failure_stage = attempt_log["stage"]  # 最后一轮的失败阶段（M5）
        attempt_log["error"] = message
        plan.attempts.append(attempt_log)
        if timed_out:
            break  # 同一句重跑只会再烧满一次预算，等模型改完再说
        feedback = message

    plan.engine = getattr(gen, "last_engine", "custom")
    plan.elapsed_ms = int((time.time() - started) * 1000)
    return plan
