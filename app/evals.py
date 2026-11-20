"""NL→SQL 评测。

- 单轮：执行结果一致率是主指标，SQL 字面匹配只作参考（同一个问题可以写出多种等价 SQL）。
- 多轮：`eval/multi_turn.json` 用同一个 `Session` 顺序跑，单独算 `multi_turn_rate`，
  **不并进单轮**——混在一起历史数字就不可比了。
- 失败：每条失败用例带 `failure` 分类，`summary.failure_counts` 汇总（AC-5.3）。
  分类靠 `ask()` 记下的 `failure_stage`（validate / execute / timeout），
  只有执行阶段的两个错误码（字段不存在 / 语法错）需要再看一眼错误信息。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

from app.agent import ask, execute, validate
from app.catalog import load_catalog
from app.domain import Catalog, SQLPlan
from app.env import load_env
from app.session import Session

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "eval" / "cases.json"
MULTI_CASES = ROOT / "eval" / "multi_turn.json"

# 失败分类 → 中文说明（docs/SPEC.md AC-5.3；前端/文档引用同一套名字）
FAILURE_LABELS = {
    "routed": "路由拦截（没走到问数）",
    "permission": "权限拦截（越权列 / 敏感字段）",
    "validate": "校验不通过（表名 / 关键字 / 单语句）",
    "field": "字段不存在",
    "syntax": "生成语法错",
    "timeout": "执行超时",
    "retrieval": "检索未命中指标",
    "result": "结果不一致",
    "intent": "意图不符（该问数 / 该拦截判错）",
    "execute": "执行报错",
    "unknown": "未分类",
}


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


def _load(path: Path, key: str) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))[key]


def _expected_result(sql: str, catalog: Catalog) -> tuple[list[tuple], str]:
    """执行期望 SQL，返回 (行, 期望 SQL 自身的错误)。

    期望 SQL 不合法/跑不通时错误会进 details，用例直接算失败——不能因为"两边都是空"蒙混过关。
    """
    ok, message = validate(sql, catalog)
    if not ok:
        return [], message
    query = sql if "limit" in sql.lower() else f"{sql} LIMIT 200"
    try:
        _, rows = execute(query)
    except Exception as exc:
        return [], f"期望 SQL 执行失败：{exc}"
    return rows, ""


def _same_rows(plan: SQLPlan, expected_rows: list[tuple], expected_error: str = "") -> bool:
    if expected_error:
        return False
    # 两边都为空（比如去年没有数据）也算一致，但 ok=False 的空结果不算
    return normalize(plan.rows) == normalize(expected_rows) and bool(expected_rows or plan.ok)


def _matched(plan: SQLPlan, expected_rows: list[tuple], expected_error: str = "") -> bool:
    # 被路由判成非 ask 也算失败：这类问题只表现为分数下降，不单独断言会很难定位
    return plan.intent == "ask" and _same_rows(plan, expected_rows, expected_error)


def _same_sql(actual: str, expected: str) -> bool:
    return " ".join(actual.lower().split()) == " ".join(expected.lower().split())


def _retrieval_missed(plan: SQLPlan) -> bool:
    """问数走到了生成，但检索没把用户说的指标命中 → 归到检索问题。"""
    ctx = plan.context
    if ctx is None:
        return False
    wanted = set((plan.keywords or {}).get("metric", []))
    hit = {h.key for h in ctx.metrics}
    return bool(wanted) and not (wanted & hit)


def classify_failure(plan: SQLPlan, matched: bool) -> str:
    """AC-5.3：把一次失败归到可行动的一类。

    优先级：权限 > 路由 > 超时 > 校验 > 执行（字段/语法）> 检索 > 结果。
    `stage` 由 `ask()` 记录，所以不依赖错误信息的前缀；只有执行阶段的
    "字段不存在 / 语法错" 需要看 SQLite 的报错原文。
    """
    if matched:
        return ""
    text = f"{plan.error}\n{plan.reply}"
    if "无权访问" in text or "无权查看" in text or "敏感字段" in text:
        return "permission"
    if plan.intent != "ask":
        return "routed"
    if plan.failure_stage == "timeout" or "超时" in plan.error:
        return "timeout"
    if plan.failure_stage == "validate":
        return "validate"
    if plan.failure_stage == "execute":
        error = plan.error.lower()
        if "no such column" in error or "has no column" in error:
            return "field"
        if "syntax error" in error:
            return "syntax"
        return "execute"
    if not plan.ok:
        return "unknown"
    if _retrieval_missed(plan):
        return "retrieval"
    return "result"


def run(verbose: bool = True, engine: str = "rule") -> dict:
    """单轮评测 + 多轮评测；返回的 summary 同时包含两组独立指标。

    `engine="rule"`（默认）走离线规则，CI 用；`engine="llm"` 走真实模型，
    必须配 `LLM_API_KEY`，并额外统计 `engine_counts` / `fallbacks`——静默回退到规则时
    分数不可信，`main()` 会直接判失败。
    """
    catalog = load_catalog()
    cases = _load(CASES, "cases")
    details: list[dict] = []
    ask_total = exec_hit = exact_hit = intent_hit = 0
    latencies: list[int] = []
    failure_counts: Counter[str] = Counter()
    engine_counts: Counter[str] = Counter()
    use_llm = engine == "llm"

    for case in cases:
        expected_intent = case.get("expected_intent", "ask")
        role = case.get("role", "analyst")
        started = time.time()
        plan = ask(case["question"], catalog=catalog, use_llm=use_llm, role=role)
        latencies.append(int((time.time() - started) * 1000))

        expected_rows: list[tuple] = []
        expected_error = ""
        if expected_intent == "ask":
            ask_total += 1
            engine_counts[plan.engine] += 1
            expected_rows, expected_error = _expected_result(case["expected_sql"], catalog)
            matched = _matched(plan, expected_rows, expected_error)
            failure = classify_failure(plan, matched)
            exec_hit += int(matched)
        else:
            # 非问数用例：意图对上、且确实没生成 SQL 才算过（AC-2.2）
            matched = plan.intent == expected_intent and not plan.sql
            failure = "" if matched else "intent"

        exact = expected_intent == "ask" and _same_sql(plan.sql, case.get("expected_sql", ""))
        intent_hit += int(plan.intent == expected_intent)
        exact_hit += int(exact)
        if failure:
            failure_counts[failure] += 1

        details.append({
            "id": case["id"],
            "question": case["question"],
            "role": role,
            "expected_intent": expected_intent,
            "intent": plan.intent,
            "engine": plan.engine,
            "sql": plan.sql,
            "expected_sql": case.get("expected_sql", ""),
            "ok": plan.ok,
            "error": plan.error or expected_error,
            "failure": failure,
            "failure_stage": plan.failure_stage,
            "exec_match": matched,
            "exact_match": exact,
            "rows": len(plan.rows) if plan.ok else 0,
            "ms": plan.elapsed_ms,
        })
        if verbose:
            flag = "[PASS]" if matched else ("[PART]" if exact else "[FAIL]")
            print(f"{flag} #{case['id']:>2} {case['question']}  ({plan.elapsed_ms}ms, {details[-1]['rows']} 行)")
            if not matched:
                if expected_intent != "ask":
                    print(f"    意图：{plan.intent}（期望 {expected_intent}）")
                else:
                    print(f"    生成：{plan.sql.splitlines()[0][:90] if plan.sql else '(空)'}")
                    print(f"    期望：{case['expected_sql'][:90]}")
                    stage = f"，stage={plan.failure_stage}" if plan.failure_stage else ""
                    print(f"    分类：{failure}（{FAILURE_LABELS[failure]}）{stage}")
                if plan.error:
                    print(f"    错误：{plan.error}")

    total = len(details)
    summary = {
        "total": total,
        "ask_total": ask_total,
        "exec_match": exec_hit,
        "exec_rate": round(exec_hit / ask_total, 4) if ask_total else 0.0,
        "exact_match": exact_hit,
        "exact_rate": round(exact_hit / ask_total, 4) if ask_total else 0.0,
        "intent_match": intent_hit,
        "intent_rate": round(intent_hit / total, 4) if total else 0.0,
        "avg_ms": int(sum(latencies) / total) if total else 0,
        "failure_counts": dict(sorted(failure_counts.items())),
        "engine": engine,
        "engine_counts": dict(sorted(engine_counts.items())),
        "fallbacks": sum(v for k, v in engine_counts.items() if k != "llm") if use_llm else 0,
        "details": details,
    }
    summary.update(run_multi_turn(catalog=catalog, verbose=verbose, engine=engine))
    return summary


def run_multi_turn(catalog: Catalog | None = None, verbose: bool = True, engine: str = "rule") -> dict:
    """多轮评测：一个 `Session` 顺序跑完一组问题，逐轮比执行结果。

    多轮指标单独统计（`multi_turn_rate`），不并进单轮 `exec_rate`。
    每轮可以声明 `expected_intent`（默认 ask）：这样"该澄清却答了数"也能被抓住。
    """
    catalog = catalog or load_catalog()
    sessions = _load(MULTI_CASES, "sessions")
    details: list[dict] = []
    hit = 0
    failure_counts: Counter[str] = Counter()
    engine_counts: Counter[str] = Counter()
    use_llm = engine == "llm"

    for spec in sessions:
        session = Session(spec.get("id", ""))
        for index, turn in enumerate(spec["turns"], start=1):
            plan = ask(turn["question"], catalog=catalog, use_llm=use_llm, session=session)
            expected_intent = turn.get("expected_intent", "ask")
            expected_rows: list[tuple] = []
            expected_error = ""
            if expected_intent == "ask":
                engine_counts[plan.engine] += 1
                expected_rows, expected_error = _expected_result(turn.get("expected_sql", ""), catalog)

            if plan.intent != expected_intent:
                matched = False
                failure = "intent"
            elif expected_intent == "ask":
                matched = _same_rows(plan, expected_rows, expected_error)
                failure = classify_failure(plan, matched)
            else:
                matched = True
                failure = ""
            hit += int(matched)
            if failure:
                failure_counts[failure] += 1

            details.append({
                "session": session.id,
                "turn": index,
                "question": turn["question"],
                "intent": plan.intent,
                "expected_intent": expected_intent,
                "engine": plan.engine,
                "inherited_slots": plan.inherited_slots,
                "rewritten_question": plan.rewritten_question,
                "sql": plan.sql,
                "expected_sql": turn.get("expected_sql", ""),
                "ok": plan.ok,
                "error": plan.error or expected_error,
                "failure": failure,
                "matched": matched,
                "rows": len(plan.rows) if plan.ok else 0,
                "ms": plan.elapsed_ms,
            })
            if verbose:
                flag = "[PASS]" if matched else "[FAIL]"
                inherited = f", 继承 {'/'.join(plan.inherited_slots)}" if plan.inherited_slots else ""
                print(f"{flag} {session.id}#{index} {turn['question']}  "
                      f"({plan.elapsed_ms}ms, {details[-1]['rows']} 行{inherited})")
                if not matched:
                    print(f"    意图：{plan.intent}（期望 {expected_intent}），分类：{failure}")
                    if plan.sql:
                        print(f"    生成：{plan.sql.splitlines()[0][:90]}")
                    if turn.get("expected_sql"):
                        print(f"    期望：{turn['expected_sql'][:90]}")
                    if plan.error:
                        print(f"    错误：{plan.error}")

    total = len(details)
    return {
        "multi_turn_total": total,
        "multi_turn_match": hit,
        "multi_turn_rate": round(hit / total, 4) if total else 0.0,
        "multi_turn_failure_counts": dict(sorted(failure_counts.items())),
        "multi_turn_engine_counts": dict(sorted(engine_counts.items())),
        "multi_turn_fallbacks": sum(v for k, v in engine_counts.items() if k != "llm") if use_llm else 0,
        "multi_turn_details": details,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NL→SQL 评测：单轮执行一致率、多轮一致率、失败分类")
    parser.add_argument("--json", action="store_true", help="输出 JSON（CI/前端用）")
    parser.add_argument("--threshold", type=float, default=0.9,
                        help="单轮执行一致率门槛，默认 0.9（SPEC 门槛 0.85，CI 按 0.9 卡）")
    parser.add_argument("--min-intent", type=float, default=0.9, help="单轮意图命中率门槛，默认 0.9")
    parser.add_argument("--min-multi", type=float, default=0.9, help="多轮执行一致率门槛，默认 0.9")
    parser.add_argument("--multi-turn", action="store_true", help="只跑多轮评测（调试用）")
    parser.add_argument("--engine", choices=("rule", "llm"), default="rule",
                        help="rule=离线规则（CI 默认）；llm=真实模型（需 LLM_API_KEY，静默回退会判失败）")
    args = parser.parse_args(argv)
    load_env(ROOT / ".env")  # 本地跑 --engine llm 时读 key；CI 没有 .env 会自动跳过

    if args.multi_turn:
        summary = run_multi_turn(verbose=not args.json, engine=args.engine)
        checks = [("多轮一致率", summary["multi_turn_rate"], args.min_multi)]
    else:
        summary = run(verbose=not args.json, engine=args.engine)
        checks = [
            ("单轮执行一致率", summary["exec_rate"], args.threshold),
            ("单轮意图命中率", summary["intent_rate"], args.min_intent),
            ("多轮执行一致率", summary["multi_turn_rate"], args.min_multi),
        ]

    failed = [(label, value, limit) for label, value, limit in checks if value < limit]
    fallbacks = 0
    if args.engine == "llm":
        fallbacks = summary.get("fallbacks", 0) + summary.get("multi_turn_fallbacks", 0)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    else:
        print("-" * 60)
        if args.multi_turn:
            print(f"多轮 {summary['multi_turn_total']} 轮 | 一致率 {summary['multi_turn_rate']:.1%} "
                  f"({summary['multi_turn_match']}/{summary['multi_turn_total']})")
        else:
            print(f"用例 {summary['total']} 条（问数 {summary['ask_total']}）| 执行一致率 {summary['exec_rate']:.1%} "
                  f"({summary['exec_match']}/{summary['ask_total']}) | 意图命中 {summary['intent_rate']:.1%} | "
                  f"SQL 完全匹配 {summary['exact_rate']:.1%} | 平均 {summary['avg_ms']}ms")
            print(f"多轮 {summary['multi_turn_total']} 轮 | 一致率 {summary['multi_turn_rate']:.1%} "
                  f"({summary['multi_turn_match']}/{summary['multi_turn_total']})")
            counts = summary["failure_counts"]
            print("失败分类：" + ("无" if not counts else " / ".join(f"{k} {v}" for k, v in counts.items())))
        if args.engine == "llm":
            print(f"引擎：llm | 静默回退：{fallbacks}（回退时 engine=rule，结果不可信）")
        multi_counts = summary.get("multi_turn_failure_counts") or {}
        if multi_counts:
            print("多轮失败分类：" + " / ".join(f"{k} {v}" for k, v in multi_counts.items()))
        for label, value, limit in failed:
            print(f"未达标：{label} {value:.1%} < {limit:.1%}")
        if fallbacks:
            print(f"未达标：LLM 静默回退 {fallbacks} 次（这轮结果其实是规则跑的）")

    return 1 if (failed or fallbacks) else 0


if __name__ == "__main__":
    sys.exit(main())
