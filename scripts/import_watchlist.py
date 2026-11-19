#!/usr/bin/env python3
"""把旧版 ``script/submit.buff`` 迁移到 ``data/watchlist.json``（幂等）。

用法：
    uv run python scripts/import_watchlist.py

已存在关注列表文件时只做校验和统计，不会重复追加。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings  # noqa: E402
from app.watchlist import WatchlistStore  # noqa: E402


def main() -> int:
    settings = Settings.from_env()
    already_exists = settings.watchlist_path.exists()
    store = WatchlistStore(
        settings.watchlist_path,
        max_items=settings.max_items,
        legacy_path=settings.legacy_path,
    )
    store.ensure_loaded()
    entries = store.entries()

    if already_exists:
        print(f"关注列表已存在，跳过迁移：{settings.watchlist_path}（{len(entries)} 条）")
    elif settings.legacy_path is not None and settings.legacy_path.is_file():
        print(f"已从 {settings.legacy_path} 迁移到 {settings.watchlist_path}（{len(entries)} 条）")
    else:
        print(f"未找到旧数据 {settings.legacy_path}，创建空关注列表（{len(entries)} 条）")

    for entry in entries:
        print(f"  {entry.goods_id}\t{entry.added_at}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
