"""三路检索（指标/表/列）+ RRF 融合。

向量是字符 n-gram 哈希近似，纯离线；换成 Qdrant 只需替换 DenseIndex。
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass

from app.domain import Catalog, Hit

CJK = re.compile(r"[\u4e00-\u9fff]+")
LATIN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def tokenize(text: str) -> list[str]:
    """中文 2-gram，英文按词。不引第三方分词器，省得部署时踩坑。"""
    text = text.lower()
    tokens: list[str] = []
    for run in CJK.findall(text):
        tokens.extend(run[i : i + 2] for i in range(max(0, len(run) - 1)))
        if len(run) <= 6:
            tokens.append(run)
    tokens.extend(m.group(0).lower() for m in LATIN.finditer(text))
    return tokens


def dense_vector(text: str, dim: int = 256) -> list[float]:
    """把文本散列到一个固定维度向量，用于离线近似语义检索。"""
    vec = [0.0] * dim
    for tok in tokenize(text):
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest()[:8], 16)
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def bm25(query: list[str], doc: list[str], k1: float = 1.5, b: float = 0.75,
         avg_len: float = 8.0) -> float:
    # 简化版 BM25：去掉 IDF，语料只有几十条时 IDF 区分度不大
    tf = Counter(doc)
    score = 0.0
    for term in set(query):
        if term not in tf:
            continue
        freq = tf[term]
        score += freq * (k1 + 1) / (freq + k1 * (1 - b + b * len(doc) / avg_len))
    return score


@dataclass
class Entry:
    kind: str
    key: str
    text: str
    payload: dict


class DenseIndex:
    """离线稠密索引：内存里存向量，检索用余弦。"""

    def __init__(self, entries: list[Entry]):
        self.entries = entries
        self.vectors = [dense_vector(e.text) for e in entries]

    def search(self, query: str, top_k: int = 10) -> list[Hit]:
        qv = dense_vector(query)
        scored = [
            Hit(kind=e.kind, key=e.key, score=cosine(qv, v), payload=e.payload)
            for e, v in zip(self.entries, self.vectors)
        ]
        scored.sort(key=lambda h: -h.score)
        return scored[:top_k]


class KeywordIndex:
    """离线关键词索引：BM25，列名/同义词精确命中时加权。"""

    def __init__(self, entries: list[Entry]):
        self.entries = entries
        self.docs = [tokenize(e.text) for e in entries]
        self.avg_len = sum(len(d) for d in self.docs) / max(1, len(self.docs))

    def search(self, query: str, top_k: int = 10) -> list[Hit]:
        q = tokenize(query)
        scored: list[Hit] = []
        for entry, doc in zip(self.entries, self.docs):
            score = bm25(q, doc, avg_len=self.avg_len)
            # 列名/表名直接出现在问题里，给一个强提升
            if entry.key.lower() in query.lower():
                score += 3.0
            if score > 0:
                scored.append(Hit(kind=entry.kind, key=entry.key, score=score, payload=entry.payload))
        scored.sort(key=lambda h: -h.score)
        return scored[:top_k]


def rrf(results: list[list[Hit]], k: int = 60, top_k: int = 10) -> list[Hit]:
    """Reciprocal Rank Fusion：多路结果按名次融合，不用各自分数校准。"""
    merged: dict[tuple[str, str], float] = {}
    payloads: dict[tuple[str, str], dict] = {}
    for hits in results:
        for rank, hit in enumerate(hits, start=1):
            key = (hit.kind, hit.key)
            merged[key] = merged.get(key, 0.0) + 1.0 / (k + rank)
            payloads[key] = hit.payload
    fused = [
        Hit(kind=kind, key=key, score=score, payload=payloads[(kind, key)])
        for (kind, key), score in merged.items()
    ]
    fused.sort(key=lambda h: -h.score)
    return fused[:top_k]


class SchemaRetriever:
    """三路检索入口。"""

    def __init__(self, catalog: Catalog):
        metric_entries = [
            Entry("metric", m.name, m.text(), {"comment": m.comment, "sql": m.sql})
            for m in catalog.metrics
        ]
        table_entries = [
            Entry("table", t.name, t.text(), {"comment": t.comment}) for t in catalog.tables
        ]
        column_entries = [
            Entry("column", c.name, c.text(), {"table": c.table, "comment": c.comment})
            for t in catalog.tables
            for c in t.columns
        ]
        self.metrics = (DenseIndex(metric_entries), KeywordIndex(metric_entries))
        self.tables = (DenseIndex(table_entries), KeywordIndex(table_entries))
        self.columns = (DenseIndex(column_entries), KeywordIndex(column_entries))

    @staticmethod
    def _search(pair: tuple[DenseIndex, KeywordIndex], query: str, top_k: int) -> list[Hit]:
        dense, keyword = pair
        return rrf([dense.search(query, top_k * 2), keyword.search(query, top_k * 2)], top_k=top_k)

    def retrieve(self, query: str, *, top_metrics: int = 3, top_tables: int = 4,
                 top_columns: int = 12) -> tuple[list[Hit], list[Hit], list[Hit]]:
        return (
            self._search(self.metrics, query, top_metrics),
            self._search(self.tables, query, top_tables),
            self._search(self.columns, query, top_columns),
        )
