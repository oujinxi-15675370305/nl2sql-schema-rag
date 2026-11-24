"""BIRD Mini-Dev（SQLite）独立评测：外部有效性，不并入 app.evals / CI。

用法（先按 benchmarks/README.md 下载数据）：

    python -m benchmarks.bird_sqlite --limit 50 --seed 20261125

它测的是“换一个没见过的数据库，一次生成 SQL 的执行结果是否对”，
不是官方 leaderboard 复现：样本量、prompt、模型和日期都会写进结果文件。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.conclusion import _chat
from app.env import load_env

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "benchmarks" / ".data" / "minidev"
DEFAULT_OUT = ROOT / "benchmarks" / "results" / "bird_mini_dev.json"
DEFAULT_TIMEOUT_S = 30.0

SYSTEM = (
    "你是 SQLite 专家。根据给定的 schema、问题和 evidence 写一条可执行的 SQLite 查询。\n"
    "硬性要求：\n"
    "- 只输出 SQL，不要解释，不要 markdown。\n"
    "- 只能写一条只读 SELECT（可以用 WITH ... SELECT）；禁止 INSERT/UPDATE/DELETE/DDL。\n"
    "- 严格使用 schema 里的表名和列名；不要编造字段。\n"
    "- 如果问题需要排序/限制，按问题写 ORDER BY / LIMIT。"
)


@dataclass
class CaseResult:
    id: int
    source_index: int
    db_id: str
    question: str
    predicted_sql: str = ""
    gold_sql: str = ""
    strict: bool = False
    loose: bool = False
    official: bool = False
    error: str = ""
    gold_error: str = ""
    retried: bool = False
    attempts: list[dict] = field(default_factory=list)
    latency_ms: int = 0


def find_dataset(root: str | Path) -> tuple[Path, Path]:
    """在下载目录里找 mini_dev_sqlite.json 和 dev_databases。"""
    base = Path(root)
    candidates = sorted(base.rglob("mini_dev_sqlite.json"))
    if not candidates:
        raise FileNotFoundError(f"没有找到 mini_dev_sqlite.json：{base}")
    questions = candidates[0]
    db_dirs = [p for p in base.rglob("dev_databases") if p.is_dir()]
    if not db_dirs:
        raise FileNotFoundError(f"没有找到 dev_databases：{base}")
    return questions, db_dirs[0]


def load_cases(path: str | Path, limit: int = 50, seed: int = 20261125,
               exclude_questions: set[str] | None = None) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        # HuggingFace 导出偶尔会把数组包一层
        for key in ("data", "mini_dev_sqlite", "questions"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        raise ValueError("mini_dev_sqlite.json 不是数组")
    excluded = exclude_questions or set()
    cases = []
    for index, item in enumerate(data):
        if not isinstance(item, dict) or not item.get("question"):
            continue
        if item.get("question") in excluded:
            continue
        item = dict(item)
        item["_source_index"] = index
        cases.append(item)
    if limit and limit < len(cases):
        rng = random.Random(seed)
        cases = rng.sample(cases, limit)
    return cases


def quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _clean_text(text: str, limit: int = 220) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def load_descriptions(db_path: str | Path) -> dict[str, dict[str, str]]:
    """读 BIRD database_description/*.csv：表 -> 列 -> 描述/值说明。"""
    desc_dir = Path(db_path).parent / "database_description"
    if not desc_dir.is_dir():
        return {}
    result: dict[str, dict[str, str]] = {}
    for path in sorted(desc_dir.glob("*.csv")):
        table = path.stem
        columns: dict[str, str] = {}
        try:
            with path.open(encoding="utf-8", errors="replace", newline="") as handle:
                for row in csv.DictReader(handle):
                    name = (row.get("original_column_name") or row.get("column_name") or "").strip()
                    if not name:
                        continue
                    description = _clean_text(row.get("column_description", ""), 160)
                    values = _clean_text(row.get("value_description", ""), 260)
                    text = "；".join(part for part in (description, values) if part)
                    if text:
                        columns[name] = text
        except Exception:
            continue
        if columns:
            result[table] = columns
    return result


def schema_ddl(db_path: str | Path, *, descriptions: bool = True) -> str:
    """从 SQLite 读表、列、外键；有 BIRD description CSV 时带上列/值说明。"""
    desc = load_descriptions(db_path) if descriptions else {}
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        objects = conn.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        lines: list[str] = []
        for name, kind in objects:
            lines.append(f"-- {kind} {name}")
            table_desc = desc.get(name, {})
            for col in conn.execute(f"PRAGMA table_info({quote(name)})").fetchall():
                # cid, name, type, notnull, dflt_value, pk
                line = (f"  {col[1]} {col[2]}"
                        + (" PRIMARY KEY" if col[5] else "")
                        + (" NOT NULL" if col[3] else ""))
                if col[1] in table_desc:
                    line += f"  -- {table_desc[col[1]]}"
                lines.append(line)
            for fk in conn.execute(f"PRAGMA foreign_key_list({quote(name)})").fetchall():
                # id, seq, table, from, to, ...
                lines.append(f"  FOREIGN KEY ({fk[3]}) REFERENCES {fk[2]}({fk[4]})")
            lines.append("")
        return "\n".join(lines).strip()
    finally:
        conn.close()


def extract_sql(text: str) -> str:
    """从模型输出里取第一条 SELECT/WITH；保留到第一个分号。"""
    text = re.sub(r"```(?:sql)?", "", text or "", flags=re.I).replace("```", "").strip()
    match = re.search(r"(?is)\b(select|with)\b", text)
    if not match:
        return ""
    sql = text[match.start():].strip()
    semicolon = sql.find(";")
    if semicolon >= 0:
        sql = sql[:semicolon + 1]
    return sql.strip()


def is_readonly(sql: str) -> bool:
    body = re.sub(r"^\s*(?:--[^\n]*\n\s*|/\*.*?\*/\s*)*", "", sql or "", flags=re.S)
    first = body.split(None, 1)[0].lower() if body.split(None, 1) else ""
    return first in {"select", "with"}


def _norm(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, bytes):
        return value.hex()
    return value


def _row_key(row) -> tuple:
    return tuple(_norm(v) for v in row)


def compare(pred_cols: list[str], pred_rows: list[tuple],
            gold_cols: list[str], gold_rows: list[tuple], *, loose: bool = False) -> bool:
    """严格=列数+行多重集完全一致；宽松=列数一致+忽略列顺序的值多重集。"""
    if len(pred_cols) != len(gold_cols) or len(pred_rows) != len(gold_rows):
        return False
    if loose:
        p = sorted((tuple(sorted(_row_key(row), key=repr)) for row in pred_rows), key=repr)
        g = sorted((tuple(sorted(_row_key(row), key=repr)) for row in gold_rows), key=repr)
    else:
        p = sorted((_row_key(row) for row in pred_rows), key=repr)
        g = sorted((_row_key(row) for row in gold_rows), key=repr)
    return p == g


def compare_official(pred_rows: list[tuple], gold_rows: list[tuple]) -> bool:
    """BIRD 官方 EX：set(predicted_res) == set(ground_truth_res)。"""
    return set(_row_key(row) for row in pred_rows) == set(_row_key(row) for row in gold_rows)


def execute(conn: sqlite3.Connection, sql: str, timeout_s: float = DEFAULT_TIMEOUT_S):
    started = time.monotonic()
    conn.set_progress_handler(lambda: 1 if time.monotonic() - started > timeout_s else 0, 10000)
    cur = conn.execute(sql)
    columns = [d[0] for d in cur.description or []]
    return columns, cur.fetchall()


def execute_one(db_path: Path, sql: str, timeout_s: float):
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    # 只读连接默认可能无法为 GROUP BY/ORDER BY 建临时文件（Windows+URI 下实测
    # “unable to open database file”）；排序/分组评测必须放内存。
    conn.execute("PRAGMA temp_store=MEMORY")
    try:
        return execute(conn, sql, timeout_s)
    finally:
        conn.close()


def find_db(db_root: Path, db_id: str) -> Path:
    candidates = [
        db_root / db_id / f"{db_id}.sqlite",
        db_root / db_id / f"{db_id}.db",
        db_root / f"{db_id}.sqlite",
    ]
    for path in candidates:
        if path.exists():
            return path
    found = sorted(db_root.rglob(f"{db_id}.sqlite"))
    if found:
        return found[0]
    raise FileNotFoundError(f"找不到数据库：{db_id}")


def _generate_sql(item: dict, ddl: str, *, model: str, timeout_s: float,
                  feedback: str = "") -> str:
    user = (
        f"Schema:\n{ddl}\n\n"
        f"Question: {item.get('question')}\n"
        f"Evidence: {item.get('evidence') or 'none'}\n\n"
        "只输出一条 SQLite SELECT。"
    )
    if feedback:
        user += f"\n\n上一版 SQL 执行失败：{feedback}\n只修列名/语法/JOIN，保持查询语义。"
    reply = _chat(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        model=model, timeout=timeout_s,
    )
    return extract_sql(reply.text) if reply is not None else ""


def run_case(item: dict, db_root: Path, *, model: str, timeout_s: float, index: int,
             retry_errors: int = 0, descriptions: bool = False) -> CaseResult:
    result = CaseResult(id=index, source_index=int(item.get("_source_index", index)),
                        db_id=str(item.get("db_id") or ""),
                        question=str(item.get("question") or ""),
                        gold_sql=str(item.get("SQL") or ""))
    started = time.monotonic()
    try:
        db_path = find_db(db_root, result.db_id)
        ddl = schema_ddl(db_path, descriptions=descriptions)
    except Exception as exc:
        result.error = f"db/schema: {exc}"
        result.latency_ms = int((time.monotonic() - started) * 1000)
        return result

    pred_cols: list[str] = []
    pred_rows: list[tuple] = []
    feedback = ""
    for attempt in range(max(0, retry_errors) + 1):
        sql = _generate_sql(item, ddl, model=model, timeout_s=timeout_s, feedback=feedback)
        result.predicted_sql = sql
        entry = {"n": attempt + 1, "sql": sql, "error": "", "stage": "generate"}
        result.attempts.append(entry)
        if not sql:
            entry["error"] = "模型输出里没有 SELECT/WITH"
            result.error = entry["error"]
            break
        if not is_readonly(sql):
            entry["error"] = "模型生成了非只读 SQL"
            result.error = entry["error"]
            break
        try:
            pred_cols, pred_rows = execute_one(db_path, sql, timeout_s)
            entry["stage"] = "ok"
            result.error = ""
            break
        except Exception as exc:
            entry["error"] = f"执行失败：{exc}"
            result.error = entry["error"]
            if attempt < retry_errors:
                feedback = str(exc)
                result.retried = True
                continue
            break

    if result.error:
        result.latency_ms = int((time.monotonic() - started) * 1000)
        return result

    if not result.gold_sql:
        result.gold_error = "没有 gold SQL"
        result.latency_ms = int((time.monotonic() - started) * 1000)
        return result
    try:
        gold_cols, gold_rows = execute_one(db_path, result.gold_sql, timeout_s)
    except Exception as exc:
        result.gold_error = f"gold 执行失败：{exc}"
        result.latency_ms = int((time.monotonic() - started) * 1000)
        return result

    result.strict = compare(pred_cols, pred_rows, gold_cols, gold_rows)
    result.loose = compare(pred_cols, pred_rows, gold_cols, gold_rows, loose=True)
    result.official = compare_official(pred_rows, gold_rows)
    result.latency_ms = int((time.monotonic() - started) * 1000)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="BIRD Mini-Dev SQLite benchmark")
    parser.add_argument("--data-root", default=str(DEFAULT_DATA))
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--seed", type=int, default=20261125)
    parser.add_argument("--model", default=None)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--retry-errors", type=int, default=0,
                        help="SQL 执行失败时最多重试几次（默认 0；1 为实验项）")
    parser.add_argument("--descriptions", action="store_true", dest="descriptions",
                        help="把 BIRD database_description 拼进 prompt（实验项，默认关）")
    parser.add_argument("--exclude", action="append", default=[],
                        help="排除已有结果文件里的问题（可重复，保证 holdout 没跑过）")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)

    load_env(ROOT / ".env")
    model = args.model or os.getenv("LLM_MODEL", "").strip() or "deepseek-chat"
    questions_path, db_root = find_dataset(args.data_root)
    excluded: set[str] = set()
    for path in args.exclude:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for case in data.get("cases", []):
            if isinstance(case, dict) and case.get("question"):
                excluded.add(str(case["question"]))
    cases = load_cases(questions_path, limit=args.limit, seed=args.seed,
                       exclude_questions=excluded)
    results: list[CaseResult] = []
    for index, item in enumerate(cases, start=1):
        result = run_case(item, db_root, model=model,
                          timeout_s=args.timeout, index=index,
                          retry_errors=args.retry_errors,
                          descriptions=args.descriptions)
        results.append(result)
        mark = "PASS" if result.official else ("STRICT" if result.strict else "FAIL")
        print(f"[{mark}] {index}/{len(cases)} {result.db_id}: "
              f"{result.question[:60]} ({result.latency_ms}ms)")
        if result.error:
            print(f"  error: {result.error[:160]}")
        if result.gold_error:
            print(f"  gold_error: {result.gold_error[:160]}")

    total = len(results)
    strict = sum(1 for r in results if r.strict)
    loose = sum(1 for r in results if r.loose)
    official = sum(1 for r in results if r.official)
    errors = sum(1 for r in results if r.error)
    gold_errors = sum(1 for r in results if r.gold_error)
    summary = {
        "dataset": "BIRD Mini-Dev SQLite",
        "questions_file": str(questions_path),
        "db_root": str(db_root),
        "model": model,
        "limit": args.limit,
        "seed": args.seed,
        "excluded": len(excluded),
        "total": total,
        "official": official,
        "official_rate": round(official / total, 4) if total else 0.0,
        "strict": strict,
        "loose": loose,
        "strict_rate": round(strict / total, 4) if total else 0.0,
        "loose_rate": round(loose / total, 4) if total else 0.0,
        "errors": errors,
        "gold_errors": gold_errors,
        "retried": sum(1 for r in results if r.retried),
        "descriptions": args.descriptions,
        "retry_errors": args.retry_errors,
        "avg_latency_ms": round(sum(r.latency_ms for r in results) / total, 1) if total else 0.0,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "cases": [asdict(r) for r in results]},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"saved {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
