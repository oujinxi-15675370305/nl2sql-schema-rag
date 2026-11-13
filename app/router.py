"""M2：意图路由。规则优先（可解释），LLM 只在规则判不出来时兜底。

五类：ask / chat / clarify / out_of_scope / unsafe。对照 docs/SPEC.md 的 AC-2.1 ~ AC-2.5。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.catalog import load_catalog
from app.domain import Catalog

WRITE_WORDS = r"(删除|删掉|删了|删|清空|更新|修改|改数|插入|导入|建表|改表|导出|下载|备份|drop|delete|update|insert|truncate|alter)"
CHAT_WORDS = r"(你好|您好|hi|hello|在吗|谢谢|感谢|再见|你是谁|你能做什么|哈哈|嗯|好的)"
VAGUE_WORDS = r"(业绩|数据|报表|情况|表现|怎么样|如何|看一下|查一下|分析一下)"
OFFTOPIC_WORDS = r"(天气|股票|彩票|新闻|写代码|写个|翻译|笑话|故事|诗|菜谱|星座|电影)"

# 能说明"这是在问数据"的词（表名、指标同义词、列同义词、时间与维度词）
DATA_HINTS = re.compile(
    r"(GMV|gmv|成交|销售|营收|订单|客户|商品|品类|品牌|地区|区域|退款|客单价|销量|件数|金额|业绩|经营|流水|收入|毛利|"
    r"上个月|上月|本月|今年|去年|最近|每月|按月|各|每)"
)


@dataclass
class Route:
    intent: str
    reason: str
    confidence: float = 1.0
    clarify_options: list[str] = field(default_factory=list)


def _has_data_hint(question: str, catalog: Catalog) -> bool:
    if DATA_HINTS.search(question):
        return True
    low = question.lower()
    for metric in catalog.metrics:
        if any(s.lower() in low for s in metric.synonyms):
            return True
    for table in catalog.tables:
        if any(s.lower() in low for s in table.synonyms):
            return True
    return False


def classify(question: str, catalog: Catalog | None = None) -> Route:
    catalog = catalog or load_catalog()
    q = question.strip()

    # 1) 写操作：最优先拦，避免生成任何 SQL
    if re.search(WRITE_WORDS, q, re.I):
        return Route("unsafe", "包含写操作或导出请求", clarify_options=[])

    # 2) 纯闲聊
    if len(q) <= 12 and re.search(CHAT_WORDS, q, re.I):
        return Route("chat", "闲聊或客套话")
    if re.fullmatch(r"[\s,，。！？!?~]+", q):
        return Route("chat", "空输入或只有标点")

    vague = bool(re.search(VAGUE_WORDS, q))
    has_hint = _has_data_hint(q, catalog)

    # 3) 有数据意图但说不清：要澄清
    if has_hint and vague and not re.search(r"(GMV|gmv|成交|销售|营收|订单数|客单价|退款率|销量)", q):
        options = []
        if not re.search(r"(地区|品类|品牌|商品|每月|按月)", q):
            options.append("按什么维度看？地区 / 品类 / 品牌 / 月份")
        options.append("看哪个指标？GMV / 订单数 / 客单价 / 退款率 / 销量")
        options.append("时间范围？本月 / 上个月 / 今年 / 具体月份")
        return Route("clarify", "缺指标或维度，需要澄清", clarify_options=options)

    # 4) 与数据无关：超纲
    if re.search(OFFTOPIC_WORDS, q) and not has_hint:
        return Route("out_of_scope", "与数据仓库无关的问题")
    if not has_hint:
        return Route("out_of_scope", "没有识别到指标、表或维度", confidence=0.6)

    # 5) 其余按问数处理
    return Route("ask", "识别到指标/维度/时间")


def reply_for(route: Route) -> str:
    if route.intent == "chat":
        return "我在。你可以直接问数据，比如「上个月的 GMV」「各地区的订单数」。"
    if route.intent == "clarify":
        return "这个问题还差点信息：" + "；".join(route.clarify_options)
    if route.intent == "out_of_scope":
        return "我只负责查数据仓库里的数据，这个问题超出范围。"
    if route.intent == "unsafe":
        return "这个请求涉及写操作或导出，出于安全考虑不支持；我可以帮你查数据。"
    return ""
