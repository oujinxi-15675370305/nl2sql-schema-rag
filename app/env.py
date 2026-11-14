"""极简 .env 读取（标准库实现，不引 python-dotenv）。

只认 `KEY=VALUE`，忽略 `#` 注释与空行，去掉成对引号；
**已存在的环境变量优先**，方便 CI 或临时 `export` 覆盖。
"""
from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | Path) -> int:
    """把 .env 里的键写进 os.environ，返回实际写入的条数。文件不存在返回 0。"""
    path = Path(path)
    if not path.exists():
        return 0

    loaded = 0
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded
