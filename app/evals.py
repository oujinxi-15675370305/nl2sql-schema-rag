"""NL→SQL 评测。执行结果一致率是主指标，SQL 字面匹配只作参考。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from app.agent import ask, execute, validate
from app.catalog import load_catalog

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "eval" / "cases.json"


def normalize(rows: list[tuple], digits: int = 2) -> list[tuple]:
    """浮点按 2 位对齐，避免 1.0 / 1.00 被当成不同结果。"""
    out = []
    for row in rows:
        fixed = []
        for v in row:
            if isinstance(v, float):
                fixed.append(round(v, digits))
            else:
                fixed.append(v)
        out.append(tuple(fixed))
    return sorted(out, key=lambda r: tuple(str(x) for x in r))


def run(verbose: bool = True) -> dict:
    catalog = load_catalog()
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    details = []
    exec_hit = exact_hit = 0
    latencies: list[int] = []

    for case in cases:
        started = time.time()
        plan = ask(case["question"], catalog=catalog, use_llm=False)
        latencies.append(int((time.time() - started) * 1000))

        ok_expected, msg_expected = validate(case["expected_sql"], catalog)
        expected_rows: list[tuple] = []
        if ok_expected:
            try:
                _, expected_rows = execute(case["expected_sql"] + (" LIMIT 200" if "limit" not in case["expected_sql"].lower() else ""))
            except Exception as exc:
                msg_expected = f"期望 SQL 执行失败：{exc}"

        plan_rows = plan.rows if plan.ok else []
        # 被路由判成非 ask 也算失败：这类问题只表现为分数下降，不单独断言会很难定位
        same = (
            plan.intent == "ask"
            and normalize(plan_rows) == normalize(expected_rows)
            and bool(expected_rows or plan.ok)
        )
        exact = " ".join(plan.sql.lower().split()) == " ".join(case["expected_sql"].lower().split())
        exec_hit += int(same)
        exact_hit += int(exact)

        details.append({
            "id": case["id"],
            "question": case["question"],
            "intent": plan.intent,
            "sql": plan.sql,
            "expected_sql": case["expected_sql"],
            "ok": plan.ok,
            "error": plan.error or msg_expected,
            "exec_match": same,
            "exact_match": exact,
            "rows": len(plan_rows),
            "ms": plan.elapsed_ms,
        })
        if verbose:
            flag = "[PASS]" if same else ("[PART]" if exact else "[FAIL]")
            print(f"{flag} #{case['id']:>2} {case['question']}  ({plan.elapsed_ms}ms, {len(plan_rows)} 行)")
            if not same and verbose:
                print(f"    生成：{plan.sql.splitlines()[0][:90]}")
                print(f"    期望：{case['expected_sql'][:90]}")
                if plan.error:
                    print(f"    错误：{plan.error}")

    total = len(details)
    summary = {
        "total": total,
        "exec_match": exec_hit,
        "exec_rate": round(exec_hit / total, 4),
        "exact_match": exact_hit,
        "exact_rate": round(exact_hit / total, 4),
        "avg_ms": int(sum(latencies) / total),
        "details": details,
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--threshold", type=float, default=0.9, help="执行一致率门槛，低于则退出码 1")
    args = parser.parse_args(argv)

    summary = run(verbose=not args.json)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    else:
        print("-" * 60)
        print(f"用例 {summary['total']} 条 | 执行一致率 {summary['exec_rate']:.1%} "
              f"({summary['exec_match']}/{summary['total']}) | SQL 完全匹配 {summary['exact_rate']:.1%} | 平均 {summary['avg_ms']}ms")
    return 0 if summary["exec_rate"] >= args.threshold else 1


if __name__ == "__main__":
    sys.exit(main())
