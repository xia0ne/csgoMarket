"""关注列表业务：并发、失败隔离、顺序与耗时。"""

from __future__ import annotations

import time

import pytest
from app.buff import (
    BuffRateLimited,
    BuffTimeout,
    BuffUpstreamError,
    GoodsNotFound,
)
from app.service import WatchlistService, failure_reason
from app.watchlist import WatchlistStore

from conftest import FakeClient, make_snapshot


def seed_store(store: WatchlistStore, ids: list[str]) -> None:
    for goods_id in ids:
        store.add(goods_id)


class TestWatchlistFetch:
    def test_empty_watchlist(self, store: WatchlistStore, fake_client: FakeClient) -> None:
        result = WatchlistService(store, fake_client).watchlist()
        assert result.count == 0
        assert result.items == []
        assert result.failed == []
        assert result.took_ms >= 0
        assert result.fetched_at.endswith("Z")

    def test_fetch_is_concurrent(self, store: WatchlistStore) -> None:
        seed_store(store, ["111", "222", "333"])
        fake = FakeClient(default_delay=0.1)
        service = WatchlistService(store, fake, max_workers=3)

        started = time.perf_counter()
        result = service.watchlist()
        elapsed = time.perf_counter() - started

        assert fake.max_active >= 2
        assert len(result.items) == 3
        assert elapsed < 0.25

    def test_single_worker_is_serial(self, store: WatchlistStore) -> None:
        seed_store(store, ["111", "222", "333"])
        fake = FakeClient(default_delay=0.01)
        service = WatchlistService(store, fake, max_workers=1)
        service.watchlist()
        assert fake.max_active == 1

    def test_one_failure_does_not_break_others(
        self, store: WatchlistStore
    ) -> None:
        seed_store(store, ["111", "222", "333"])
        fake = FakeClient(errors={"222": BuffTimeout("down")})
        result = WatchlistService(store, fake).watchlist()

        assert [item.goods_id for item in result.items] == ["111", "333"]
        assert [item.goods_id for item in result.failed] == ["222"]
        assert result.failed[0].reason == "超时"
        assert result.count == 3

    def test_all_failures_are_reported(self, store: WatchlistStore) -> None:
        seed_store(store, ["111", "222"])
        fake = FakeClient(
            errors={
                "111": BuffRateLimited("429"),
                "222": BuffUpstreamError("bad"),
            }
        )
        result = WatchlistService(store, fake).watchlist()
        assert result.items == []
        assert [item.reason for item in result.failed] == [
            "BUFF 暂时限制访问",
            "BUFF 返回数据异常",
        ]

    def test_order_matches_watchlist_even_with_slow_first(
        self, store: WatchlistStore
    ) -> None:
        seed_store(store, ["111", "222", "333"])
        fake = FakeClient(delays={"111": 0.08}, default_delay=0.01)
        result = WatchlistService(store, fake, max_workers=3).watchlist()
        assert [item.goods_id for item in result.items] == ["111", "222", "333"]

    def test_refresh_flag_is_forwarded(self, store: WatchlistStore) -> None:
        seed_store(store, ["111", "222"])
        fake = FakeClient()
        WatchlistService(store, fake).watchlist(refresh=True)
        assert [refresh for _, refresh in fake.calls] == [True, True]

    def test_cached_snapshot_is_preserved(self, store: WatchlistStore) -> None:
        seed_store(store, ["111"])
        fake = FakeClient(snapshots={"111": make_snapshot("111", cached=True)})
        result = WatchlistService(store, fake).watchlist()
        assert result.items[0].cached is False

    def test_took_ms_is_measured(self, store: WatchlistStore) -> None:
        seed_store(store, ["111"])
        fake = FakeClient(default_delay=0.02)
        result = WatchlistService(store, fake).watchlist()
        assert result.took_ms >= 15


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (GoodsNotFound("x"), "未找到该商品"),
        (BuffTimeout("x"), "超时"),
        (BuffRateLimited("x"), "BUFF 暂时限制访问"),
        (BuffUpstreamError("x"), "BUFF 返回数据异常"),
        (RuntimeError("x"), "获取失败"),
    ],
)
def test_failure_reason_mapping(error: BaseException, reason: str) -> None:
    assert failure_reason(error) == reason
