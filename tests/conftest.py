"""pytest 公共 fixture：临时关注列表、伪 BUFF 客户端与 TestClient。"""

from __future__ import annotations

import threading
import time
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

import pytest
from app import create_app
from app.buff import GoodsSnapshot
from app.config import Settings
from app.service import WatchlistService
from app.watchlist import WatchlistStore
from fastapi.testclient import TestClient

FIXED_TIME = "2026-11-18T09:30:00Z"


def make_snapshot(
    goods_id: str = "42235",
    *,
    name: str | None = None,
    steam_price_cny: float | None = 8.26,
    buff_min_price: float | None = 7.0,
    sell_count: int = 91,
    top_prices: tuple[float, ...] = (7.0, 7.0, 7.2, 7.5, 7.8),
    icon_url: str = "https://example.invalid/icon.png",
    cached: bool = False,
) -> GoodsSnapshot:
    return GoodsSnapshot(
        goods_id=goods_id,
        name=name or f"测试饰品 {goods_id}",
        steam_price_cny=steam_price_cny,
        buff_min_price=buff_min_price,
        sell_count=sell_count,
        top_prices=top_prices,
        icon_url=icon_url,
        fetched_at=FIXED_TIME,
        cached=cached,
    )


class FakeClient:
    """可注入错误、延迟与并发统计的假 BUFF 客户端。"""

    def __init__(
        self,
        *,
        snapshots: dict[str, GoodsSnapshot] | None = None,
        errors: dict[str, BaseException] | None = None,
        delays: dict[str, float] | None = None,
        default_delay: float = 0.0,
    ) -> None:
        self.snapshots = snapshots or {}
        self.errors = errors or {}
        self.delays = delays or {}
        self.default_delay = default_delay
        self.calls: list[tuple[str, bool]] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def get_goods(self, goods_id: str, *, refresh: bool = False) -> GoodsSnapshot:
        with self._lock:
            self.calls.append((goods_id, refresh))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            delay = self.delays.get(goods_id, self.default_delay)
            if delay:
                time.sleep(delay)
            error = self.errors.get(goods_id)
            if error is not None:
                raise error
            snapshot = self.snapshots.get(goods_id)
            if snapshot is not None:
                return replace(snapshot, cached=False)
            return make_snapshot(goods_id)
        finally:
            with self._lock:
                self.active -= 1

    @property
    def cache_entries(self) -> int:
        return 0


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """每个测试独立的关注列表路径，绝不碰仓库里的 data/。"""
    return Settings(
        watchlist_path=tmp_path / "data" / "watchlist.json",
        legacy_path=None,
    )


@pytest.fixture
def store(settings: Settings) -> WatchlistStore:
    return WatchlistStore(settings.watchlist_path, max_items=settings.max_items)


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def make_client(settings: Settings, fake_client: FakeClient):
    """构造带伪客户端的 TestClient；支持预置关注列表与配置覆盖。"""
    stack = ExitStack()

    def factory(
        *,
        seed: tuple[str, ...] = (),
        fake: FakeClient | None = None,
        config: Settings | None = None,
    ) -> TestClient:
        resolved = config or settings
        store = WatchlistStore(resolved.watchlist_path, max_items=resolved.max_items)
        for goods_id in seed:
            store.add(goods_id)

        app = create_app(resolved)
        active_fake = fake or fake_client
        app.state.store = store
        app.state.client = active_fake
        app.state.service = WatchlistService(
            store, active_fake, max_workers=resolved.max_workers
        )

        client = stack.enter_context(TestClient(app))
        client.fake = active_fake  # type: ignore[attr-defined]
        client.store = store  # type: ignore[attr-defined]
        return client

    yield factory
    stack.close()


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()
