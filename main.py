"""启动入口：python main.py"""
from __future__ import annotations

import os
from pathlib import Path

from app.env import load_env
from app.server import serve

ROOT = Path(__file__).resolve().parent

if __name__ == "__main__":
    loaded = load_env(ROOT / ".env")
    if loaded:
        print(f"已读取 .env（{loaded} 项）")
    serve(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")))
