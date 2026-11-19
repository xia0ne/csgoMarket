"""BUFF 客户端：带超时、重试、TTL 缓存与响应归一化。

只依赖 ``requests``，不感知 FastAPI；测试可以注入伪 Session、伪时钟与
no-op sleep，从而完全离线运行。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import requests

from app.schemas import normalize_goods_id, utc_now_iso

logger = logging.getLogger(__name__)

BUFF_SELL_ORDER_URL = "https://buff.163.com/api/market/goods/sell_order"
BUFF_GOODS_PAGE = "https://buff.163.com/goods/{goods_id}"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MAX_TOP_PRICES = 5
DEFAULT_MAX_CACHE_ENTRIES = 512


class BuffError(Exception):
    """BUFF 客户端异常基类。"""


class GoodsNotFound(BuffError):
    """商品不存在，或 BUFF 明确返回失败。"""


class BuffTimeout(BuffError):
    """超时、连接失败或网络错误。"""


class BuffRateLimited(BuffError):
    """触发 BUFF 访问频率限制（HTTP 429）。"""


class BuffUpstreamError(BuffError):
    """BUFF 返回了无法解析或不符合预期的数据。"""


@dataclass(frozen=True, slots=True)
class GoodsSnapshot:
    """归一化后的单件饰品行情。"""

    goods_id: str
    name: str
    steam_price_cny: float | None
    buff_min_price: float | None
    sell_count: int
    top_prices: tuple[float, ...]
    icon_url: str
    fetched_at: str
    cached: bool = False


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    expires_at: float
    snapshot: GoodsSnapshot


def _to_price(value: Any) -> float | None:
    """把接口里的价格字符串/数字转成 float；非法值和占位的 0 一律视为无。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if price <= 0 or price != price:  # NaN 自比较为假
        return None
    return price


def _to_count(value: Any, fallback: int) -> int:
    if value is None or isinstance(value, bool):
        return fallback
    try:
        count = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(count, 0)


class BuffClient:
    """访问 BUFF 卖单接口的线程安全客户端。"""

    def __init__(
        self,
        *,
        timeout: tuple[float, float] = (5.0, 10.0),
        cache_ttl: float = 30.0,
        max_cache_entries: int = DEFAULT_MAX_CACHE_ENTRIES,
        cookie: str | None = None,
        max_retries: int = 2,
        backoff: tuple[float, ...] = (0.3, 0.6),
        session: Any | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._timeout = timeout
        self._cache_ttl = max(cache_ttl, 0.0)
        self._max_cache_entries = max(max_cache_entries, 1)
        self._cookie = cookie
        self._max_retries = max(max_retries, 0)
        self._backoff = backoff or (0.0,)
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep if sleep is not None else time.sleep
        self._clock = clock if clock is not None else time.monotonic
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._cache_lock = threading.RLock()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------
    def get_goods(self, goods_id: str | int, *, refresh: bool = False) -> GoodsSnapshot:
        """获取单个商品行情；``refresh=True`` 跳过缓存读取。"""
        gid = normalize_goods_id(goods_id)

        if not refresh:
            cached = self._cache_get(gid)
            if cached is not None:
                logger.info("BUFF 缓存命中 goods_id=%s", gid)
                return replace(cached, cached=True)

        snapshot = self._fetch_with_retry(gid)
        self._cache_put(gid, snapshot)
        return snapshot

    @property
    def cache_entries(self) -> int:
        """当前缓存条数（含尚未过期的条目）。"""
        with self._cache_lock:
            return len(self._cache)

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    def close(self) -> None:
        close = getattr(self._session, "close", None)
        if callable(close):
            close()

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------
    def _cache_get(self, goods_id: str) -> GoodsSnapshot | None:
        if self._cache_ttl <= 0:
            return None
        with self._cache_lock:
            entry = self._cache.get(goods_id)
            if entry is None:
                return None
            if entry.expires_at <= self._clock():
                del self._cache[goods_id]
                return None
            self._cache.move_to_end(goods_id)
            return entry.snapshot

    def _cache_put(self, goods_id: str, snapshot: GoodsSnapshot) -> None:
        if self._cache_ttl <= 0:
            return
        with self._cache_lock:
            self._cache[goods_id] = _CacheEntry(
                expires_at=self._clock() + self._cache_ttl,
                snapshot=snapshot,
            )
            self._cache.move_to_end(goods_id)
            while len(self._cache) > self._max_cache_entries:
                self._cache.popitem(last=False)

    # ------------------------------------------------------------------
    # 网络请求
    # ------------------------------------------------------------------
    def _headers(self, goods_id: str) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9",
            "referer": BUFF_GOODS_PAGE.format(goods_id=goods_id),
            "user-agent": DEFAULT_USER_AGENT,
        }
        if self._cookie:
            headers["cookie"] = self._cookie
        return headers

    def _fetch_with_retry(self, goods_id: str) -> GoodsSnapshot:
        attempts = self._max_retries + 1
        last_error: BuffError | None = None

        for attempt in range(1, attempts + 1):
            if attempt > 1:
                delay = self._backoff[min(attempt - 2, len(self._backoff) - 1)]
                logger.warning(
                    "BUFF 请求重试 goods_id=%s attempt=%d/%d delay=%.1fs",
                    goods_id,
                    attempt,
                    attempts,
                    delay,
                )
                self._sleep(delay)

            started = time.perf_counter()
            try:
                response = self._session.get(
                    BUFF_SELL_ORDER_URL,
                    params=self._params(goods_id),
                    headers=self._headers(goods_id),
                    timeout=self._timeout,
                )
            except requests.Timeout as exc:
                last_error = BuffTimeout("访问 BUFF 超时")
                self._log_attempt(goods_id, attempt, attempts, started, error="timeout")
                logger.debug("BUFF 超时详情：%s", exc)
                continue
            except requests.RequestException as exc:
                last_error = BuffTimeout("访问 BUFF 网络异常")
                self._log_attempt(goods_id, attempt, attempts, started, error=type(exc).__name__)
                continue

            status = int(getattr(response, "status_code", 0))

            if status == 429:
                last_error = BuffRateLimited("BUFF 暂时限制访问")
                self._log_attempt(goods_id, attempt, attempts, started, status=status)
                continue
            if status >= 500:
                last_error = BuffUpstreamError(f"BUFF 服务端错误（HTTP {status}）")
                self._log_attempt(goods_id, attempt, attempts, started, status=status)
                continue
            if status != 200:
                self._log_attempt(goods_id, attempt, attempts, started, status=status)
                raise BuffUpstreamError(f"BUFF 返回异常状态码（HTTP {status}）")

            try:
                payload = response.json()
            except ValueError as exc:
                self._log_attempt(goods_id, attempt, attempts, started, status=status, error="json")
                raise BuffUpstreamError("BUFF 返回数据异常") from exc

            snapshot = self._parse(goods_id, payload)
            self._log_attempt(goods_id, attempt, attempts, started, status=status)
            return snapshot

        assert last_error is not None
        raise last_error

    def _log_attempt(
        self,
        goods_id: str,
        attempt: int,
        attempts: int,
        started: float,
        *,
        status: int | None = None,
        error: str | None = None,
    ) -> None:
        elapsed_ms = (time.perf_counter() - started) * 1000
        if error is None and status == 200:
            logger.info(
                "BUFF 请求成功 goods_id=%s status=%s attempt=%d/%d took=%.1fms",
                goods_id,
                status,
                attempt,
                attempts,
                elapsed_ms,
            )
        else:
            logger.warning(
                "BUFF 请求失败 goods_id=%s status=%s error=%s attempt=%d/%d took=%.1fms",
                goods_id,
                status if status is not None else "-",
                error or "http",
                attempt,
                attempts,
                elapsed_ms,
            )

    @staticmethod
    def _params(goods_id: str) -> dict[str, Any]:
        return {
            "game": "csgo",
            "goods_id": goods_id,
            "page_num": 1,
            "sort_by": "default",
            "mode": "",
            "allow_tradable_cooldown": 1,
            "_": int(time.time() * 1000),
        }

    # ------------------------------------------------------------------
    # 响应归一化
    # ------------------------------------------------------------------
    def _parse(self, goods_id: str, payload: Any) -> GoodsSnapshot:
        if not isinstance(payload, dict):
            raise BuffUpstreamError("BUFF 返回数据异常")
        if "code" not in payload:
            raise BuffUpstreamError("BUFF 返回数据异常")
        if payload.get("code") != "OK":
            raise GoodsNotFound(goods_id)

        data = payload.get("data")
        if not isinstance(data, dict):
            raise BuffUpstreamError("BUFF 返回数据异常")

        goods_infos = data.get("goods_infos")
        if not isinstance(goods_infos, dict):
            raise BuffUpstreamError("BUFF 返回数据异常")

        info = goods_infos.get(goods_id)
        if not isinstance(info, dict):
            raise GoodsNotFound(goods_id)

        name = info.get("name")
        if not isinstance(name, str) or not name.strip():
            raise BuffUpstreamError("BUFF 返回数据异常")

        raw_items = data.get("items")
        if raw_items is None:
            raw_items = []
        if not isinstance(raw_items, list):
            raise BuffUpstreamError("BUFF 返回数据异常")

        top_prices: list[float] = []
        for item in raw_items:
            if len(top_prices) >= MAX_TOP_PRICES:
                break
            if not isinstance(item, dict):
                continue
            price = _to_price(item.get("price"))
            if price is not None:
                top_prices.append(price)

        sell_min_price = _to_price(info.get("sell_min_price"))
        market_min_price = _to_price(info.get("market_min_price"))
        # BUFF 的 sell_min_price 可能不含带冷却/议价的卖单，与页面上可见的最低价不一致；
        # 两者都取到的时候用较小值，保证「最低价」不会高于前五卖单里的任何一条。
        candidates = [price for price in (sell_min_price, *top_prices) if price is not None]
        buff_min_price = min(candidates) if candidates else market_min_price

        sell_count = _to_count(data.get("total_count"), len(raw_items))
        icon_url = info.get("icon_url")
        if not isinstance(icon_url, str):
            icon_url = ""

        return GoodsSnapshot(
            goods_id=goods_id,
            name=name.strip(),
            steam_price_cny=_to_price(info.get("steam_price_cny")),
            buff_min_price=buff_min_price,
            sell_count=sell_count,
            top_prices=tuple(top_prices),
            icon_url=icon_url,
            fetched_at=utc_now_iso(),
        )
