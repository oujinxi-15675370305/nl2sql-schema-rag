"""跑一次真实模型评测，写入只读快照 `eval/llm_result.json`。

页面不现场调模型：它只读这份 artifact（`GET /api/eval/llm/last`），显示"上次模型评测"。
这个脚本才真正调模型，适合手动/夜间跑：

    python -m scripts.record_llm_eval
    python -m scripts.record_llm_eval --hard-runs 2

不写细节（`details` / `multi_turn_details` 全部丢掉）；静默回退、期望 SQL 自身报错时
直接失败、不落盘，避免把"规则跑出来的成绩"当成模型成绩。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # 直接 `python scripts/record_llm_eval.py` 也能跑
    sys.path.insert(0, str(ROOT))

from app.env import load_env  # noqa: E402
from app.evals import LLM_RESULT, run, run_hard  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="跑真实模型评测并写入 eval/llm_result.json（页面只读它，不在点击时调模型）"
    )
    parser.add_argument("--hard-runs", type=int, default=4,
                        help="hard 组重复次数（温度 0 仍有采样波动），默认 4")
    parser.add_argument("--out", type=Path, default=LLM_RESULT, help="artifact 输出路径")
    args = parser.parse_args(argv)

    load_env(ROOT / ".env")
    if not os.getenv("LLM_API_KEY", "").strip():
        print("没有 LLM_API_KEY：这个脚本只记录真实模型结果，跳过（不写假数据）")
        return 2

    print("跑基础 57 条 + 多轮 21 轮（engine=llm）…")
    base = run(verbose=False, engine="llm")

    hard_runs = max(1, args.hard_runs)
    hard_results = []
    for index in range(hard_runs):
        print(f"跑 hard 10 条（第 {index + 1}/{hard_runs} 次）…")
        hard_results.append(run_hard(verbose=False, engine="llm"))

    fallbacks = (base["fallbacks"] + base["multi_turn_fallbacks"]
                 + sum(item["hard_fallbacks"] for item in hard_results))
    bad_expected = sum(item["hard_bad_expected"] for item in hard_results)
    if fallbacks or bad_expected:
        print(f"结果不可信：静默回退 {fallbacks} 次、hard 期望 SQL 自身报错 {bad_expected} 条；不写 artifact")
        return 1

    hard_matches = [item["hard_match"] for item in hard_results]
    summary = {
        "total": base["total"],
        "ask_total": base["ask_total"],
        "exec_match": base["exec_match"],
        "intent_match": base["intent_match"],
        "multi_turn_match": base["multi_turn_match"],
        "multi_turn_total": base["multi_turn_total"],
        "hard_total": hard_results[0]["hard_total"],
        "hard_match_min": min(hard_matches),
        "hard_match_max": max(hard_matches),
        "hard_samples": len(hard_matches),
        "fallbacks": fallbacks,
        "avg_ms": base["avg_ms"],
        "hard_avg_ms": int(sum(item["hard_avg_ms"] for item in hard_results) / len(hard_results)),
    }
    artifact = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "model": os.getenv("LLM_MODEL", "deepseek-chat"),
        "engine": "llm",
        "summary": summary,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"已写入 {args.out}")
    print(f"  基础 {summary['exec_match']}/{summary['ask_total']} · "
          f"多轮 {summary['multi_turn_match']}/{summary['multi_turn_total']} · "
          f"hard {summary['hard_match_min']}–{summary['hard_match_max']}/{summary['hard_total']} · "
          f"回退 {summary['fallbacks']} · 平均 {summary['avg_ms']}ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
