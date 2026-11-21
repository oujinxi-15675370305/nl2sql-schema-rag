"""离线回退：模板拼 SQL，**不是主路径**。

主路径在 `app/agent.py`：LLM 写 SQL → 代码校验 → 执行 → 失败带报错重试。
这里只负责两件事：

1. **降级**：没配 `LLM_API_KEY`、模型请求失败/超时，或调用方显式 `use_llm=False` 时，
   用它产出一条能跑的 SQL，保证服务不空转（返回里 `engine="rule"`，调用方看得出来）；
2. **确定性基线**：CI 不配 key，`python -m app.evals --engine rule` 跑的就是这份规则，
   用来挡住检索/校验/权限/多轮这些不依赖模型的回归；同一问题永远同一条 SQL。

它只认识 `app/slots.py` 里的 6 个指标 × 6 个维度、6 类时间和单值筛选，覆盖范围是窄的：
IN、HAVING、季度、占比、复购这类问法它拼不出来。`eval/hard_cases.json` 就是专门测这个边界的，
规则在那组用例上明显低于模型——那是"模型不是可有可无"的证据，不是要修的 bug。

约束：本模块只依赖 `app.domain` 和 `app.slots`，不要 import `app.agent`（避免循环）。
"""
from __future__ import annotations

import re

from app.domain import Catalog, SchemaContext
from app.slots import extract_keywords


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


class OfflineFallback:
    """离线规则生成器：降级用，也是 CI 的确定性回归基线。

    不看 feedback：模板改不了自己，重试同一句只会重复同样的错，所以它不是"自我修复"，
    只是"保底能出一条 SQL"。拿到的是 ask() 改写过的自包含问句（继承槽位已补进文本），
    所以这里照常抽一次槽位即可。
    """

    last_engine = "rule"

    def __call__(self, question: str, ctx: SchemaContext, catalog: Catalog,
                 feedback: str | None = None) -> str:
        return rule_based_sql(question, catalog, ctx, extract_keywords(question, catalog))
