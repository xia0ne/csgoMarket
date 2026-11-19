"""关注列表存储：去重、上限、并发、原子写、损坏恢复与旧数据迁移。"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from app.config import PROJECT_ROOT
from app.schemas import GoodsIdError
from app.watchlist import (
    DuplicateGoods,
    WatchlistFull,
    WatchlistStore,
)

LEGACY_IDS = ["895581", "35283", "857550", "871542", "42235", "769498"]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TestBasics:
    def test_add_creates_versioned_file(self, store: WatchlistStore) -> None:
        entry = store.add("42235")

        assert entry.goods_id == "42235"
        assert entry.added_at.endswith("Z")
        payload = read_json(store.path)
        assert payload["version"] == 1
        assert payload["items"] == [{"goods_id": "42235", "added_at": entry.added_at}]
        assert store.count == 1

    def test_ids_are_not_substring_matched(self, store: WatchlistStore) -> None:
        store.add("4223")
        store.add("42235")
        assert [entry.goods_id for entry in store.entries()] == ["4223", "42235"]

    def test_duplicate_raises(self, store: WatchlistStore) -> None:
        store.add("42235")
        with pytest.raises(DuplicateGoods):
            store.add("42235")
        assert store.count == 1

    def test_remove_existing(self, store: WatchlistStore) -> None:
        store.add("42235")
        store.add("769498")

        assert store.remove("42235") is True
        assert [entry.goods_id for entry in store.entries()] == ["769498"]
        assert read_json(store.path)["items"][0]["goods_id"] == "769498"

    def test_remove_missing_returns_false(self, store: WatchlistStore) -> None:
        assert store.remove("999999") is False

    def test_invalid_id_is_rejected_and_nothing_written(
        self, store: WatchlistStore
    ) -> None:
        with pytest.raises(GoodsIdError):
            store.add("../etc/passwd")
        assert not store.path.exists()

    def test_limit_is_enforced(self, settings) -> None:
        store = WatchlistStore(settings.watchlist_path, max_items=2)
        store.add("111")
        store.add("222")
        with pytest.raises(WatchlistFull) as excinfo:
            store.add("333")
        assert "上限 2" in str(excinfo.value)
        assert store.count == 2

    def test_added_at_survives_reload(self, settings) -> None:
        store = WatchlistStore(
            settings.watchlist_path,
            max_items=10,
            clock=lambda: "2023-01-03T15:10:23Z",
        )
        store.add("42235")

        reloaded = WatchlistStore(settings.watchlist_path, max_items=10)
        entries = reloaded.entries()
        assert entries[0].goods_id == "42235"
        assert entries[0].added_at == "2023-01-03T15:10:23Z"


class TestConcurrency:
    def test_twenty_threads_add_only_one_record(self, store: WatchlistStore) -> None:
        successes: list[str] = []
        duplicates: list[str] = []
        barrier = threading.Barrier(20)

        def worker() -> None:
            barrier.wait()
            try:
                successes.append(store.add("42235").goods_id)
            except DuplicateGoods:
                duplicates.append("42235")

        with ThreadPoolExecutor(max_workers=20) as pool:
            list(pool.map(lambda _: worker(), range(20)))

        assert successes == ["42235"]
        assert len(duplicates) == 19
        assert store.count == 1
        assert len(read_json(store.path)["items"]) == 1

    def test_concurrent_distinct_adds_keep_file_valid(self, store: WatchlistStore) -> None:
        stop = threading.Event()
        failures: list[BaseException] = []

        def reader() -> None:
            while not stop.is_set():
                try:
                    payload = read_json(store.path)
                except FileNotFoundError:
                    continue
                except BaseException as exc:  # noqa: BLE001 - 记录任何解析失败
                    failures.append(exc)
                    return
                else:
                    if not isinstance(payload.get("items"), list):
                        failures.append(AssertionError("items 不是数组"))

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            ids = [str(100000 + index) for index in range(30)]
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(store.add, ids))
        finally:
            stop.set()
            thread.join(timeout=5)

        assert failures == []
        assert store.count == 30
        assert len(read_json(store.path)["items"]) == 30


class TestCorruption:
    def test_corrupt_file_is_backed_up_and_reset(self, store: WatchlistStore) -> None:
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text("{这不是 JSON", encoding="utf-8")

        assert store.entries() == ()
        assert store.count == 0
        backups = list(store.path.parent.glob("watchlist.json.corrupt-*"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == "{这不是 JSON"
        assert not store.path.exists()

    def test_wrong_structure_is_treated_as_corrupt(self, store: WatchlistStore) -> None:
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(json.dumps({"version": 1, "items": "oops"}), encoding="utf-8")

        assert store.entries() == ()
        assert list(store.path.parent.glob("watchlist.json.corrupt-*"))

    def test_duplicate_and_invalid_rows_are_ignored(self, store: WatchlistStore) -> None:
        store.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "items": [
                {"goods_id": "42235", "added_at": "2023-01-01T00:00:00Z"},
                {"goods_id": "42235", "added_at": "2023-01-02T00:00:00Z"},
                {"goods_id": "abc", "added_at": "2023-01-03T00:00:00Z"},
                {"goods_id": "769498"},
                {"nope": True},
            ],
        }
        store.path.write_text(json.dumps(payload), encoding="utf-8")

        entries = store.entries()
        assert [entry.goods_id for entry in entries] == ["42235", "769498"]
        assert entries[0].added_at == "2023-01-01T00:00:00Z"
        assert entries[1].added_at.endswith("Z")


class TestMigration:
    def test_real_legacy_file_is_migrated_in_order(self, settings) -> None:
        legacy = PROJECT_ROOT / "script" / "submit.buff"
        assert legacy.is_file()

        store = WatchlistStore(
            settings.watchlist_path, max_items=200, legacy_path=legacy
        )
        entries = store.entries()

        assert [entry.goods_id for entry in entries] == LEGACY_IDS
        assert read_json(store.path)["items"][0]["goods_id"] == "895581"
        assert store.path.exists()

    def test_migration_dedupes_and_skips_invalid_lines(self, settings, tmp_path: Path) -> None:
        legacy = tmp_path / "submit.buff"
        legacy.write_text(
            "42235\nabc\n42235\n 769498 \n\n../x\n895581", encoding="utf-8"
        )
        store = WatchlistStore(
            settings.watchlist_path, max_items=200, legacy_path=legacy
        )
        assert [entry.goods_id for entry in store.entries()] == [
            "42235",
            "769498",
            "895581",
        ]

    def test_migration_is_idempotent(self, settings, tmp_path: Path) -> None:
        legacy = tmp_path / "submit.buff"
        legacy.write_text("111\n222\n", encoding="utf-8")
        first = WatchlistStore(settings.watchlist_path, legacy_path=legacy)
        first.entries()
        content = settings.watchlist_path.read_text(encoding="utf-8")

        second = WatchlistStore(settings.watchlist_path, legacy_path=legacy)
        entries = second.entries()

        assert [entry.goods_id for entry in entries] == ["111", "222"]
        assert settings.watchlist_path.read_text(encoding="utf-8") == content

    def test_existing_file_wins_over_legacy(self, settings, tmp_path: Path) -> None:
        legacy = tmp_path / "submit.buff"
        legacy.write_text("111\n", encoding="utf-8")

        store = WatchlistStore(settings.watchlist_path)
        store.add("999")

        reopened = WatchlistStore(settings.watchlist_path, legacy_path=legacy)
        assert [entry.goods_id for entry in reopened.entries()] == ["999"]

    def test_no_legacy_file_starts_empty(self, settings) -> None:
        store = WatchlistStore(settings.watchlist_path, legacy_path=None)
        assert store.entries() == ()
        assert not store.path.exists()
