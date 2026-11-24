"""公开集 harness 的离线测试：不下载 BIRD、不调模型。"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from app.conclusion import ChatResult
from benchmarks.bird_sqlite import (
    compare,
    compare_official,
    extract_sql,
    find_dataset,
    is_readonly,
    load_cases,
    run_case,
    schema_ddl,
)


class BirdHelpersTest(unittest.TestCase):
    def test_extract_sql_from_fence_and_prose(self) -> None:
        text = "说明\n```sql\nSELECT a FROM t WHERE b = 1;\n```\n结束"
        self.assertEqual(extract_sql(text), "SELECT a FROM t WHERE b = 1;")
        self.assertEqual(extract_sql("WITH x AS (SELECT 1) SELECT * FROM x;"),
                         "WITH x AS (SELECT 1) SELECT * FROM x;")
        self.assertEqual(extract_sql("没有 SQL"), "")

    def test_readonly_check(self) -> None:
        self.assertTrue(is_readonly("SELECT 1"))
        self.assertTrue(is_readonly("  -- comment\nWITH x AS (SELECT 1) SELECT * FROM x"))
        self.assertFalse(is_readonly("DELETE FROM t"))
        self.assertFalse(is_readonly("/* x */ UPDATE t SET a = 1"))
        self.assertFalse(is_readonly(""))

    def test_compare_strict_and_loose(self) -> None:
        cols = ["a", "b"]
        self.assertTrue(compare(cols, [(1, "x"), (2, "y")], ["a", "b"], [(2, "y"), (1, "x")]))
        self.assertTrue(compare(cols, [("x", 1)], ["a", "b"], [(1, "x")], loose=True))
        self.assertFalse(compare(cols, [("x", 1)], ["a", "b"], [(1, "x")], loose=False))
        self.assertFalse(compare(cols, [(1, "x")], cols, [(1, "x"), (2, "y")]))
        self.assertTrue(compare(cols, [(1.0000001, "x")], cols, [(1.0, "x")]))
        # BIRD 官方 EX 是 set 相等：重复行不计，列数不单独检查
        self.assertTrue(compare_official([(1,)], [(1,), (1,)]))
        self.assertFalse(compare_official([(1,)], [(2,)]))

    def test_schema_ddl_and_dataset_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_dir = root / "data" / "dev_databases" / "demo"
            db_dir.mkdir(parents=True)
            db = db_dir / "demo.sqlite"
            conn = sqlite3.connect(db)
            conn.executescript(
                "CREATE TABLE parent(id INTEGER PRIMARY KEY, name TEXT);"
                "CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER,"
                " FOREIGN KEY(parent_id) REFERENCES parent(id));"
            )
            conn.close()
            questions = root / "data" / "mini_dev_sqlite.json"
            questions.write_text(json.dumps([
                {"question": "q1", "db_id": "demo", "SQL": "SELECT 1", "evidence": ""},
                {"question": "q2", "db_id": "demo", "SQL": "SELECT 2", "evidence": ""},
            ]), encoding="utf-8")
            desc_dir = db_dir / "database_description"
            desc_dir.mkdir()
            (desc_dir / "parent.csv").write_text(
                "original_column_name,column_name,column_description,data_format,value_description\n"
                "id,id,parent id,integer,1/2\n"
                "name,name,parent name,text,alpha\n",
                encoding="utf-8",
            )

            found_questions, found_dbs = find_dataset(root)
            self.assertEqual(found_questions, questions)
            self.assertEqual(found_dbs, root / "data" / "dev_databases")
            ddl = schema_ddl(db)
            self.assertIn("-- table parent", ddl)
            self.assertIn("FOREIGN KEY (parent_id) REFERENCES parent(id)", ddl)
            self.assertIn("parent name；alpha", ddl)
            self.assertEqual(len(load_cases(questions, limit=1, seed=1)), 1)
            self.assertEqual([c["question"] for c in load_cases(
                questions, limit=0, exclude_questions={"q1"})], ["q2"])

    def test_retry_on_execution_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_dir = root / "dev_databases" / "demo"
            db_dir.mkdir(parents=True)
            db = db_dir / "demo.sqlite"
            conn = sqlite3.connect(db)
            conn.executescript("CREATE TABLE parent(id INTEGER PRIMARY KEY, name TEXT);"
                               "INSERT INTO parent VALUES (1, 'a');")
            conn.close()
            item = {"db_id": "demo", "question": "列出名字",
                    "SQL": "SELECT name FROM parent"}
            replies = [
                ChatResult(text="SELECT nope FROM parent"),
                ChatResult(text="SELECT name FROM parent"),
            ]
            with mock.patch("benchmarks.bird_sqlite._chat", side_effect=replies):
                result = run_case(item, root / "dev_databases", model="fake",
                                  timeout_s=5, index=1, retry_errors=1,
                                  descriptions=False)
        self.assertTrue(result.retried)
        self.assertTrue(result.official, result)
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(result.error, "")


    def test_run_case_end_to_end_with_fake_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_dir = root / "dev_databases" / "demo"
            db_dir.mkdir(parents=True)
            db = db_dir / "demo.sqlite"
            conn = sqlite3.connect(db)
            conn.executescript(
                "CREATE TABLE parent(id INTEGER PRIMARY KEY, name TEXT);"
                "INSERT INTO parent VALUES (1, 'a'), (2, 'b');"
            )
            conn.close()
            item = {"db_id": "demo", "question": "列出名字",
                    "SQL": "SELECT name FROM parent ORDER BY id"}
            with mock.patch("benchmarks.bird_sqlite._chat",
                            return_value=ChatResult(text="```sql\nSELECT name FROM parent ORDER BY id;\n```")):
                result = run_case(item, root / "dev_databases", model="fake",
                                  timeout_s=5, index=1)
        self.assertTrue(result.strict, result)
        self.assertTrue(result.loose)
        self.assertEqual(result.predicted_sql, "SELECT name FROM parent ORDER BY id;")
        self.assertEqual(result.error, "")
        self.assertEqual(result.gold_error, "")


if __name__ == "__main__":
    unittest.main()
