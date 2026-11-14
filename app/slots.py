"""业务词槽位抽取：指标 / 维度 / 筛选值 / 时间。

这里是全项目**唯一**的业务词表：路由判意图、生成 SQL、评测都看同一份
`extract_keywords` 结果。之前 router 里还抄了一份指标同义词，两套词表对不上，
才会出现"问的是销售额，判成需要澄清"。加词只改这里（或 catalog.json 的
synonyms），不要再在别处复制一份。
"""
from __future__ import annotations

import re

from app.domain import Catalog

REGIONS = ("华东", "华南", "华北", "西南")
CATEGORIES = ("家电", "数码", "服饰", "食品")

# 规则生成器支持的五个指标 → 口语说法
METRIC_HINTS: dict[str, tuple[str, ...]] = {
    "gmv": ("成交", "销售额", "营收", "gmv", "交易额", "卖了多少"),
    "order_count": ("多少单", "订单数", "单量", "订单量", "多少订单"),
    "aov": ("客单价", "平均订单", "均价"),
    "refund_rate": ("退款率", "退货率"),
    "units": ("销量", "件数", "卖了多少件", "多少件"),
}

DIMENSION_HINTS: dict[str, tuple[str, ...]] = {
    "region": ("地区", "区域", "大区", "各省"),
    "category": ("品类", "类目", "分类"),
    "brand": ("品牌",),
    "month": ("每月", "按月", "月份", "月度"),
    "product": ("商品", "产品", "单品"),
}

GROUP_INTENT = re.compile(r"(各|每|按|分|排名|top|分别|各地|各类|哪些)")


def extract_keywords(question: str, catalog: Catalog) -> dict[str, list[str]]:
    """把一句中文问题抽成四个槽位；抽不到就是空列表，调用方据此决定要不要澄清。"""
    q = question.lower()
    out: dict[str, list[str]] = {"metric": [], "dimension": [], "filter": [], "time": []}
    for metric, hints in METRIC_HINTS.items():
        if any(h.lower() in q for h in hints):
            out["metric"].append(metric)
    for metric in catalog.metrics:
        if any(s.lower() in q for s in metric.synonyms):
            if metric.name not in out["metric"]:
                out["metric"].append(metric.name)
    group_intent = bool(GROUP_INTENT.search(question))
    if group_intent:
        for dim, hints in DIMENSION_HINTS.items():
            if any(h in question for h in hints):
                out["dimension"].append(dim)
    for region in REGIONS:
        if region in question:
            out["filter"].append(f"region:{region}")
    for cat in CATEGORIES:
        if cat in question:
            out["filter"].append(f"category:{cat}")
    if "上个月" in question or "上月" in question:
        out["time"].append("last_month")
    if "本月" in question or "这个月" in question:
        out["time"].append("this_month")
    if "今年" in question:
        out["time"].append("this_year")
    if "去年" in question:
        out["time"].append("last_year")
    if "最近30天" in question or "近30天" in question:
        out["time"].append("last_30d")
    m = re.search(r"(20\d{2})-(\d{1,2})", question)
    if m:
        out["time"].append(f"month:{m.group(1)}-{int(m.group(2)):02d}")
    return out
