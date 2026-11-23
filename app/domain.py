"""领域对象。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Column:
    table: str
    name: str
    type: str
    comment: str = ""
    synonyms: tuple[str, ...] = ()
    is_dimension: bool = False
    is_measure: bool = False
    sensitive: bool = False  # M4：敏感列（按角色隐藏或掩码）

    def text(self) -> str:
        parts = [self.name, self.comment, *self.synonyms]
        return " ".join(p for p in parts if p)


@dataclass(frozen=True)
class Table:
    name: str
    comment: str
    columns: tuple[Column, ...] = ()
    synonyms: tuple[str, ...] = ()

    def text(self) -> str:
        parts = [self.name, self.comment, *self.synonyms]
        return " ".join(p for p in parts if p)

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)


@dataclass(frozen=True)
class Metric:
    name: str
    comment: str
    sql: str
    synonyms: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()

    def text(self) -> str:
        parts = [self.name, self.comment, *self.synonyms]
        return " ".join(p for p in parts if p)


@dataclass
class Catalog:
    tables: tuple[Table, ...]
    metrics: tuple[Metric, ...]
    relations: tuple[tuple[str, str], ...] = ()

    def table(self, name: str) -> Table | None:
        return next((t for t in self.tables if t.name == name), None)

    def metric(self, name: str) -> Metric | None:
        return next((m for m in self.metrics if m.name == name), None)


@dataclass(frozen=True)
class Hit:
    """一条检索命中，kind 为 metric / table / column。"""

    kind: str
    key: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class SchemaContext:
    """送进 prompt 之前裁剪好的 schema 上下文。"""

    metrics: list[Hit] = field(default_factory=list)
    tables: list[Hit] = field(default_factory=list)
    columns: list[Hit] = field(default_factory=list)

    def table_names(self) -> list[str]:
        return [h.key for h in self.tables]

    def metric_names(self) -> list[str]:
        return [h.key for h in self.metrics]

    def ddl(self, catalog: Catalog) -> str:
        """只渲染命中的表与列，控制 prompt 长度。

        两类列即使没被检索命中也要带上：
        - 主外键（order_id / customer_id / region_id / product_id）和 status/order_date，
          否则模型看不到 JOIN 键，会自己编 `o.order_id = c.customer_id` 这种条件；
        - 命中指标 `requires` 里点名的列（比如客单价的 pay_amount）。
        """
        key_columns = {"order_id", "customer_id", "region_id", "product_id", "status", "order_date"}
        required: dict[str, set[str]] = {}
        for hit in self.metrics:
            metric = catalog.metric(hit.key)
            if metric is None:
                continue
            for ref in metric.requires:
                table_name, _, column = ref.partition(".")
                if table_name and column:
                    required.setdefault(table_name, set()).add(column)

        lines: list[str] = []
        wanted_columns: dict[str, set[str]] = {}
        for h in self.columns:
            table = h.payload.get("table")
            if table:
                wanted_columns.setdefault(table, set()).add(h.key)
        rendered = set(self.table_names())
        for name in self.table_names():
            table = catalog.table(name)
            if not table:
                continue
            keep = wanted_columns.get(name)
            force = required.get(name, set()) | key_columns
            cols = [c for c in table.columns if not keep or c.name in keep or c.name in force]
            lines.append(f"-- {table.name}: {table.comment}")
            for c in cols:
                flag = " [维度]" if c.is_dimension else (" [度量]" if c.is_measure else "")
                lines.append(f"  {c.name} {c.type}{flag}  -- {c.comment}")
            lines.append("")
        relations = [
            (left, right)
            for left, right in catalog.relations
            if left.split(".", 1)[0] in rendered and right.split(".", 1)[0] in rendered
        ]
        if relations:
            lines.append("-- 表关系（JOIN 条件，只能按这些连表）")
            lines.extend(f"--   {left} = {right}" for left, right in relations)
            lines.append("")
        for h in self.metrics:
            metric = catalog.metric(h.key)
            if metric:
                lines.append(f"-- 指标 {metric.name}: {metric.comment} => {metric.sql}")
        return "\n".join(lines).strip()


@dataclass
class AnalysisStep:
    """分析模式的一步：子问题 + 实际执行的 SQL 与结果预览。"""

    id: str
    question: str
    purpose: str = ""
    depends_on: list[str] = field(default_factory=list)
    sql: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)  # UI 预览行（最多 20 行）
    row_count: int = 0  # 实际取回行数（执行层最多 200 行）
    ok: bool = False
    skipped: bool = False  # 被澄清/越权/预算拦下，没有硬跑
    error: str = ""
    intent: str = ""
    engine: str = ""
    elapsed_ms: int = 0


@dataclass
class AnalysisResult:
    """Planner → Executor → Synthesizer 的汇总；planner_ok=False 时回退单 SQL。"""

    steps: list[AnalysisStep] = field(default_factory=list)
    answer: str = ""
    evidence: list[str] = field(default_factory=list)
    error: str = ""
    warning: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    elapsed_ms: int = 0
    planner_ok: bool = True


@dataclass
class SQLPlan:
    question: str
    sql: str = ""
    ok: bool = False
    error: str = ""
    failure_stage: str = ""  # M5：最后一次失败发生在哪一步（validate/execute/timeout）；成功时清空
    rows: list[tuple] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    context: SchemaContext | None = None
    keywords: dict[str, list[str]] = field(default_factory=dict)
    attempts: list[dict] = field(default_factory=list)
    engine: str = ""  # rule / llm / custom，用来确认这次真的走了模型
    intent: str = "ask"
    reply: str = ""
    clarify_options: list[str] = field(default_factory=list)
    session_id: str = ""  # M3：会话标识（没有会话时为空）
    rewritten_question: str = ""  # M3：补上继承槽位后的自包含问句
    inherited_slots: list[str] = field(default_factory=list)  # M3：本轮从上文继承的槽位名
    role: str = "analyst"  # M4：本次请求的角色
    masked_columns: list[str] = field(default_factory=list)  # M4：被服务端掩码的列
    # 分析模式（mode="analysis"）：快路径这些字段保持默认值，响应结构不变
    mode: str = "single"
    steps: list[AnalysisStep] = field(default_factory=list)
    answer: str = ""
    evidence: list[str] = field(default_factory=list)
    analysis_error: str = ""  # 规划失败/综合失败；规划失败时回退单 SQL
    analysis_warning: str = ""
    usage: dict[str, int] = field(default_factory=dict)
