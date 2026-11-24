"""目录（catalog）加载：把 JSON 里的表/列/指标变成领域对象。"""
from __future__ import annotations

import json
from pathlib import Path

from app.domain import Catalog, Column, Metric, Table

DEFAULT_PATH = Path(__file__).with_name("catalog.json")


def _text(value: object) -> str:
    """语义字段都是手写 JSON：null 不能变成字符串 "None" 混进 prompt。"""
    return "" if value is None else str(value).strip()


def _flag(value: object, default: bool = True) -> bool:
    """`"additive": "false"` 这种手写字符串也要当成 False——口径字段写反比缺省更危险。"""
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no", "off")
    return default if value is None else bool(value)


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
                sensitive=bool(c.get("sensitive", False)),
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
            additive=_flag(m.get("additive", True)),
            grain=_text(m.get("grain")),
            warning=_text(m.get("warning")),
        )
        for m in raw.get("metrics", [])
    )
    return Catalog(
        tables=tuple(tables),
        metrics=metrics,
        relations=tuple(tuple(r) for r in raw.get("relations", ())),
        instructions={
            str(key): str(value)
            for key, value in (raw.get("instructions") or {}).items()
            if isinstance(value, str) and value.strip()
        },
    )
