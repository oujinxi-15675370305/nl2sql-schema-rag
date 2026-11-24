"""M4：角色、列级权限、掩码与审计。对照 docs/SPEC.md 的 AC-4.1 ~ AC-4.4。

顺序（AC-3.6）：**role → 按角色裁剪 catalog → 会话槽位 → classify → 检索/生成**。
列过滤必须在检索之前：`SchemaContext.ddl()` 只渲染命中列，晚过滤会让隐藏列进 prompt；
槽位抽取也要用裁剪后的 catalog，否则隐藏列的同义词还会被抽出来。

三条边界（写进 docs/decisions.md）：
- role 按请求传、**不存进 Session**。槽位只描述业务维度/指标，从不写列名，所以跨角色继承不会泄漏隐藏列。
- 掩码在服务端结果行上做，不写进 SQL（不靠 `substr(phone,1,3)||'****'`，那样列名还在、口径也变了）。
- 列白名单用"隐藏列名 + 同义词"匹配，不做完整 SQL 解析：命中即拒，话术不带列名。
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, replace
from typing import Deque, Iterable, Sequence

from app.domain import Catalog

ANALYST = "analyst"
VIEWER = "viewer"
ROLES = (ANALYST, VIEWER)
DEFAULT_ROLE = ANALYST

# 角色 → 敏感列的处理方式：
# hidden：完全不可见（不进 SQL / 结果 / 错误 / prompt）；
# masked：可见，但结果行要在服务端打码。
# 没被任何角色列进 masked 的敏感列默认 hidden —— 新加敏感列时"忘了配置"也不会泄漏。
ROLE_POLICIES: dict[str, dict[str, tuple[str, ...]]] = {
    ANALYST: {"hidden": (), "masked": ("customer_phone",)},
    VIEWER: {"hidden": ("customer_phone",), "masked": ()},
}


def policy_for(role: str) -> dict[str, tuple[str, ...]]:
    try:
        return ROLE_POLICIES[role]
    except KeyError as exc:
        raise ValueError(f"未知角色：{role}") from exc


def hidden_columns(catalog: Catalog, role: str) -> set[str]:
    """该角色看不到的列名（全局按列名算，demo 里没有跨表同名列）。"""
    policy = policy_for(role)
    hidden = set(policy["hidden"])
    for table in catalog.tables:
        for column in table.columns:
            if column.sensitive and column.name not in policy["masked"]:
                hidden.add(column.name)
    return hidden


def masked_column_names(role: str) -> tuple[str, ...]:
    return policy_for(role)["masked"]


def visible_catalog(catalog: Catalog, role: str = DEFAULT_ROLE) -> Catalog:
    """按角色裁掉隐藏列。表本身保留（join 还要用），指标若依赖隐藏列也一并去掉。"""
    hidden = hidden_columns(catalog, role)
    tables = tuple(
        replace(table, columns=tuple(c for c in table.columns if c.name not in hidden))
        for table in catalog.tables
    )
    metrics = tuple(
        metric
        for metric in catalog.metrics
        if not any(part.split(".")[-1] in hidden for part in metric.requires)
    )
    return Catalog(tables=tables, metrics=metrics, instructions=dict(catalog.instructions))


def denied_terms(catalog: Catalog, role: str) -> tuple[str, ...]:
    """隐藏列的列名与同义词，用来在路由阶段拦住"我要看手机号"这类显式请求。"""
    hidden = hidden_columns(catalog, role)
    terms: list[str] = []
    for table in catalog.tables:
        for column in table.columns:
            if column.name in hidden:
                terms.append(column.name)
                terms.extend(s for s in column.synonyms if s)
    return tuple(dict.fromkeys(terms))


def mask_value(value: object) -> str:
    """手机号之类的掩码：138****0047。NULL 保持 NULL 语义（返回空串）。"""
    if value is None:
        return ""
    text = str(value)
    if len(text) <= 4:
        return "***"
    if len(text) >= 8:
        return f"{text[:3]}****{text[-4:]}"
    return f"{text[:2]}***{text[-1:]}"


PHONE_VALUE_RE = re.compile(r"^1\d{10}$")  # 值模式兜底：别名/表达式没拦住时也不回原值


def mask_rows(
    columns: Sequence[str],
    rows: Iterable[Sequence[object]],
    masked: Iterable[str],
) -> tuple[list[tuple], list[str]]:
    """在结果行上掩码，返回 (新行, 实际被掩的列名)。

    先按结果列名掩码；只要这个角色配了掩码列，再按"手机号形状"扫一遍值兜底——
    生成器把列改名或包一层函数时，列名对不上，靠值这层仍然不会把真实号码带出去。
    """
    masked = tuple(masked)
    targets = {c for c in masked if c in set(columns)}
    indexes = [i for i, name in enumerate(columns) if name in targets]
    masked_rows = []
    for row in rows:
        values = list(row)
        for i in indexes:
            values[i] = mask_value(values[i])
        if masked:
            for i, value in enumerate(values):
                if isinstance(value, str) and PHONE_VALUE_RE.match(value):
                    values[i] = mask_value(value)
        masked_rows.append(tuple(values))
    return masked_rows, sorted(targets)


def redact_sql(sql: str, names: Iterable[str]) -> str:
    """把 SQL 里的隐藏列名打码，避免出现在 attempts / 审计 / 前端。"""
    redacted = sql
    for name in names:
        if name:
            redacted = re.sub(rf"\b{re.escape(name)}\b", "***", redacted)
    return redacted


@dataclass(frozen=True)
class AuditEntry:
    """一次越权尝试。AC-4.4：内存环形缓冲，重启即清空。"""

    ts: str
    role: str
    question: str
    session_id: str
    reason: str
    sql: str

    def as_dict(self) -> dict:
        return asdict(self)


class AuditLog:
    def __init__(self, max_entries: int = 100) -> None:
        self.max_entries = max(1, max_entries)
        self._items: Deque[AuditEntry] = deque(maxlen=self.max_entries)
        self._lock = threading.Lock()

    def record(self, *, role: str, question: str, reason: str,
               session_id: str = "", sql: str = "") -> AuditEntry:
        entry = AuditEntry(
            ts=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            role=role,
            question=question,
            session_id=session_id,
            reason=reason,
            sql=sql,
        )
        with self._lock:
            self._items.append(entry)
        return entry

    def entries(self) -> list[AuditEntry]:
        with self._lock:
            return list(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


AUDIT_LOG = AuditLog()  # 进程内单例：server 和 ask() 共用一个
