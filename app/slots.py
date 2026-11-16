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

# 规则生成器支持的六个指标 → 口语说法
METRIC_HINTS: dict[str, tuple[str, ...]] = {
    "gmv": ("成交", "销售额", "营收", "gmv", "交易额", "卖了多少"),
    "order_count": ("多少单", "订单数", "单量", "订单量", "多少订单"),
    "aov": ("客单价", "平均订单", "均价"),
    "refund_rate": ("退款率", "退货率"),
    "units": ("销量", "件数", "卖了多少件", "多少件"),
    "customer_count": ("客户数", "客户数量", "多少客户", "多少个客户", "客户人数"),
}

DIMENSION_HINTS: dict[str, tuple[str, ...]] = {
    "region": ("地区", "区域", "大区", "各省"),
    "category": ("品类", "类目", "分类"),
    "brand": ("品牌",),
    "month": ("每月", "按月", "月份", "月度"),
    "product": ("商品", "产品", "单品"),
    # 不能写光秃秃的"客户"："各地区的客户数"会被判成按客户分组
    "customer": ("各客户", "每个客户", "每位客户", "按客户", "客户名", "客户名单"),
}

GROUP_INTENT = re.compile(r"(各|每|按|分|排名|top|分别|各地|各类|哪些)")

TIME_HINTS = ("上个月", "上月", "本月", "这个月", "今年", "去年", "最近30天", "近30天")

# 补充问法里允许出现的"不带新内容"的词：语气词、看/查这种动词、改写动词。
# 注意只用来判断"这句是不是纯槽位"，不参与槽位抽取。
FILLERS = re.compile(
    r"(帮我看下|帮我看看|帮我|给我|请|麻烦|看一下|看下|看看|查一下|查下|看|查|改成|改为|一下"
    r"|各|每|按|分|的|了|呢|吧|啊|吗|呀|那|这|个|和|与|再|来|下)+"
)
MONTH_RE = re.compile(r"20\d{2}-\d{1,2}")

# 所有槽位词，长的在前，避免"销售额"被"销售"先截断
SLOT_TERMS: tuple[str, ...] = tuple(sorted(
    {h for hints in METRIC_HINTS.values() for h in hints}
    | {h for hints in DIMENSION_HINTS.values() for h in hints}
    | set(REGIONS) | set(CATEGORIES) | set(TIME_HINTS),
    key=len,
    reverse=True,
))


def leftover(question: str) -> str:
    """去掉槽位词、时间词和语气/改写词之后剩下的内容。

    空字符串表示这句只是在补槽位（"上个月" / "按品类看下" / "改成华南"），可以继承上文；
    还剩"工资""新闻"这类新名词时不能瞎继承，交给路由去澄清。
    """
    text = MONTH_RE.sub("", question.lower())
    for term in SLOT_TERMS:
        text = text.replace(term, "")
    return FILLERS.sub("", text).strip()


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
