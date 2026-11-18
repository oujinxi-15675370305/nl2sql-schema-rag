"""M3：多轮会话与槽位继承。对照 docs/SPEC.md 的 AC-3.1 ~ AC-3.6。

`ask()` 里的顺序必须是 **session → 槽位继承 → classify → 生成**。
先路由再继承的话，"那上个月呢"字面上抽不到指标，会被判成 clarify，多轮断在第二轮。

继承规则（都在 `inherit_slots` 里，可解释、可测）：
- 指标：同一指标接着说，或者有指代词（那/呢/换成…）才继承；换了指标又没指代 → 开新话题（AC-3.4）。
- 维度：没给就继承；给了就覆盖；句子里有"总/整体/一共"且没有"各/按"时，视为显式要求不分组。
- 筛选：按类型覆盖（"换成华南"只换地区，上文的品类筛选保留）。
- 时间：没给就继承；"那上个月呢"里的"上个月"覆盖上文的月份。

会话只在进程内存里（AC-3.5）：不落盘，`SessionStore` 也有上限，避免长跑时无限增长。
"""
from __future__ import annotations

import re
import threading
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Deque, Iterable

from app.slots import TIME_HINTS, leftover

MAX_TURNS = 5  # AC-3.1：默认保留最近 5 轮
MAX_SESSIONS = 200  # 进程内会话数上限，只防泄漏，不是业务限制

SLOT_KEYS = ("metric", "dimension", "filter", "time")

# 指代 / 改写信号：出现这些词说明这句在接着说上文
ANAPHORA = re.compile(r"(那|这|它|其|呢|换成|再看|还看|同样|一样|同上|还有|分别|其中)")

# "总/整体/一共"是对"不分组"的显式声明，要覆盖上文继承来的维度；
# 但"各地区的 GMV 总额"里既有"总"又有"各"，仍然要分组。
TOTAL_RESET = re.compile(r"(总共|总|一共|累计|整体|全部|总体)")
GROUP_WORDS = re.compile(r"(各|每|按|分|排名|top|分别|哪些)", re.I)

# 槽位 → 中文标签。改写后的问句要能重新抽出同一组槽位（生成器按问句工作），
# 所以这些标签必须落在 app/slots.py 的词表里，test_session 有回环测试钉住。
METRIC_LABELS = {
    "gmv": "GMV",
    "order_count": "订单数",
    "aov": "客单价",
    "refund_rate": "退款率",
    "units": "销量",
    "customer_count": "客户数",
}
DIMENSION_LABELS = {
    "region": "各地区",
    "category": "各品类",
    "brand": "各品牌",
    "month": "按月",
    "product": "各商品",
    "customer": "各客户",
}
TIME_LABELS = {
    "last_month": "上个月",
    "this_month": "本月",
    "this_year": "今年",
    "last_year": "去年",
    "last_30d": "最近30天",
}


def _empty_slots() -> dict[str, list[str]]:
    return {key: [] for key in SLOT_KEYS}


def _copy(slots: dict[str, list[str]] | None) -> dict[str, list[str]]:
    out = _empty_slots()
    for key in SLOT_KEYS:
        out[key] = list((slots or {}).get(key, []))
    return out


def _is_anaphoric(question: str) -> bool:
    """判指代前先剥掉时间词。

    不然"这个月退款率是多少"里的"这"会被当成"接着说"，新问题被套上上文的维度。
    """
    text = question
    for word in TIME_HINTS:
        text = text.replace(word, "")
    return bool(ANAPHORA.search(text))


def _should_inherit(question: str, current: dict[str, list[str]], previous: dict[str, list[str]]) -> bool:
    """这句算不算"接着说上文"。"""
    cur = set(current.get("metric", []))
    prev = set(previous.get("metric", []))
    if cur & prev:
        return True  # 同一个指标：可以接着补时间/地区/维度
    # 纯槽位的补充问法（"上个月"/"按品类看下"/"华南"）：省略主语也该接上，
    # 但去掉槽位词后还剩新名词（"上个月工资发了多少"）就不能猜。
    if (
        not cur
        and any(current.get(key) for key in ("dimension", "filter", "time"))
        and not leftover(question)
    ):
        return True
    # 换了指标或没给指标：只有指代词才算承接，否则是新话题（AC-3.4）
    return _is_anaphoric(question)


def _clears_dimension(question: str) -> bool:
    return bool(TOTAL_RESET.search(question)) and not GROUP_WORDS.search(question)


def _merge_filters(previous: list[str], current: list[str]) -> list[str]:
    """筛选按类型覆盖：显式给出的类型替换上文同类型，没给的保留。"""
    kinds = {item.split(":", 1)[0] for item in current}
    kept = [item for item in previous if item.split(":", 1)[0] not in kinds]
    return kept + list(current)


def inherit_slots(
    question: str,
    current: dict[str, list[str]],
    previous: dict[str, list[str]] | None,
) -> tuple[dict[str, list[str]], list[str]]:
    """把上文缺失的槽位补进当前问题。

    返回 `(合并后的槽位, 被继承的槽位名)`；第二个值用来告诉前端/日志"哪些是上文带的"。
    """
    merged = _copy(current)
    if not previous or not _should_inherit(question, merged, previous):
        return merged, []

    inherited: list[str] = []
    if not merged["metric"] and previous.get("metric"):
        merged["metric"] = list(previous["metric"])
        inherited.append("metric")
    if not merged["dimension"] and previous.get("dimension") and not _clears_dimension(question):
        merged["dimension"] = list(previous["dimension"])
        inherited.append("dimension")
    filters = _merge_filters(previous.get("filter", []), merged["filter"])
    if filters != merged["filter"]:
        merged["filter"] = filters
        inherited.append("filter")
    if not merged["time"] and previous.get("time"):
        merged["time"] = list(previous["time"])
        inherited.append("time")
    return merged, inherited


def _label(kind: str, value: str) -> str:
    if kind == "metric":
        return METRIC_LABELS.get(value, value)
    if kind == "dimension":
        return DIMENSION_LABELS.get(value, value)
    if kind == "time":
        if value.startswith("month:"):
            return value.split(":", 1)[1]
        return TIME_LABELS.get(value, value)
    return value.split(":", 1)[1] if ":" in value else value  # filter:region:华东 → 华东


def describe_slots(slots: dict[str, list[str]], only: Iterable[str] | None = None) -> list[str]:
    """把槽位翻成中文短语，用于改写问句和前端展示。"""
    wanted = None if only is None else set(only)
    out: list[str] = []
    for kind in SLOT_KEYS:
        if wanted is not None and kind not in wanted:
            continue
        for value in slots.get(kind, []):
            label = _label(kind, value)
            if label and label not in out:
                out.append(label)
    return out


def rewrite_question(question: str, slots: dict[str, list[str]], inherited: Iterable[str]) -> str:
    """把"那上个月呢"改写成自包含的问句，给检索和大模型用。

    没有继承任何槽位就原样返回；有的话在后面补一句"承接上文：GMV、各地区"。
    这样生成器（以及配了 LLM 时的 prompt）看到的是一个完整问题，不用自己猜。
    """
    inherited = list(inherited)
    labels = describe_slots(slots, only=inherited)
    if not labels:
        return question
    return f"{question}（承接上文：{'、'.join(labels)}）"


@dataclass
class Turn:
    """一轮问答：原始问题 + 解析（或继承）后的槽位。"""

    question: str
    slots: dict[str, list[str]] = field(default_factory=_empty_slots)


class Session:
    """一次会话。只放内存里，进程结束就没了（AC-3.5）。

    `ThreadingHTTPServer` 下同一会话可能并发送达，读写都加锁。
    """

    def __init__(self, session_id: str = "", max_turns: int = MAX_TURNS) -> None:
        self.id = session_id or uuid.uuid4().hex[:12]
        self.max_turns = max(1, max_turns)
        self.turns: Deque[Turn] = deque(maxlen=self.max_turns)
        self._lock = threading.Lock()

    def add(self, question: str, slots: dict[str, list[str]]) -> Turn:
        turn = Turn(question=question, slots=_copy(slots))
        with self._lock:
            self.turns.append(turn)  # 超上限自动淘汰最旧的（AC-3.5）
        return turn

    def last(self) -> Turn | None:
        with self._lock:
            return self.turns[-1] if self.turns else None

    def last_slots(self) -> dict[str, list[str]] | None:
        turn = self.last()
        return _copy(turn.slots) if turn else None

    def clear(self) -> None:
        with self._lock:
            self.turns.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self.turns)


class SessionStore:
    """HTTP 层的会话表：按 session_id 取，没有就新建。

    内存 + 上限：demo 不落盘，重启即清空；会话数超了淘汰最久没用的那个，
    避免有人狂刷 session_id 把内存打满。
    """

    def __init__(self, max_sessions: int = MAX_SESSIONS, max_turns: int = MAX_TURNS) -> None:
        self.max_sessions = max(1, max_sessions)
        self.max_turns = max_turns
        self._items: "OrderedDict[str, Session]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, session_id: str | None = None) -> Session:
        with self._lock:
            if session_id:
                session_id = session_id[:64]
                if session_id in self._items:
                    self._items.move_to_end(session_id)
                    return self._items[session_id]
            session = Session(session_id or "", max_turns=self.max_turns)
            self._items[session.id] = session
            while len(self._items) > self.max_sessions:
                self._items.popitem(last=False)
            return session

    def __len__(self) -> int:
        return len(self._items)
