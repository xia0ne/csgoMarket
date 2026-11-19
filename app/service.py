"""关注列表业务：并发拉取、失败隔离与顺序稳定。"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

from app.buff import (
    BuffRateLimited,
    BuffTimeout,
    BuffUpstreamError,
    GoodsNotFound,
    GoodsSnapshot,
)
from app.schemas import utc_now_iso
from app.watchlist import WatchlistStore

logger = logging.getLogger(__name__)


class GoodsFetcher(Protocol):
    """``BuffClient`` 的最小接口，测试可注入假实现。"""

    def get_goods(self, goods_id: str, *, refresh: bool = False) -> GoodsSnapshot: ...


@dataclass(frozen=True, slots=True)
class FailedItem:
    goods_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class WatchlistResult:
    items: list[GoodsSnapshot]
    failed: list[FailedItem]
    took_ms: float
    fetched_at: str

    @property
    def count(self) -> int:
        return len(self.items) + len(self.failed)


def failure_reason(exc: BaseException) -> str:
    """把客户端异常转成面向前端的中文短原因。"""
    if isinstance(exc, GoodsNotFound):
        return "未找到该商品"
    if isinstance(exc, BuffTimeout):
        return "超时"
    if isinstance(exc, BuffRateLimited):
        return "BUFF 暂时限制访问"
    if isinstance(exc, BuffUpstreamError):
        return "BUFF 返回数据异常"
    return "获取失败"


class WatchlistService:
    def __init__(
        self,
        store: WatchlistStore,
        client: GoodsFetcher,
        *,
        max_workers: int = 6,
    ) -> None:
        self._store = store
        self._client = client
        self._max_workers = max(int(max_workers), 1)

    @property
    def store(self) -> WatchlistStore:
        return self._store

    def goods(self, goods_id: str, *, refresh: bool = False) -> GoodsSnapshot:
        return self._client.get_goods(goods_id, refresh=refresh)

    def watchlist(self, *, refresh: bool = False) -> WatchlistResult:
        """按关注列表原顺序并发拉取；单个失败只记录到 ``failed``。"""
        started = time.perf_counter()
        entries = self._store.entries()

        items: list[GoodsSnapshot | None] = [None] * len(entries)
        failed: list[FailedItem] = []

        if entries:
            workers = min(self._max_workers, len(entries))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="buff") as pool:
                futures = [
                    pool.submit(self._client.get_goods, entry.goods_id, refresh=refresh)
                    for entry in entries
                ]
                for index, (entry, future) in enumerate(zip(entries, futures, strict=True)):
                    try:
                        items[index] = future.result()
                    except Exception as exc:  # noqa: BLE001 - 单个商品失败必须隔离
                        reason = failure_reason(exc)
                        logger.warning(
                            "关注列表拉取失败 goods_id=%s reason=%s error=%r",
                            entry.goods_id,
                            reason,
                            exc,
                        )
                        failed.append(FailedItem(goods_id=entry.goods_id, reason=reason))

        took_ms = round((time.perf_counter() - started) * 1000, 2)
        succeeded = [item for item in items if item is not None]
        logger.info(
            "关注列表拉取完成 total=%d ok=%d failed=%d took=%.2fms",
            len(entries),
            len(succeeded),
            len(failed),
            took_ms,
        )
        return WatchlistResult(
            items=succeeded,
            failed=failed,
            took_ms=took_ms,
            fetched_at=utc_now_iso(),
        )
