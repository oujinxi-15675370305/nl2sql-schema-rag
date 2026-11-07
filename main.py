"""启动入口：python main.py"""
from __future__ import annotations

import os

from app.server import serve

if __name__ == "__main__":
    serve(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")))
