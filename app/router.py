"""M2：意图路由。规则优先（可解释），LLM 只在规则判不出来时兜底。

五类：ask / chat / clarify / out_of_scope / unsafe。对照 docs/SPEC.md 的 AC-2.1 ~ AC-2.5。

判定顺序（越直接越靠前，避免"写操作"被当成问数）：
1. 空输入 / 纯标点 → chat
2. 写操作 / 导出 → unsafe（只拦祈使用法，"被删除的订单"这种定语放行）
3. 整句都是客套话 → chat（剥掉寒暄后不剩内容才算，"好的，各地区的GMV" 仍是 ask）
4. 问表结构 / 字段 → out_of_scope
5. 与数据无关 → out_of_scope
6. 有数据词但**抽不到指标** → clarify（AC-2.3）
7. 其余 → ask

槽位由调用方传进来（M3 在会话继承算完槽位之后才调这里），**classify 不自己抽取**：
`ask()` 的调用顺序是 session → 槽位继承 → classify → 生成；如果这里再抽一遍原始问题，
"那上个月呢" 会抽出空指标，被判成 clarify，多轮就断在第二轮。
文本类判定（闲聊 / 写操作 / 超纲）仍然看原始问题，不看继承来的槽位。

`denied_terms` 是当前角色看不到的列名/同义词（M4）：只用于把"我要看手机号"这类
显式越权请求判成 unsafe（AC-4.2），不参与槽位抽取。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from app.domain import Catalog

# 无条件拦：这些词没有"读查询"的用法
STRONG_WRITE = re.compile(
    r"(导出|下载|备份|清空|建表|改表|truncate|drop|attach|vacuum)"
    r"|(?<![A-Za-z])(?:delete|update|insert|alter|create)(?![A-Za-z])",
    re.I,
)
# 有歧义：只有祈使用法才算写操作。"被删除的订单""更新时间分布"是在读数据，放行。
# "改成华南"是改写问句（M3 多轮），只有"把/将 … 改成/改为"才是改数据。
# 注意 lookahead 里要写"除"：否则正则回溯到单字"删"时，会绕过"删除的"这层豁免。
SOFT_WRITE = re.compile(
    r"(?<!被)(?:删除|删掉|删了|删|更新|修改|改数|插入|导入)"
    r"(?!的|后|前|过|时间|日期|状态|记录|标记|除)"
    r"|(?:把|将)[^。？?!！]{0,12}(?:改成|改为)"
)

CHAT_WORDS = re.compile(
    r"你好|您好|在吗|谢谢|感谢|再见|你是谁|你能做什么|哈哈|嗯|好的"
    r"|(?<![A-Za-z])(?:hi|hello|ok|okay)(?![A-Za-z])",
    re.I,
)
CHAT_NOISE = re.compile(r"[\s,，。！？!?~、…\.：:；;\"'“”‘’]+")

META_PATTERN = re.compile(r"(表结构|字段|数据字典|schema|有哪些表|哪些维度|哪些指标|怎么用)", re.I)
OFFTOPIC_WORDS = re.compile(r"(天气|股票|彩票|新闻|写代码|写个|翻译|笑话|故事|诗|菜谱|星座|电影)")

# 只要出现这些词，就认为"问题像是问数据的"（不含"各/每"这种到处都是的字）
DATA_HINTS = re.compile(
    r"(GMV|gmv|成交|销售|营收|订单|客户|商品|品类|品牌|地区|区域|退款|客单价|销量|件数|金额|业绩|经营|流水|收入|毛利|"
    r"上个月|上月|本月|这个月|今年|去年|最近|每月|按月)"
)

# 澄清时能给的维度，从 catalog 里带 is_dimension 的列翻译过来
DIMENSION_LABELS = {
    "region_name": "地区",
    "category": "品类",
    "brand": "品牌",
    "order_date": "月份",
    "product_name": "商品",
    "customer_name": "客户",
}
TIME_OPTIONS = "时间范围？本月 / 上个月 / 今年 / 去年 / 具体月份"


@dataclass
class Route:
    intent: str
    reason: str
    confidence: float = 1.0
    clarify_options: list[str] = field(default_factory=list)
    reply: str = ""  # 特殊场景（如问表结构）自定义话术，为空则用 reply_for 的默认话术


def _has_data_hint(question: str, catalog: Catalog, keywords: dict[str, list[str]] | None = None) -> bool:
    """字面数据词 + 槽位。传 keywords 时把继承来的槽位也算进来（"换成华南"字面上没有数据词）。"""
    if keywords and any(keywords.get(k) for k in ("metric", "dimension", "filter", "time")):
        return True
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


def _metric_options(catalog: Catalog) -> str:
    labels = [m.synonyms[0] if m.synonyms else m.name for m in catalog.metrics]
    return "看哪个指标？" + " / ".join(labels)


def _dimension_options(catalog: Catalog) -> str:
    labels = [DIMENSION_LABELS[c.name] for t in catalog.tables for c in t.columns
              if c.is_dimension and c.name in DIMENSION_LABELS]
    return "按什么维度看？" + " / ".join(dict.fromkeys(labels))


def _clarify_options(question: str, catalog: Catalog, slots: dict[str, list[str]]) -> list[str]:
    options: list[str] = []
    if not slots["dimension"] and not slots["filter"]:
        options.append(_dimension_options(catalog))
    options.append(_metric_options(catalog))
    if not slots["time"]:
        options.append(TIME_OPTIONS)
    return options


def classify(
    question: str,
    catalog: Catalog,
    slots: dict[str, list[str]],
    denied_terms: Iterable[str] = (),
) -> Route:
    """`slots` 必须是调用方算好的槽位（M3 里是继承之后的），这里不再重新抽取。

    `denied_terms` 是当前角色不可见列的列名/同义词（M4），命中即判 unsafe（AC-4.2）。
    """
    q = question.strip()

    if not q:
        return Route("chat", "空输入")
    if not CHAT_NOISE.sub("", q):
        return Route("chat", "只有标点或空白")

    # 1) 写操作：最优先拦，避免生成任何 SQL
    if STRONG_WRITE.search(q) or SOFT_WRITE.search(q):
        return Route("unsafe", "包含写操作或导出请求")

    # 1.5) 显式请求无权列：unsafe + 说明，不静默改列（AC-4.2）
    #      话术里不带列名，避免把隐藏字段名回显出去（AC-4.1）。
    low = q.lower()
    if any(term and term.lower() in low for term in denied_terms):
        return Route(
            "unsafe",
            "请求了无权访问的列",
            reply="这个字段属于敏感信息，当前角色无权查看；可以换一个不含它的问法。",
        )

    # 2) 整句都是客套话（剥掉寒暄后没剩下内容）
    if not CHAT_NOISE.sub("", CHAT_WORDS.sub("", q)).strip():
        return Route("chat", "闲聊或客套话")

    # 3) 问 schema 本身不是问数
    if META_PATTERN.search(q) and not slots["metric"]:
        return Route(
            "out_of_scope",
            "问的是表结构/字段，不是数据",
            reply="这是问数入口；表结构可以看右上角「看 schema」或 /api/schema。",
        )

    # 4) 与数据无关：超纲。这里只看字面——超纲词不能被继承来的槽位救回来
    if OFFTOPIC_WORDS.search(q) and not _has_data_hint(q, catalog):
        return Route("out_of_scope", "与数据仓库无关的问题")
    if not _has_data_hint(q, catalog, slots):
        return Route("out_of_scope", "没有识别到指标、表或维度", confidence=0.6)

    # 5) 有数据意图但抽不到指标：要澄清（AC-2.3）
    if not slots["metric"]:
        return Route("clarify", "没有识别出指标，需要澄清",
                     clarify_options=_clarify_options(q, catalog, slots))

    # 6) 其余按问数处理
    return Route("ask", "识别到指标")


def reply_for(route: Route) -> str:
    if route.reply:
        return route.reply
    if route.intent == "chat":
        return "我在。你可以直接问数据，比如「上个月的 GMV」「各地区的订单数」。"
    if route.intent == "clarify":
        return "这个问题还差点信息：" + "；".join(route.clarify_options)
    if route.intent == "out_of_scope":
        return "我只负责查数据仓库里的数据，这个问题超出范围。"
    if route.intent == "unsafe":
        return "这个请求涉及写操作或导出，出于安全考虑不支持；我可以帮你查数据。"
    return ""
