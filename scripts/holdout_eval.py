"""Holdout 手动验证：用没写进 prompt 示例的 3 条变体检查泛化（prompt v3.1）。

用法：

    python -m scripts.holdout_eval                 # 真实模型，跑 1 次
    python -m scripts.holdout_eval --repeat 3      # 跑 3 次看稳定性
    python -m scripts.holdout_eval --engine rule   # 离线对照组

只读 `eval/holdout_cases.json`，不写入模型快照、不进 CI、不设门槛。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.agent import ask
from app.catalog import load_catalog
from app.env import load_env
from app.evals import FAILURE_LABELS, _expected_result, _matched, classify_failure

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "eval" / "holdout_cases.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Holdout 复杂问法泛化验证")
    parser.add_argument("--engine", choices=("rule", "llm"), default="llm")
    parser.add_argument("--repeat", type=int, default=1, help="重复次数，默认 1")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    load_env(ROOT / ".env")

    catalog = load_catalog()
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    use_llm = args.engine == "llm"
    runs: list[dict] = []
    for run_index in range(1, max(1, args.repeat) + 1):
        details = []
        hit = 0
        for case in cases:
            plan = ask(case["question"], catalog=catalog, use_llm=use_llm)
            expected_rows, expected_error = _expected_result(case["expected_sql"], catalog)
            matched = _matched(plan, expected_rows, expected_error)
            failure = classify_failure(plan, matched)
            hit += int(matched)
            details.append({
                "id": case["id"], "question": case["question"],
                "sql": plan.sql, "expected_sql": case["expected_sql"],
                "ok": plan.ok, "error": plan.error or expected_error,
                "failure": failure, "matched": matched,
                "rows": len(plan.rows) if plan.ok else 0, "ms": plan.elapsed_ms,
            })
            if not args.json:
                flag = "[PASS]" if matched else "[FAIL]"
                print(f"{flag} H{case['id']} {case['question']} ({plan.elapsed_ms}ms, {len(plan.rows)} 行)")
                if not matched:
                    print(f"    生成：{' '.join((plan.sql or '(空)').split())[:120]}")
                    print(f"    期望：{' '.join(case['expected_sql'].split())[:120]}")
                    print(f"    分类：{failure}（{FAILURE_LABELS.get(failure, failure)}）")
                    if plan.error:
                        print(f"    错误：{plan.error}")
        runs.append({"run": run_index, "match": hit, "total": len(cases), "details": details})
        if not args.json:
            print(f"--- run {run_index}: {hit}/{len(cases)} ---")

    if args.json:
        print(json.dumps({"engine": args.engine, "runs": runs}, ensure_ascii=False, indent=1))
    else:
        scores = [f'{run["match"]}/{run["total"]}' for run in runs]
        print("holdout：" + "、".join(scores) + f"（engine={args.engine}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
