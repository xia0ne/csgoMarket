"""应用工厂：装配配置、生命周期、中间件、静态资源与路由。

测试可以用临时 ``Settings`` 创建互不干扰的 app 实例；
``app.main:app`` 则直接交给 uvicorn。
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import api
from app.buff import BuffClient
from app.config import Settings
from app.schemas import ErrorResponse
from app.service import WatchlistService
from app.watchlist import WatchlistStore

logger = logging.getLogger(__name__)

STATIC_DIR = api.STATIC_DIR


def configure_logging(level: int = logging.INFO) -> None:
    """幂等的基础日志配置；宿主程序已配置日志时不覆盖。"""
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建 FastAPI 应用；``settings`` 为空时从环境变量读取。"""
    configure_logging()
    resolved = settings or Settings.from_env()

    store = WatchlistStore(
        resolved.watchlist_path,
        max_items=resolved.max_items,
        legacy_path=resolved.legacy_path,
    )
    client = BuffClient(
        timeout=resolved.http_timeout,
        cache_ttl=resolved.cache_ttl,
        cookie=resolved.buff_cookie,
    )
    service = WatchlistService(store, client, max_workers=resolved.max_workers)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store.ensure_loaded()
        logger.info(
            "服务启动：关注列表 %s（%d 条），缓存 TTL %.0fs，并发 %d",
            resolved.watchlist_path,
            store.count,
            resolved.cache_ttl,
            resolved.max_workers,
        )
        try:
            yield
        finally:
            client.close()
            logger.info("服务停止")

    app = FastAPI(
        title="csgoMarket API",
        description=(
            "网易 BUFF 饰品价格监控。响应统一为 JSON，错误统一为 `{\"detail\": \"中文说明\"}`；"
            "旧版 `/buff/{id}`、`/submit/{id}`、`/get` 已移除。"
        ),
        version="2.0.0",
        lifespan=lifespan,
        responses={500: {"model": ErrorResponse}},
    )
    app.state.settings = resolved
    app.state.store = store
    app.state.client = client
    app.state.service = service

    if resolved.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.cors_origins),
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        took_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Response-Time-ms"] = f"{took_ms:.1f}"
        logger.info(
            "请求 method=%s path=%s status=%d took=%.1fms",
            request.method,
            request.url.path,
            response.status_code,
            took_ms,
        )
        return response

    api.register_error_handlers(app)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(api.router)
    return app
