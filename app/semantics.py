"""口径判定的单一事实源：哪些问题算"占比题"、哪些算"复购题"。

这些正则以前散在 `agent.py`（份额提示、复购表过滤）和 `analysis.py`（复购聚合层级、
份额 hint 注入）里，改一处忘另一处就会让"提示到的表/口径"和"实际检索/校验到的"不一致。
现在只在这里定义，`agent` / `analysis` 都从这里取。

注意：这里只做**文本判定**，不碰 schema、不碰权限；判定结果要么用来选 prompt 段落，
要么用来选确定性守卫，不用来生成 SQL。
"""
from __future__ import annotations

import re

# 占比/份额：分子分母口径必须同源，且 SQL 里必须真的出现比值计算。
SHARE_RE = re.compile(r"占比|比例|份额|占.{0,8}(?:%|百分之)")

DEFAULT_SHARE_INSTRUCTION = (
    "本次是份额/占比问题：分子和分母必须同源。品牌/品类销售额用可加的行级口径"
    "（SUM(i.item_amount) 或按明细分摊整单金额），地区总额也用同一口径；"
    "不要用整单 pay_amount 当分母去配 item_amount 分子。"
)
DEFAULT_REPURCHASE_INSTRUCTION = (
    "复购客户可以直接查 repurchase_customers（customer_id, region_id, order_count），"
    "用客户所属地区，不要按订单地区拆客户。"
)

# 复购/回头客/阈值客户：这几类问法都要"先按客户聚合再筛"，只是措辞不同。
# 之前 `build_context` 只认"复购|回头客"，`analysis` 的聚合层级守卫却认"至少两单"，
# 同一句"下过至少两单的客户数按地区"在两个模块里被判成不同的问题。
REPURCHASE_RE = re.compile(
    r"复购|回头客|下过至少|至少.{0,4}(?:两|2)\s*单|(?:两|2)\s*单以上|"
    r"买过.{0,2}(?:两|2)\s*次|购买.{0,2}(?:两|2)\s*次"
)


def is_share_question(text: str) -> bool:
    return bool(SHARE_RE.search(text or ""))


def is_repurchase_question(text: str) -> bool:
    return bool(REPURCHASE_RE.search(text or ""))
