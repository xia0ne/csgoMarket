"""API 契约：商品、关注列表、健康检查、错误处理、CORS 与静态资源。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from app.buff import (
    BuffRateLimited,
    BuffTimeout,
    BuffUpstreamError,
    GoodsNotFound,
)
from app.config import (
    DEFAULT_MAX_ITEMS,
    DEFAULT_MAX_WORKERS,
    PROJECT_ROOT,
    ConfigError,
    Settings,
)

from conftest import FakeClient

GOODS_ID = "42235"


# ---------------------------------------------------------------------------
# 商品行情
# ---------------------------------------------------------------------------
class TestGoodsEndpoint:
    def test_success_shape(self, client) -> None:
        response = client.get(f"/api/v1/goods/{GOODS_ID}")

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "goods_id",
            "name",
            "steam_price_cny",
            "buff_min_price",
            "sell_count",
            "top_prices",
            "icon_url",
            "fetched_at",
            "cached",
        }
        assert body["goods_id"] == GOODS_ID
        assert body["steam_price_cny"] == pytest.approx(8.26)
        assert body["buff_min_price"] == pytest.approx(7.0)
        assert body["sell_count"] == 91
        assert body["top_prices"] == [7.0, 7.0, 7.2, 7.5, 7.8]
        assert body["cached"] is False
        assert client.fake.calls == [(GOODS_ID, False)]

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/goods/",
            "/api/v1/goods/abc",
            "/api/v1/goods/12a",
            "/api/v1/goods/1234567890123",
            "/api/v1/goods/42235%20",
            "/api/v1/goods/%2042235",
            "/api/v1/goods/4223.5",
            "/api/v1/goods/..%2Fetc",
        ],
    )
    def test_invalid_ids_return_422(self, client, path: str) -> None:
        response = client.get(path)
        assert response.status_code == 422
        assert response.json() == {"detail": "商品 ID 必须是 1–12 位纯数字。"}

    @pytest.mark.parametrize(
        ("error", "status", "detail"),
        [
            (GoodsNotFound("x"), 404, "未找到该商品"),
            (BuffTimeout("x"), 504, "访问 BUFF 超时，请稍后重试"),
            (BuffRateLimited("x"), 503, "BUFF 暂时限制访问，请稍后重试"),
            (BuffUpstreamError("x"), 502, "BUFF 返回数据异常"),
        ],
    )
    def test_upstream_failures_are_mapped(
        self, make_client, error: BaseException, status: int, detail: str
    ) -> None:
        client = make_client(fake=FakeClient(errors={GOODS_ID: error}))
        response = client.get(f"/api/v1/goods/{GOODS_ID}")
        assert response.status_code == status
        assert response.json() == {"detail": detail}


# ---------------------------------------------------------------------------
# 关注列表
# ---------------------------------------------------------------------------
class TestWatchlistEndpoint:
    def test_full_flow(self, make_client) -> None:
        client = make_client()

        created = client.post("/api/v1/watchlist", json={"goods_id": GOODS_ID})
        assert created.status_code == 201
        body = created.json()
        assert set(body) == {"goods_id", "added_at"}
        assert body["goods_id"] == GOODS_ID
        assert body["added_at"].endswith("Z")

        listed = client.get("/api/v1/watchlist")
        assert listed.status_code == 200
        payload = listed.json()
        assert payload["count"] == 1
        assert [item["goods_id"] for item in payload["items"]] == [GOODS_ID]
        assert payload["failed"] == []
        assert isinstance(payload["took_ms"], (int, float))
        assert payload["fetched_at"].endswith("Z")

        deleted = client.delete(f"/api/v1/watchlist/{GOODS_ID}")
        assert deleted.status_code == 204
        assert deleted.content == b""

        assert client.get("/api/v1/watchlist").json()["count"] == 0

    def test_numeric_goods_id_is_accepted(self, make_client) -> None:
        client = make_client()
        response = client.post("/api/v1/watchlist", json={"goods_id": 42235})
        assert response.status_code == 201
        assert response.json()["goods_id"] == "42235"

    def test_duplicate_returns_409(self, make_client) -> None:
        client = make_client(seed=(GOODS_ID,))
        response = client.post("/api/v1/watchlist", json={"goods_id": GOODS_ID})
        assert response.status_code == 409
        assert response.json() == {"detail": "该商品已在关注列表中"}

    def test_duplicate_is_rejected_before_calling_buff(self, make_client) -> None:
        fake = FakeClient()
        client = make_client(seed=(GOODS_ID,), fake=fake)
        response = client.post("/api/v1/watchlist", json={"goods_id": GOODS_ID})
        assert response.status_code == 409
        assert fake.calls == []

    def test_unknown_goods_returns_404(self, make_client) -> None:
        fake = FakeClient(errors={"999999": GoodsNotFound("x")})
        client = make_client(fake=fake)
        response = client.post("/api/v1/watchlist", json={"goods_id": "999999"})
        assert response.status_code == 404
        assert response.json() == {"detail": "未找到该商品"}
        assert client.get("/api/v1/watchlist").json()["count"] == 0

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"goods_id": ""},
            {"goods_id": "abc"},
            {"goods_id": "1234567890123"},
            {"goods_id": " 42235"},
            {"goods_id": "../1"},
            {"goods_id": 42235.0},
            {"goods_id": True},
            {"goods_id": None},
        ],
    )
    def test_invalid_body_returns_422(self, client, payload: dict) -> None:
        response = client.post("/api/v1/watchlist", json=payload)
        assert response.status_code == 422
        assert response.json() == {"detail": "商品 ID 必须是 1–12 位纯数字。"}

    def test_extra_fields_are_rejected(self, client) -> None:
        response = client.post(
            "/api/v1/watchlist", json={"goods_id": GOODS_ID, "name": "x"}
        )
        assert response.status_code == 422

    def test_limit_returns_409(self, make_client, settings) -> None:
        config = replace(settings, max_items=1)
        client = make_client(seed=("111",), config=config)
        response = client.post("/api/v1/watchlist", json={"goods_id": "222"})
        assert response.status_code == 409
        assert response.json() == {"detail": "关注列表已满（上限 1）"}
        assert client.fake.calls == []

    def test_delete_missing_returns_404(self, client) -> None:
        response = client.delete("/api/v1/watchlist/999999")
        assert response.status_code == 404
        assert response.json() == {"detail": "该商品不在关注列表中"}

    def test_delete_invalid_id_returns_422(self, client) -> None:
        response = client.delete("/api/v1/watchlist/abc")
        assert response.status_code == 422

    def test_failed_items_are_isolated(self, make_client) -> None:
        fake = FakeClient(errors={"222": BuffTimeout("down")})
        client = make_client(seed=("111", "222", "333"), fake=fake)

        payload = client.get("/api/v1/watchlist").json()
        assert payload["count"] == 3
        assert [item["goods_id"] for item in payload["items"]] == ["111", "333"]
        assert payload["failed"] == [{"goods_id": "222", "reason": "超时"}]

    def test_watchlist_order_is_preserved(self, make_client) -> None:
        fake = FakeClient(delays={"111": 0.05})
        client = make_client(seed=("111", "222", "333"), fake=fake)
        payload = client.get("/api/v1/watchlist").json()
        assert [item["goods_id"] for item in payload["items"]] == ["111", "222", "333"]

    def test_refresh_query_bypasses_cache(self, make_client) -> None:
        client = make_client(seed=(GOODS_ID,))
        client.get("/api/v1/watchlist")
        client.get("/api/v1/watchlist?refresh=true")
        assert [refresh for _, refresh in client.fake.calls] == [False, True]

    def test_empty_watchlist(self, client) -> None:
        payload = client.get("/api/v1/watchlist").json()
        assert payload["count"] == 0
        assert payload["items"] == []
        assert payload["failed"] == []
        assert payload["took_ms"] >= 0

    def test_all_failed_still_200(self, make_client) -> None:
        fake = FakeClient(errors={"111": BuffUpstreamError("bad")})
        client = make_client(seed=("111",), fake=fake)
        response = client.get("/api/v1/watchlist")
        assert response.status_code == 200
        assert response.json()["items"] == []
        assert response.json()["failed"][0]["reason"] == "BUFF 返回数据异常"


# ---------------------------------------------------------------------------
# 运维端点与页面
# ---------------------------------------------------------------------------
class TestOpsEndpoints:
    def test_healthz_empty(self, client) -> None:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "watchlist_count": 0,
            "cache_entries": 0,
        }

    def test_healthz_counts_watchlist(self, make_client) -> None:
        client = make_client(seed=("111", "222"))
        body = client.get("/healthz").json()
        assert body["watchlist_count"] == 2

    def test_index_page(self, client) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        html = response.text
        assert 'lang="zh-CN"' in html
        assert "csgoMarket" in html
        assert "数据来自网易 BUFF" in html
        assert "layui" not in html.lower()

    def test_static_assets(self, client) -> None:
        css = client.get("/static/style.css")
        assert css.status_code == 200
        assert "text/css" in css.headers["content-type"]
        js = client.get("/static/app.js")
        assert js.status_code == 200
        assert "javascript" in js.headers["content-type"]
        assert "AbortController" in js.text

    def test_templates_directory_is_not_exposed(self, client) -> None:
        assert client.get("/templates/index.html").status_code == 404
        assert client.get("/templates/test.html").status_code == 404

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/buff/42235"),
            ("get", "/get"),
            ("post", "/submit/42235"),
        ],
    )
    def test_legacy_endpoints_are_gone(self, client, method: str, path: str) -> None:
        response = getattr(client, method)(path)
        assert response.status_code == 404
        assert response.json() == {"detail": "未找到该资源"}


class TestCors:
    def test_disabled_by_default(self, client) -> None:
        response = client.get("/healthz", headers={"Origin": "https://example.com"})
        assert "access-control-allow-origin" not in response.headers

    def test_enabled_from_settings(self, make_client, settings) -> None:
        config = replace(settings, cors_origins=("https://example.com",))
        client = make_client(config=config)
        response = client.get(
            "/healthz",
            headers={"Origin": "https://example.com"},
        )
        assert response.headers["access-control-allow-origin"] == "https://example.com"

    def test_other_origins_are_rejected(self, make_client, settings) -> None:
        config = replace(settings, cors_origins=("https://example.com",))
        client = make_client(config=config)
        response = client.get("/healthz", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in response.headers


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
class TestConfig:
    def test_defaults(self) -> None:
        settings = Settings.from_env({})
        assert settings.watchlist_path == (PROJECT_ROOT / "data" / "watchlist.json").resolve()
        assert settings.cache_ttl == 30.0
        assert settings.http_timeout == (5.0, 10.0)
        assert settings.max_workers == DEFAULT_MAX_WORKERS
        assert settings.max_items == DEFAULT_MAX_ITEMS
        assert settings.cors_origins == ()
        assert settings.buff_cookie is None
        assert settings.legacy_path == PROJECT_ROOT / "script" / "submit.buff"

    def test_relative_and_absolute_paths(self, tmp_path: Path) -> None:
        relative = Settings.from_env({"CSGO_WATCHLIST_PATH": "var/list.json"})
        assert relative.watchlist_path == (PROJECT_ROOT / "var" / "list.json").resolve()

        absolute = Settings.from_env({"CSGO_WATCHLIST_PATH": str(tmp_path / "list.json")})
        assert absolute.watchlist_path == (tmp_path / "list.json").resolve()

    @pytest.mark.parametrize(
        ("env", "name"),
        [
            ({"CSGO_MAX_WORKERS": "abc"}, "CSGO_MAX_WORKERS"),
            ({"CSGO_MAX_WORKERS": "0"}, "CSGO_MAX_WORKERS"),
            ({"CSGO_MAX_ITEMS": "99999"}, "CSGO_MAX_ITEMS"),
            ({"CSGO_CACHE_TTL": "-1"}, "CSGO_CACHE_TTL"),
            ({"CSGO_CACHE_TTL": "abc"}, "CSGO_CACHE_TTL"),
            ({"CSGO_WATCHLIST_PATH": "   "}, "CSGO_WATCHLIST_PATH"),
            ({"CSGO_HTTP_TIMEOUT": "1,2,3"}, "CSGO_HTTP_TIMEOUT"),
            ({"CSGO_HTTP_TIMEOUT": "0"}, "CSGO_HTTP_TIMEOUT"),
        ],
    )
    def test_invalid_values_raise_chinese_config_error(self, env: dict, name: str) -> None:
        with pytest.raises(ConfigError) as excinfo:
            Settings.from_env(env)
        assert name in str(excinfo.value)

    def test_timeout_formats(self) -> None:
        assert Settings.from_env({"CSGO_HTTP_TIMEOUT": "8"}).http_timeout == (8.0, 8.0)
        assert Settings.from_env({"CSGO_HTTP_TIMEOUT": "2,7"}).http_timeout == (2.0, 7.0)

    def test_cors_and_cookie_parsing(self) -> None:
        settings = Settings.from_env(
            {
                "CSGO_CORS_ORIGINS": " https://a.example , https://b.example ,,",
                "BUFF_COOKIE": " session=xyz ",
            }
        )
        assert settings.cors_origins == ("https://a.example", "https://b.example")
        assert settings.buff_cookie == "session=xyz"
        assert Settings.from_env({"BUFF_COOKIE": "  "}).buff_cookie is None
