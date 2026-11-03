"""目录（catalog）加载：把 JSON 里的表/列/指标变成领域对象。"""
from __future__ import annotations

import json
from pathlib import Path

from app.domain import Catalog, Column, Metric, Table

DEFAULT_PATH = Path(__file__).with_name("catalog.json")


def load_catalog(path: str | Path | None = None) -> Catalog:
    raw = json.loads(Path(path or DEFAULT_PATH).read_text(encoding="utf-8"))
    tables: list[Table] = []
    for t in raw.get("tables", []):
        columns = tuple(
            Column(
                table=t["name"],
                name=c["name"],
                type=c.get("type", "TEXT"),
                comment=c.get("comment", ""),
                synonyms=tuple(c.get("synonyms", ())),
                is_dimension=bool(c.get("is_dimension", False)),
                is_measure=bool(c.get("is_measure", False)),
            )
            for c in t.get("columns", [])
        )
        tables.append(
            Table(
                name=t["name"],
                comment=t.get("comment", ""),
                columns=columns,
                synonyms=tuple(t.get("synonyms", ())),
            )
        )
    metrics = tuple(
        Metric(
            name=m["name"],
            comment=m.get("comment", ""),
            sql=m["sql"],
            synonyms=tuple(m.get("synonyms", ())),
            requires=tuple(m.get("requires", ())),
        )
        for m in raw.get("metrics", [])
    )
    return Catalog(tables=tuple(tables), metrics=metrics)
