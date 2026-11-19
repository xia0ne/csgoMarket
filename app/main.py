"""uvicorn 入口：``uvicorn app.main:app``。"""

from __future__ import annotations

from app import create_app

app = create_app()
