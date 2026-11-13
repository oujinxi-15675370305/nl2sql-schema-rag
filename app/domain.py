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
        """只渲染命中的表与列，控制 prompt 长度。"""
        lines: list[str] = []
        wanted_columns: dict[str, set[str]] = {}
        for h in self.columns:
            table = h.payload.get("table")
            if table:
                wanted_columns.setdefault(table, set()).add(h.key)
        for name in self.table_names():
            table = catalog.table(name)
            if not table:
                continue
            keep = wanted_columns.get(name)
            cols = [c for c in table.columns if not keep or c.name in keep]
            lines.append(f"-- {table.name}: {table.comment}")
            for c in cols:
                flag = " [维度]" if c.is_dimension else (" [度量]" if c.is_measure else "")
                lines.append(f"  {c.name} {c.type}{flag}  -- {c.comment}")
            lines.append("")
        for h in self.metrics:
            metric = catalog.metric(h.key)
            if metric:
                lines.append(f"-- 指标 {metric.name}: {metric.comment} => {metric.sql}")
        return "\n".join(lines).strip()


@dataclass
class SQLPlan:
    question: str
    sql: str = ""
    ok: bool = False
    error: str = ""
    rows: list[tuple] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    context: SchemaContext | None = None
    keywords: dict[str, list[str]] = field(default_factory=dict)
    attempts: list[dict] = field(default_factory=list)
    intent: str = "ask"
    reply: str = ""
    clarify_options: list[str] = field(default_factory=list)
