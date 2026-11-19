"""BUFF 客户端：解析、容错、重试、缓存与请求参数。"""

from __future__ import annotations

import time

import pytest
import requests
from app.buff import (
    BuffClient,
    BuffRateLimited,
    BuffTimeout,
    BuffUpstreamError,
    GoodsNotFound,
)
from app.schemas import GoodsIdError

GOODS_ID = "42235"


def make_payload(
    goods_id: str = GOODS_ID,
    *,
    code: str = "OK",
    prices: tuple[str, ...] = ("7.0", "7.0", "7.2", "7.5", "7.8"),
    total_count: object = 91,
    steam: object = "8.26",
    sell_min: object = "7.0",
    market_min: object = "6.9",
    name: object = "USP消音版 | 力矩 (略有磨损)",
    **info_overrides: object,
) -> dict:
    info = {
        "name": name,
        "steam_price_cny": steam,
        "sell_min_price": sell_min,
        "market_min_price": market_min,
        "icon_url": "https://example.invalid/icon.png",
        "sell_reference_price": "8.0",
    }
    info.update(info_overrides)
    return {
        "code": code,
        "data": {
            "goods_infos": {goods_id: info},
            "items": [{"price": price} for price in prices],
            "total_count": total_count,
        },
    }


class FakeResponse:
    def __init__(
        self,
        payload: object = None,
        *,
        status_code: int = 200,
        json_error: bool = False,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self) -> object:
        if self._json_error:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.closed = False

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(
            {"url": url, "params": params, "headers": headers, "timeout": timeout}
        )
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def make_client(session: FakeSession, **kwargs) -> BuffClient:
    options = {
        "session": session,
        "sleep": lambda _seconds: None,
        "cache_ttl": 0.0,
    }
    options.update(kwargs)
    return BuffClient(**options)


class TestParsing:
    def test_success_normalizes_fields(self) -> None:
        session = FakeSession([FakeResponse(make_payload())])
        snapshot = make_client(session).get_goods(GOODS_ID)

        assert snapshot.goods_id == GOODS_ID
        assert snapshot.name == "USP消音版 | 力矩 (略有磨损)"
        assert snapshot.steam_price_cny == pytest.approx(8.26)
        assert snapshot.buff_min_price == pytest.approx(7.0)
        assert snapshot.sell_count == 91
        assert snapshot.top_prices == pytest.approx((7.0, 7.0, 7.2, 7.5, 7.8))
        assert snapshot.icon_url.startswith("https://")
        assert snapshot.cached is False
        assert snapshot.fetched_at.endswith("Z")

    def test_only_five_prices_are_kept(self) -> None:
        session = FakeSession(
            [FakeResponse(make_payload(prices=("1", "2", "3", "4", "5", "6", "7")))]
        )
        snapshot = make_client(session).get_goods(GOODS_ID)
        assert snapshot.top_prices == (1.0, 2.0, 3.0, 4.0, 5.0)

    def test_zero_orders_do_not_raise_and_fall_back(self) -> None:
        session = FakeSession([FakeResponse(make_payload(prices=(), total_count=0))])
        snapshot = make_client(session).get_goods(GOODS_ID)
        assert snapshot.top_prices == ()
        assert snapshot.buff_min_price == pytest.approx(7.0)
        assert snapshot.sell_count == 0

    def test_three_orders_are_returned_as_is(self) -> None:
        session = FakeSession(
            [FakeResponse(make_payload(prices=("7.1", "7.3", "7.4"), total_count=3))]
        )
        snapshot = make_client(session).get_goods(GOODS_ID)
        assert snapshot.top_prices == (7.1, 7.3, 7.4)

    def test_placeholder_zero_prices_become_none(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    make_payload(
                        prices=(),
                        sell_min="0",
                        market_min="0",
                        steam="0",
                        total_count=0,
                    )
                )
            ]
        )
        snapshot = make_client(session).get_goods(GOODS_ID)
        assert snapshot.buff_min_price is None
        assert snapshot.steam_price_cny is None

    def test_invalid_prices_are_ignored(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    make_payload(
                        prices=("abc", "7.5", "", "8.0"),
                        steam="not-a-price",
                        sell_min="oops",
                        market_min=-1,
                    )
                )
            ]
        )
        snapshot = make_client(session).get_goods(GOODS_ID)
        assert snapshot.top_prices == (7.5, 8.0)
        assert snapshot.steam_price_cny is None
        assert snapshot.buff_min_price == 7.5

    def test_top_price_used_when_sell_min_missing(self) -> None:
        session = FakeSession([FakeResponse(make_payload(sell_min=None, prices=("9.1",)))])
        snapshot = make_client(session).get_goods(GOODS_ID)
        assert snapshot.buff_min_price == 9.1

    def test_lower_order_price_wins_over_sell_min(self) -> None:
        # 实测 BUFF 会返回高于首条卖单的 sell_min_price（不含带冷却订单），
        # 展示的最低价不能高于前五卖单里可见的任何一条。
        session = FakeSession(
            [FakeResponse(make_payload(sell_min="24.4", prices=("24.0", "24.4")))]
        )
        snapshot = make_client(session).get_goods(GOODS_ID)
        assert snapshot.buff_min_price == 24.0

    def test_text_sell_count_is_coerced(self) -> None:
        session = FakeSession([FakeResponse(make_payload(total_count="12"))])
        assert make_client(session).get_goods(GOODS_ID).sell_count == 12

    def test_invalid_goods_id_is_rejected_before_network(self) -> None:
        session = FakeSession([])
        with pytest.raises(GoodsIdError):
            make_client(session).get_goods("../1")
        assert session.calls == []


class TestUpstreamErrors:
    def test_code_not_ok_is_not_found(self) -> None:
        session = FakeSession([FakeResponse(make_payload(code="ERROR"))])
        with pytest.raises(GoodsNotFound):
            make_client(session).get_goods(GOODS_ID)
        assert len(session.calls) == 1

    def test_missing_goods_info_is_not_found(self) -> None:
        payload = make_payload()
        payload["data"]["goods_infos"] = {}
        session = FakeSession([FakeResponse(payload)])
        with pytest.raises(GoodsNotFound):
            make_client(session).get_goods(GOODS_ID)

    def test_missing_code_is_upstream_error(self) -> None:
        session = FakeSession([FakeResponse({"data": {}})])
        with pytest.raises(BuffUpstreamError):
            make_client(session).get_goods(GOODS_ID)

    def test_data_not_object_is_upstream_error(self) -> None:
        session = FakeSession([FakeResponse({"code": "OK", "data": []})])
        with pytest.raises(BuffUpstreamError):
            make_client(session).get_goods(GOODS_ID)

    def test_items_not_list_is_upstream_error(self) -> None:
        payload = make_payload()
        payload["data"]["items"] = "oops"
        session = FakeSession([FakeResponse(payload)])
        with pytest.raises(BuffUpstreamError):
            make_client(session).get_goods(GOODS_ID)

    def test_non_json_body_is_upstream_error(self) -> None:
        session = FakeSession([FakeResponse(json_error=True)])
        with pytest.raises(BuffUpstreamError):
            make_client(session).get_goods(GOODS_ID)

    def test_forbidden_status_is_upstream_error_without_retry(self) -> None:
        session = FakeSession([FakeResponse(status_code=403)])
        with pytest.raises(BuffUpstreamError):
            make_client(session).get_goods(GOODS_ID)
        assert len(session.calls) == 1


class TestRetries:
    def test_server_error_then_success(self) -> None:
        session = FakeSession(
            [FakeResponse(status_code=500), FakeResponse(status_code=502), FakeResponse(make_payload())]
        )
        snapshot = make_client(session).get_goods(GOODS_ID)
        assert snapshot.goods_id == GOODS_ID
        assert len(session.calls) == 3

    def test_server_errors_are_exhausted(self) -> None:
        session = FakeSession([FakeResponse(status_code=500) for _ in range(3)])
        with pytest.raises(BuffUpstreamError):
            make_client(session).get_goods(GOODS_ID)
        assert len(session.calls) == 3

    def test_timeout_maps_to_buff_timeout(self) -> None:
        session = FakeSession([requests.Timeout("boom") for _ in range(3)])
        with pytest.raises(BuffTimeout):
            make_client(session).get_goods(GOODS_ID)
        assert len(session.calls) == 3

    def test_connection_error_maps_to_buff_timeout(self) -> None:
        session = FakeSession([requests.ConnectionError("down") for _ in range(3)])
        with pytest.raises(BuffTimeout):
            make_client(session).get_goods(GOODS_ID)

    def test_rate_limit_is_retried_then_raised(self) -> None:
        session = FakeSession([FakeResponse(status_code=429) for _ in range(3)])
        with pytest.raises(BuffRateLimited):
            make_client(session).get_goods(GOODS_ID)
        assert len(session.calls) == 3

    def test_backoff_delays_are_exponential(self) -> None:
        delays: list[float] = []
        session = FakeSession([FakeResponse(status_code=500) for _ in range(3)])
        client = make_client(session, sleep=delays.append)
        with pytest.raises(BuffUpstreamError):
            client.get_goods(GOODS_ID)
        assert delays == pytest.approx([0.3, 0.6])


class TestCache:
    def test_cache_hit_skips_network(self) -> None:
        session = FakeSession([FakeResponse(make_payload())])
        client = make_client(session, cache_ttl=30.0)

        first = client.get_goods(GOODS_ID)
        second = client.get_goods(GOODS_ID)

        assert first.cached is False
        assert second.cached is True
        assert len(session.calls) == 1
        assert client.cache_entries == 1

    def test_refresh_bypasses_cache(self) -> None:
        session = FakeSession([FakeResponse(make_payload()), FakeResponse(make_payload())])
        client = make_client(session, cache_ttl=30.0)
        client.get_goods(GOODS_ID)
        client.get_goods(GOODS_ID, refresh=True)
        assert len(session.calls) == 2

    def test_ttl_expiry_triggers_refetch(self) -> None:
        now = [1000.0]
        session = FakeSession([FakeResponse(make_payload()), FakeResponse(make_payload())])
        client = make_client(session, cache_ttl=30.0, clock=lambda: now[0])

        client.get_goods(GOODS_ID)
        now[0] += 31
        snapshot = client.get_goods(GOODS_ID)

        assert snapshot.cached is False
        assert len(session.calls) == 2

    def test_zero_ttl_disables_cache(self) -> None:
        session = FakeSession([FakeResponse(make_payload()), FakeResponse(make_payload())])
        client = make_client(session, cache_ttl=0)
        client.get_goods(GOODS_ID)
        client.get_goods(GOODS_ID)
        assert len(session.calls) == 2
        assert client.cache_entries == 0

    def test_cache_is_bounded(self) -> None:
        session = FakeSession(
            [
                FakeResponse(make_payload("111")),
                FakeResponse(make_payload("222")),
            ]
        )
        client = make_client(session, cache_ttl=30.0, max_cache_entries=1)
        client.get_goods("111")
        client.get_goods("222")
        assert client.cache_entries == 1

    def test_clear_cache(self) -> None:
        session = FakeSession([FakeResponse(make_payload())])
        client = make_client(session, cache_ttl=30.0)
        client.get_goods(GOODS_ID)
        client.clear_cache()
        assert client.cache_entries == 0


class TestRequestShape:
    def test_params_and_headers(self) -> None:
        session = FakeSession([FakeResponse(make_payload())])
        client = make_client(session)
        client.get_goods(GOODS_ID)

        call = session.calls[0]
        assert call["url"] == "https://buff.163.com/api/market/goods/sell_order"
        assert call["params"]["goods_id"] == GOODS_ID
        assert call["params"]["game"] == "csgo"
        assert call["params"]["page_num"] == 1
        assert abs(call["params"]["_"] - time.time() * 1000) < 5000
        assert call["params"]["_"] != 1672670602560
        assert call["headers"]["referer"] == f"https://buff.163.com/goods/{GOODS_ID}"
        assert "Mozilla" in call["headers"]["user-agent"]
        assert "cookie" not in call["headers"]
        assert call["timeout"] == (5.0, 10.0)

    def test_cookie_is_forwarded(self) -> None:
        session = FakeSession([FakeResponse(make_payload())])
        make_client(session, cookie="session=abc").get_goods(GOODS_ID)
        assert session.calls[0]["headers"]["cookie"] == "session=abc"

    def test_close_delegates_to_session(self) -> None:
        session = FakeSession([])
        make_client(session).close()
        assert session.closed is True
