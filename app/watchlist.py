"""关注列表存储：线程安全、原子写、去重、上限与旧数据迁移。"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.schemas import GoodsIdError, normalize_goods_id, utc_now_iso

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class WatchlistError(Exception):
    """关注列表异常基类。"""


class DuplicateGoods(WatchlistError):
    """商品已在关注列表中。"""


class WatchlistFull(WatchlistError):
    """关注列表达到上限。"""

    def __init__(self, limit: int) -> None:
        super().__init__(f"关注列表已满（上限 {limit}）")
        self.limit = limit


class WatchlistWriteError(WatchlistError):
    """关注列表落盘失败。"""


@dataclass(frozen=True, slots=True)
class WatchEntry:
    goods_id: str
    added_at: str


class WatchlistStore:
    """JSON 文件支撑的关注列表。

    所有读改写都在同一把 ``threading.Lock`` 内完成；写入采用同目录临时
    文件 + ``os.replace``，读者永远只会看到完整的旧文件或完整的新文件。
    """

    def __init__(
        self,
        path: Path | str,
        *,
        max_items: int = 200,
        legacy_path: Path | str | None = None,
        clock: object | None = None,
    ) -> None:
        self._path = Path(path)
        self._max_items = max(int(max_items), 1)
        self._legacy_path = Path(legacy_path) if legacy_path is not None else None
        self._lock = threading.Lock()
        self._entries: list[WatchEntry] | None = None
        self._now = clock if callable(clock) else utc_now_iso

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    @property
    def max_items(self) -> int:
        return self._max_items

    def ensure_loaded(self) -> None:
        """加载磁盘数据（含一次性迁移），幂等。"""
        with self._lock:
            self._ensure_loaded_locked()

    def entries(self) -> tuple[WatchEntry, ...]:
        with self._lock:
            self._ensure_loaded_locked()
            assert self._entries is not None
            return tuple(self._entries)

    @property
    def count(self) -> int:
        return len(self.entries())

    def contains(self, goods_id: str | int) -> bool:
        gid = normalize_goods_id(goods_id)
        with self._lock:
            self._ensure_loaded_locked()
            assert self._entries is not None
            return any(entry.goods_id == gid for entry in self._entries)

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def add(self, goods_id: str | int) -> WatchEntry:
        """新增关注。重复抛 DuplicateGoods，超上限抛 WatchlistFull。"""
        gid = normalize_goods_id(goods_id)
        with self._lock:
            self._ensure_loaded_locked()
            assert self._entries is not None
            if any(entry.goods_id == gid for entry in self._entries):
                raise DuplicateGoods(gid)
            if len(self._entries) >= self._max_items:
                raise WatchlistFull(self._max_items)

            entry = WatchEntry(goods_id=gid, added_at=str(self._now()))
            updated = [*self._entries, entry]
            self._persist(updated)
            self._entries = updated
            logger.info("关注列表新增 goods_id=%s，当前 %d 条", gid, len(updated))
            return entry

    def remove(self, goods_id: str | int) -> bool:
        """删除关注；不存在时返回 False。"""
        gid = normalize_goods_id(goods_id)
        with self._lock:
            self._ensure_loaded_locked()
            assert self._entries is not None
            remaining = [entry for entry in self._entries if entry.goods_id != gid]
            if len(remaining) == len(self._entries):
                return False
            self._persist(remaining)
            self._entries = remaining
            logger.info("关注列表删除 goods_id=%s，剩余 %d 条", gid, len(remaining))
            return True

    # ------------------------------------------------------------------
    # 内部：加载 / 迁移 / 落盘
    # ------------------------------------------------------------------
    def _ensure_loaded_locked(self) -> None:
        if self._entries is not None:
            return

        if self._path.exists():
            self._entries = self._load_file()
            logger.info("关注列表已加载：%s（%d 条）", self._path, len(self._entries))
            return

        self._entries = []
        if self._legacy_path is not None and self._legacy_path.is_file():
            self._migrate_legacy(self._legacy_path)

    def _load_file(self) -> list[WatchEntry]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._backup_corrupt(exc)
            return []

        items = raw.get("items") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            self._backup_corrupt(ValueError("缺少 items 数组"))
            return []

        entries: list[WatchEntry] = []
        seen: set[str] = set()
        skipped = 0
        for item in items:
            if not isinstance(item, dict):
                skipped += 1
                continue
            try:
                gid = normalize_goods_id(item.get("goods_id"))
            except GoodsIdError:
                skipped += 1
                continue
            if gid in seen:
                skipped += 1
                continue
            added_at = item.get("added_at")
            if not isinstance(added_at, str) or not added_at.strip():
                added_at = utc_now_iso()
            seen.add(gid)
            entries.append(WatchEntry(goods_id=gid, added_at=added_at))

        if skipped:
            logger.warning("关注列表 %s 中有 %d 条非法/重复记录已忽略", self._path, skipped)
        return entries

    def _backup_corrupt(self, exc: BaseException) -> None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        backup = self._path.with_name(f"{self._path.name}.corrupt-{stamp}")
        counter = 1
        while backup.exists():
            backup = self._path.with_name(f"{self._path.name}.corrupt-{stamp}-{counter}")
            counter += 1
        try:
            os.replace(self._path, backup)
        except OSError as rename_error:
            logger.error("关注列表文件损坏且无法备份：%s（%s）", self._path, rename_error)
            return
        logger.warning(
            "关注列表文件损坏（%s），已备份为 %s，本次从空列表开始",
            exc,
            backup.name,
        )

    def _migrate_legacy(self, legacy: Path) -> None:
        assert self._entries is not None
        try:
            text = legacy.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("读取旧关注列表 %s 失败：%s", legacy, exc)
            return

        entries: list[WatchEntry] = []
        seen: set[str] = set()
        skipped = 0
        for line in text.splitlines():
            value = line.strip()
            if not value:
                continue
            try:
                gid = normalize_goods_id(value)
            except GoodsIdError:
                skipped += 1
                continue
            if gid in seen:
                skipped += 1
                continue
            seen.add(gid)
            entries.append(WatchEntry(goods_id=gid, added_at=utc_now_iso()))

        self._entries = entries
        if entries:
            self._persist(entries)
        logger.info(
            "已从 %s 迁移 %d 条关注记录（跳过 %d 行非法/重复数据）",
            legacy,
            len(entries),
            skipped,
        )

    def _persist(self, entries: list[WatchEntry]) -> None:
        payload = {
            "version": SCHEMA_VERSION,
            "items": [
                {"goods_id": entry.goods_id, "added_at": entry.added_at} for entry in entries
            ],
        }
        try:
            self._atomic_write(payload)
        except OSError as exc:
            raise WatchlistWriteError(f"关注列表写入失败：{exc}") from exc

    def _atomic_write(self, payload: dict[str, object]) -> None:
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)

        fd, tmp_name = tempfile.mkstemp(
            dir=str(directory), prefix=f".{self._path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._path)
            self._fsync_directory(directory)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)
