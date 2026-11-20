#!/bin/sh
# 容器入口：演示库不存在时先 seed（挂载空 volume 也能起来），然后 exec 主命令。
set -eu

if [ ! -f data/demo.sqlite ]; then
    echo "[entrypoint] 演示库不存在，先生成 data/demo.sqlite"
    python scripts/seed_demo_db.py
fi

exec "$@"
