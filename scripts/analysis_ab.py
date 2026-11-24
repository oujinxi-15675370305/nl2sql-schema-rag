"""分析模式 A/B：对 complex 可判定题逐条跑，只替换 Planner/Synthesizer 模型。

用法（仓库根目录）：

    python -m scripts.analysis_ab --models deepseek-chat,deepseek-flash
    python -m scripts.analysis_ab --models qwen3.8-flash --out analysis_ab.json

- SQL 生成始终用 `.env` 的 `LLM_MODEL`（快路径），本脚本只改 `ANALYSIS_MODEL`；
- 接千问等另一家服务时，先在环境里配 `ANALYSIS_BASE_URL` / `ANALYSIS_API_KEY`；
- 覆盖率 = 最终结论里出现的 expected 数值比例，只作“有没有把结果说出来”的信号，
  不是正确率；完整分类仍需人工看答案（本脚本会把答案留在 JSON 里）。

不写 `eval/llm_result.json`，不进 CI，不碰单 SQL 评测。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

from app.agent import ask
from app.catalog import load_catalog
from app.env import load_env
from app.evals import _expected_result

ROOT = Path(__file__).resolve().parent.parent
COMPLEX_CASES = ROOT / "eval" / "complex_cases.json"


def answer_numbers(text: str) -> list[float]:
    """抽取答案里的数字；把 Unicode 负号（U+2212 等）归一成 ASCII '-'。

    C8 的环比是 `−0.1627`（U+2212），旧正则只抓正数，导致 coverage 少算 3 个负值；
    这不是答案缺数，是度量脚本的 bug。
    """
    text = (text or "").replace(",", "")
    for minus in ("\u2212", "\uFE63", "\uFF0D"):  # − / ﹣ / －
        text = text.replace(minus, "-")
    values: list[float] = []
    for raw in re.findall(r"-?\d+(?:\.\d+)?\s*%?", text):
        pct = "%" in raw
        try:
            value = float(raw.replace("%", "").strip())
        except ValueError:
            continue
        values.append(value / 100 if pct else value)
    return values


def expected_numbers(expected_sql: str, catalog) -> list[float]:
    rows, error = _expected_result(expected_sql, catalog)
    if error:
        return []
    values: list[float] = []
    for row in rows:
        for cell in row:
            if isinstance(cell, (int, float)) and not isinstance(cell, bool):
                values.append(float(cell))
    return values


def coverage(case: dict, answer: str, catalog) -> float:
    expected = expected_numbers(case["expected_sql"], catalog)
    if not expected:
        return 0.0
    got = answer_numbers(answer)
    hits = 0
    for want in expected:
        if any(abs(value - want) <= max(0.01, abs(want) * 0.001)
               or abs(value - want * 100) <= max(0.1, abs(want * 100) * 0.001)
               for value in got):
            hits += 1
    return hits / len(expected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="分析模式 A/B（只换 Planner/Synthesizer）")
    parser.add_argument("--models", default="deepseek-chat,deepseek-flash",
                        help="逗号分隔的 ANALYSIS_MODEL 列表")
    parser.add_argument("--cases", default="", help="逗号分隔的 complex id；默认全部可判定题")
    parser.add_argument("--out", default="", help="可选：把逐题答案/SQL 写到 JSON")
    args = parser.parse_args(argv)

    load_env(ROOT / ".env")
    os.environ.setdefault("ANALYSIS_TIMEOUT_S", "120")
    os.environ.setdefault("ANALYSIS_TOTAL_BUDGET_S", "300")
    catalog = load_catalog()
    cases = [case for case in json.loads(COMPLEX_CASES.read_text(encoding="utf-8"))["cases"]
             if not case.get("expected_unsupported")]
    if args.cases:
        wanted = {int(part) for part in args.cases.split(",") if part.strip()}
        cases = [case for case in cases if case["id"] in wanted]

    results: dict[str, list[dict]] = {}
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        os.environ["ANALYSIS_MODEL"] = model
        print(f"\n=== {model} ===")
        rows: list[dict] = []
        for case in cases:
            started = time.time()
            plan = ask(case["question"], catalog=catalog, mode="analysis")
            elapsed = int((time.time() - started) * 1000)
            row = {
                "id": case["id"],
                "category": case["category"],
                "question": case["question"],
                "steps": len(plan.steps),
                "ok_steps": sum(1 for step in plan.steps if step.ok),
                "skipped": sum(1 for step in plan.steps if step.skipped),
                "warnings": [step.warning for step in plan.steps if step.warning],
                "analysis_error": plan.analysis_error,
                "elapsed_ms": elapsed,
                "usage": plan.usage,
                "coverage": round(coverage(case, plan.answer, catalog), 4),
                "answer": plan.answer,
                "step_sql": [step.sql for step in plan.steps],
            }
            rows.append(row)
            print(f'C{case["id"]:>2} steps={row["ok_steps"]}/{row["steps"]} '
                  f'skip={row["skipped"]} warn={len(row["warnings"])} '
                  f'cov={row["coverage"]:.2f} {elapsed}ms')
        results[model] = rows

    for model, rows in results.items():
        steps = sum(row["steps"] for row in rows)
        ok = sum(row["ok_steps"] for row in rows)
        prompt = sum((row["usage"] or {}).get("prompt_tokens", 0) for row in rows)
        completion = sum((row["usage"] or {}).get("completion_tokens", 0) for row in rows)
        avg_ms = int(sum(row["elapsed_ms"] for row in rows) / len(rows)) if rows else 0
        avg_cov = sum(row["coverage"] for row in rows) / len(rows) if rows else 0.0
        print(f"\n{model}: steps {ok}/{steps}, avg {avg_ms}ms, "
              f"tokens {prompt}/{completion}, coverage {avg_cov:.2f}")

    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"saved {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
