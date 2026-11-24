"""主路径：检索 schema → LLM 写 SQL → 代码校验 → 执行 → 失败带报错重试。

规则 SQL 不在这里：它退到 `app/fallback.py` 的 `OfflineFallback`，只在没 key / 请求失败 /
调用方显式 `use_llm=False` 时兜底，也是 CI 的离线回归基线。本模块只保留
`LLMGenerator`、`ScriptedGenerator`、`build_context`、`validate`、`execute`、`ask`。
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
from app.fallback import OfflineFallback
from app.retrieval import SchemaRetriever
from app.router import classify, reply_for
from app.semantics import (
    DEFAULT_REPURCHASE_INSTRUCTION,
    DEFAULT_SHARE_INSTRUCTION,
    is_repurchase_question,
    is_share_question,
)
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
    # 所有指标都要么过滤 orders.status、要么按订单去重/关联，orders 基本是必需表。
    # 没有指标的维度清单（"华东有哪些商品类别"）也要靠 orders 才能从地区连到商品，
    # 所以只要问题有槽位就把 orders 带上。
    if any(slots.get(key) for key in ("metric", "dimension", "filter", "time")):
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
    # 复购表只服务复购/阈值客户问题；否则“有多少客户”会被它挤进上下文，模型可能直接数全量客户
    if not is_repurchase_question(question):
        tables = [h for h in tables if h.key != "repurchase_customers"]
        columns = [h for h in columns if h.payload.get("table") != "repurchase_customers"]
    required = _required_tables(catalog, slots)
    if is_repurchase_question(question) and catalog.table("repurchase_customers"):
        # 复购题必须有这张语义表；不能只指望检索的同义词命中。
        # 仍要过 catalog.table()：表不在当前 catalog 里时不能把名字塞进 ctx，
        # 否则 prompt 会提到 DDL 里没有的表（AC-9.8 修的就是这个幻觉）。
        required = list(dict.fromkeys([*required, "repurchase_customers"]))
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


# 筛选槽位对应的输出列：只换筛选条件时，这些列不该出现在 SELECT 里
FILTER_COLUMNS = {"region": "region_name", "category": "category"}

# “每个分组前 N 名”：N 后面紧跟时间单位时是时间范围，不是 Top-N
# （“各品类前 10 个月”= 最近 10 个月，不是每个品类的前 10 名）。
# `[0-9]+(?![0-9])` 阻止把 “10 个月” 退化成 “1” + “0 个月” 再误判。
GROUPED_TOP_N = re.compile(
    r"(?:每个|各|按).{0,20}(?:前|top)\s*(?:[0-9]+(?![0-9])|[一二三四五六七八九十两]+)\s*"
    r"(?!个?\s*(?:月|天|日|周|年|季|小时|分钟))",
    re.I,
)
WINDOW_FN = re.compile(r"\b(?:row_number|rank|dense_rank)\s*\(", re.I)


def _shape_feedback(sql: str, slots: dict, question: str = "") -> str:
    """生成后的语义形状检查：筛选条件必须保留；只换筛选时不要 SELECT 筛选列。

    这不是替代 `validate`，而是把 prompt 里已经写明的约定变成可执行的反馈：
    返回非空时按一次 validate 失败处理，把话术作为 feedback 让模型重写。
    只对 `filter` 槽位和“每个分组内 Top-N”生效；无筛选/普通排名的问题不受影响。
    """
    low = sql.lower()
    if GROUPED_TOP_N.search(question) and not WINDOW_FN.search(low):
        return ("问题要求每个分组内的 Top-N，当前 SQL 只有全局排序/LIMIT；"
                "请用 WITH + ROW_NUMBER() OVER (PARTITION BY 分组列 ORDER BY 指标 DESC) "
                "筛 rn <= N 后再输出。")
    if is_share_question(question) and "/" not in sql:
        return ("占比/份额题必须输出比值列（分子/分母），且分子分母同源；"
                "当前 SQL 没有除法，只返回了原始指标。请改写为 share = 分子 / 分母。")
    filters = slots.get("filter", [])
    if not filters:
        return ""
    values = [item.split(":", 1)[1] for item in filters if ":" in item]
    missing = [value for value in values if value not in sql]
    if missing:
        return f"上文筛选条件必须保留：{', '.join(missing)}。请把筛选加回 WHERE 再生成。"
    if len(values) > 1:
        # "华东和华南对比"这类多值筛选，按筛选列分组是合理的，不要误判成"多选了筛选列"
        return ""
    if slots.get("dimension"):
        return ""
    # 只看最后一个 SELECT 的投影，避免把 CTE 内部的选择误当成最终输出
    select_part = re.split(r"\bfrom\b", low.rsplit("select", 1)[-1], maxsplit=1)[0]
    for item in filters:
        column = FILTER_COLUMNS.get(item.split(":", 1)[0])
        if column and column in select_part:
            return (f"问题只是换筛选条件，没有要求按筛选列分组；"
                    f"请从 SELECT 里去掉 {column}，只保留指标列。")
    return ""


# ---------- SQL 生成（LLM 主线；规则在 app/fallback.py） ----------
def _instructions_block(ctx: SchemaContext) -> str:
    """把执行约束渲染成独立 prompt 段；没有就整段省略。

    hint 走这里而不是拼进问题文本：问题要留给 `extract_keywords` / 检索 / 形状守卫，
    拼进去会让"占比口径：按品牌/品类拆分…"这类提示被当成新维度，改变检索到的指标，
    还会让"只换筛选不要 SELECT 筛选列"的守卫因为 dimension 非空而跳过。
    """
    lines = [f"- {text}" for text in ctx.instructions if text]
    if not lines:
        return ""
    return "执行约束（只影响 SQL 生成，不是用户问题的一部分）：\n" + "\n".join(lines) + "\n\n"


def _conditional_rules(question: str, ctx: SchemaContext, catalog: Catalog | None = None) -> str:
    """按“本次上下文里真实存在的东西”拼口径规则；不存在的表/场景一个字都不提。

    文案从 catalog 的 `instructions` 取，改口径不用改代码；catalog 缺失时退回
    `app.semantics` 的默认文案。静态 prompt 说“可以直接查 repurchase_customers”
    是幻觉源：复购表只在复购/阈值客户问题里进上下文。
    """
    rules: list[str] = []
    if is_share_question(question):
        text = catalog.instructions.get("share", DEFAULT_SHARE_INSTRUCTION) if catalog else DEFAULT_SHARE_INSTRUCTION
        rules.append(f"- {text}\n")
    if "repurchase_customers" in ctx.table_names():
        text = catalog.instructions.get("repurchase", DEFAULT_REPURCHASE_INSTRUCTION) if catalog else DEFAULT_REPURCHASE_INSTRUCTION
        rules.append(f"- {text}\n")
    return "".join(rules)


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
        f"{_instructions_block(ctx)}"
        "口径与格式（必须遵守）：\n"
        f"{_conditional_rules(question, ctx, catalog)}"
        "- 只输出一条 SELECT 语句（复杂问题可以用 WITH ... SELECT 的 CTE），不要解释，不要 markdown。\n"
        "- 指标口径以 schema 里给出的 metric SQL 为准。\n"
        "- GMV/成交额/销售额的金额列只能来自已支付订单：必须用 WHERE o.status = 'paid'，"
        "或在聚合里 SUM(CASE WHEN o.status = 'paid' THEN o.pay_amount ELSE 0 END)。\n"
        "- 不要直接 SUM(o.pay_amount) 和退款率混在同一条未过滤 status 的查询里；"
        "退款率按全部订单算，不要加 paid 过滤。\n"
        "- 成交额/订单数/客单价/销量/客户数只统计 o.status = 'paid'；退款率不要加这个过滤。\n"
        "- 日期列是 TEXT 'YYYY-MM-DD'；按月分组用 substr(o.order_date, 1, 7)，不要用 strftime。\n"
        "- 本月/上个月/今年/去年/最近30天用 date('now', ...) 计算，不要把 'now' 当字符串 substr：\n"
        "  本月 o.order_date >= date('now','start of month')；\n"
        "  上个月 >= date('now','start of month','-1 month') 且 < date('now','start of month')；\n"
        "  今年 >= date('now','start of year')；去年 >= date('now','start of year','-1 year') 且 < date('now','start of year')；\n"
        "  最近30天 >= date('now','-30 day')；\n"
        "  只有指定月份（YYYY-MM）才用 substr(o.order_date,1,7) = 'YYYY-MM'。\n"
        "- 只 SELECT 问题要求的维度列和指标列，不要额外列：\n"
        "  占比/比例题只输出维度列 + 占比列，不要额外输出分子、分母或中间聚合列；\n"
        "  分组 Top-N 只输出分组列 + 对象列 + 指标列，不要额外输出 product_id 等 ID 列；\n"
        "  排名指标必须与问题一致：销售额/GMV 用 SUM(o.pay_amount)，"
        "不要用 item_amount、quantity 或 quantity*unit_price 代替。\n"
        "- 问题明确指定了输出格式时严格照做；季度用 YYYY-Qn（如 2026-Q1），不要只输出月份数字。\n"
        "- 客户排名/明细只输出客户名和指标；除非问题明确要客户编号，不要额外 SELECT customer_id。\n"
        "- 复购/阈值客户统计（如“下过至少两单的客户数”或“GMV超过某值的客户按地区统计人数”）："
        "先在子查询按 customer_id 聚合 + HAVING（订单数 >= 2 或 SUM(pay_amount) > 阈值），"
        "再在外层按地区 COUNT(DISTINCT customer_id)；不要按 customer_id + 地区分组返回客户明细，"
        "也不要把 HAVING 套在地区汇总上。\n"
        "- 如果问题只问「有哪些 / 列出」某个维度、没有指标：用 SELECT DISTINCT 只选该维度列，"
        "不要臆造指标，也不要 GROUP BY。\n"
        "- 有指标的问题必须 SELECT 指标列（可以再带分组维度），不要只返回维度取值。\n"
        "- 问题里的词在 schema 中没有对应表时（例如「门店」），不要编造表；用已有的地区 / 客户维度近似。\n"
        "- 如果问题只是换筛选条件（例如「换成华南」），不要额外 SELECT 被筛选的列，除非问题要求按它分组。\n"
        "- 问题里有 排名/Top/最多/前N 时：ORDER BY 指标 DESC LIMIT 10；"
        "但如果要求「每个分组内」的 Top-N（例如每个品类销售额前两名商品），"
        "用 WITH 分两步，再用 ROW_NUMBER() OVER (PARTITION BY 分组列 ORDER BY 指标 DESC) "
        "筛 rn <= N，不要只 ORDER BY 返回全部分组，也不要用全局 LIMIT 10。"
        "否则不要 ORDER BY、不要 LIMIT。\n"
        "- JOIN 只能使用 schema 里列出的表关系。\n\n"
        "示例：\n"
        "问题：各地区的GMV\n"
        "SQL：SELECT r.region_name, ROUND(SUM(o.pay_amount), 2) AS gmv "
        "FROM orders o JOIN regions r ON r.region_id = o.region_id "
        "WHERE o.status = 'paid' GROUP BY r.region_name\n\n"
        "问题：华东地区有哪些商品类别\n"
        "SQL：SELECT DISTINCT p.category FROM orders o JOIN order_items i ON i.order_id = o.order_id "
        "JOIN products p ON p.product_id = i.product_id JOIN regions r ON r.region_id = o.region_id "
        "WHERE o.status = 'paid' AND r.region_name = '华东'\n\n"
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
class LLMGenerator:
    """主路径生成器：模型写 SQL，失败时把错误带回去重写。

    `fallback` 只在没配 key / 请求失败 / 模型空返回时启用（`app/fallback.py` 的
    `OfflineFallback`，模板拼 SQL），此时 `last_engine` 会变成 `rule`，上层能看出来。
    """

    def __init__(self, fallback: OfflineFallback | None = None) -> None:
        self.fallback = fallback or OfflineFallback()
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

    允许 `WITH ... SELECT`：复杂问题（分组 Top-N、多步聚合）需要 CTE + 窗口函数，
    只放行只读的 WITH；DML 关键字仍然在 FORBIDDEN 里被拦。
    """
    s = sql.strip().rstrip(";")
    low = s.lower()
    if not (low.startswith("select") or low.startswith("with")):
        return False, "只允许 SELECT 或 WITH ... SELECT"
    if low.startswith("with") and "select" not in low:
        return False, "只允许 SELECT 或 WITH ... SELECT"
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
    # WITH 里的 CTE 名字不是物理表：先摘出来，否则 `FROM ranked` 会被当成未知表
    cte_names = set(re.findall(
        r"(?:\bwith\b|,)\s*([a-z_][a-z0-9_]*)\s*(?:\([^)]*\))?\s+as\s*\(", low))
    used = set(re.findall(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", low)) - cte_names
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
    mode: str = "single",
    conclusion: bool = False,
    hint: str = "",
) -> SQLPlan:
    """AC-1.1：失败带报错重试；AC-1.4：每次尝试都要过校验；AC-1.5：max_repair=0 只跑一次。

    `mode="analysis"` 走有界 plan-and-execute（`app/analysis.py`）：Planner 失败或离线模式时
    回退本函数的单 SQL 快路径，并把原因放进 `analysis_error`；默认 `mode="single"`，结构不变。
    `conclusion=True` 时，单 SQL 成功后再可选调用一次模型写 1–3 句结论（Step 3，默认关闭）；
    结论失败只写 `conclusion_error`，不影响 SQL 的 `ok/rows`。

    `hint` 是执行约束（份额口径、规划约束）：**只进 SQL prompt，不进问题文本**。
    拼进问题会污染槽位抽取、检索和形状守卫（实测会多抽一个 dimension、把指标从
    order_count 换成 refund_rate），所以走 `SchemaContext.instructions` 独立通道。

    顺序（AC-3.6）：
    **role → 按角色裁剪 catalog → session 槽位继承 → classify → 检索/build_context → 生成**。
    不能先路由："那上个月呢"字面上没有指标，先路由会被判成 clarify。
    列过滤必须在检索之前，否则隐藏列会进 context / prompt（AC-4.1）。
    """
    if mode not in ("single", "analysis"):
        raise ValueError(f"未知模式：{mode!r}（可选 single / analysis）")
    if mode == "analysis":
        # 分析模式已有综合结论；API 同时传 conclusion=true 也不叠加
        conclusion = False
    started = time.time()
    full_catalog = catalog or load_catalog()
    hidden = permissions.hidden_columns(full_catalog, role)  # 未知角色在这里抛 ValueError
    catalog = permissions.visible_catalog(full_catalog, role)  # AC-4.1：不可见列根本不在 catalog 里
    retriever = retriever or SchemaRetriever(catalog)
    gen = generator if generator is not None else (LLMGenerator() if use_llm else OfflineFallback())
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
    plan.mode = mode
    if route.intent != "ask":  # AC-2.2：只有 ask 生成 SQL
        plan.reply = reply_for(route)
        plan.clarify_options = route.clarify_options
        plan.context = None  # 非 ask 不检索、也不返回 schema（AC-4.1）
        if mode == "analysis":
            # 分析模式也要能回答澄清/越权：直接复用话术，不硬跑步骤
            plan.answer = plan.reply
        if route.intent == "unsafe":  # AC-4.4：越权/写操作尝试进审计
            permissions.AUDIT_LOG.record(
                role=role, question=question, session_id=plan.session_id, reason=route.reason
            )
        plan.elapsed_ms = int((time.time() - started) * 1000)
        return plan

    # 只有问数才检索，且检索用的是裁剪后的 catalog（AC-4.1）
    ctx, keywords = build_context(rewritten, catalog, retriever, keywords=slots)
    if hint:
        # 执行约束走 context 的独立通道：问题文本保持干净，槽位/检索/形状守卫只看用户问题
        ctx.instructions = (*ctx.instructions, hint)
    plan.context = ctx
    plan.keywords = keywords

    if session is not None:
        session.add(question, slots)  # AC-3.1：只把真正问数的一轮记成上下文

    # 分析模式：Planner/Executor/Synthesizer 每步复用本函数；失败回退下面的单 SQL 快路径
    if mode == "analysis":
        if not use_llm:
            plan.mode = "single"
            plan.analysis_error = "分析模式需要模型规划；当前是离线/规则模式，已回退单 SQL"
        else:
            from app.analysis import run_analysis  # 局部导入：analysis 会把 ask 当执行器传回来
            result = run_analysis(
                rewritten,
                role=role,
                catalog=full_catalog,
                retriever=retriever,
                ask_fn=ask,
                use_llm=use_llm,
            )
            if result.planner_ok:
                plan.mode = "analysis"
                plan.steps = result.steps
                plan.answer = result.answer
                plan.evidence = result.evidence
                plan.analysis_error = result.error
                plan.analysis_warning = result.warning
                plan.usage = result.usage
                plan.ok = any(step.ok for step in result.steps)
                plan.engine = "analysis"
                plan.elapsed_ms = result.elapsed_ms
                return plan
            plan.mode = "single"
            plan.analysis_error = result.error

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
        if ok:
            # 语义形状检查：筛选条件丢了 / 只换筛选却多选了筛选列 → 当 validate 失败，带话术重写
            shape_error = _shape_feedback(raw_sql, keywords, question)
            if shape_error:
                ok, message = False, shape_error
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
    if conclusion and plan.intent == "ask" and plan.ok:
        if plan.engine != "llm":
            plan.conclusion_error = "规则 fallback 不生成自动结论"
        elif not plan.rows:
            plan.conclusion_error = "没有结果可总结"
        else:
            from app.conclusion import summarize  # 局部导入，保持 agent 的依赖关系简单
            # 多轮里原始问句可能是“那上个月呢”；用改写后的自包含问句，结论才不会丢上下文
            answer, error, usage = summarize(
                plan.rewritten_question or question, plan.columns, plan.rows
            )
            plan.answer = answer
            plan.conclusion = True
            plan.conclusion_error = error
            plan.conclusion_usage = usage
    plan.elapsed_ms = int((time.time() - started) * 1000)
    return plan
