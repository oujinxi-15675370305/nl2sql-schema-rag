"""演示 M1：第一次把列名写错，带报错重试后改对。

运行：python -m scripts.demo_repair
"""
from __future__ import annotations

from app.agent import ScriptedGenerator, ask
from app.catalog import load_catalog

BAD = "SELECT COUNT(DISTINCT order_cnt) FROM orders WHERE status = 'paid' LIMIT 10"
GOOD = "SELECT COUNT(DISTINCT order_id) FROM orders WHERE status = 'paid' LIMIT 10"


def main() -> None:
    plan = ask(
        "一共多少单",
        catalog=load_catalog(),
        generator=ScriptedGenerator([BAD, GOOD]),
        max_repair=1,
    )
    print(f"问题：{plan.question}")
    for attempt in plan.attempts:
        status = "OK " if attempt["ok"] else "ERR"
        print(f"  第 {attempt['n']} 次 [{status}] {attempt['error'][:70] or attempt['sql'][:70]}")
    print(f"结果：ok={plan.ok} rows={plan.rows}")


if __name__ == "__main__":
    main()
