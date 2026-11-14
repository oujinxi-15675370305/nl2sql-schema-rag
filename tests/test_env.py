"""`.env` 读取的测试。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.env import load_env


class LoadEnvTest(unittest.TestCase):
    def test_loads_values_and_skips_existing(self) -> None:
        # 文件里的键写进环境；已存在的环境变量不被覆盖（CI/临时 export 优先）
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# 注释\n"
                "NL2SQL_TEST_KEY=from_file\n"
                'NL2SQL_TEST_QUOTED="quoted value"\n'
                "NL2SQL_TEST_EXIST=from_file\n",
                encoding="utf-8",
            )
            os.environ.pop("NL2SQL_TEST_KEY", None)
            os.environ.pop("NL2SQL_TEST_QUOTED", None)
            os.environ["NL2SQL_TEST_EXIST"] = "from_env"
            try:
                loaded = load_env(path)
                self.assertEqual(os.environ["NL2SQL_TEST_KEY"], "from_file")
                self.assertEqual(os.environ["NL2SQL_TEST_QUOTED"], "quoted value")
                self.assertEqual(os.environ["NL2SQL_TEST_EXIST"], "from_env")
                self.assertEqual(loaded, 2)
            finally:
                for key in ("NL2SQL_TEST_KEY", "NL2SQL_TEST_QUOTED", "NL2SQL_TEST_EXIST"):
                    os.environ.pop(key, None)

    def test_missing_file_is_noop(self) -> None:
        # 反向用例：没有 .env 不能报错，也不能污染环境
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_env(Path(tmp) / "nope.env"), 0)


if __name__ == "__main__":
    unittest.main()
