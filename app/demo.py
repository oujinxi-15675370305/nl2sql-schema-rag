"""离线演示素材：让"自我修复"在没配 LLM key 时也能在页面上看见。

规则生成器不会写错列名，所以离线页面永远只有一次尝试。这里用脚本化生成器
复现"第一次写错列名 → 带着报错改对"（和 `scripts/demo_repair.py` 同一套路），
只给 `/api/demo/repair` 和截图脚本使用：**不参与评测、不写会话、不改生成器**。

SQL 固定、演示库由固定 seed 生成，所以截图/录屏每次的结果都一样。
"""
from __future__ import annotations

QUESTION = "各地区的GMV"

# 第一次：GROUP BY 用了 orders 上不存在的 region_name → 执行报 no such column；
# 第二次：改成 r.region_name，结果和"一次写对"完全一致（AC-1.2）。
ATTEMPTS = (
    "SELECT r.region_name AS 地区, ROUND(SUM(o.pay_amount), 2) AS gmv\n"
    "FROM orders o JOIN regions r ON r.region_id = o.region_id\n"
    "WHERE o.status = 'paid'\n"
    "GROUP BY o.region_name",
    "SELECT r.region_name AS 地区, ROUND(SUM(o.pay_amount), 2) AS gmv\n"
    "FROM orders o JOIN regions r ON r.region_id = o.region_id\n"
    "WHERE o.status = 'paid'\n"
    "GROUP BY r.region_name",
)
