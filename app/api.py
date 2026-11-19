"""HTTP 路由：商品行情、关注列表、健康检查与监控页。"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.buff import (
    BuffClient,
    BuffRateLimited,
    BuffTimeout,
    BuffUpstreamError,
    GoodsNotFound,
    GoodsSnapshot,
)
from app.config import ConfigError, Settings
from app.schemas import (
    AddOut,
    AddRequest,
    ErrorResponse,
    GoodsIdError,
    GoodsOut,
    HealthOut,
    WatchlistOut,
    normalize_goods_id,
)
from app.service import WatchlistService
from app.watchlist import DuplicateGoods, WatchlistFull, WatchlistWriteError

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

BUFF_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse, "description": "商品不存在"},
    422: {"model": ErrorResponse, "description": "商品 ID 格式非法"},
    502: {"model": ErrorResponse, "description": "上游数据异常"},
    503: {"model": ErrorResponse, "description": "BUFF 限流"},
    504: {"model": ErrorResponse, "description": "访问 BUFF 超时"},
}


# ---------------------------------------------------------------------------
# 依赖注入
# ---------------------------------------------------------------------------
def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_client(request: Request) -> BuffClient:
    return request.app.state.client


def get_service(request: Request) -> WatchlistService:
    return request.app.state.service


SettingsDep = Annotated[Settings, Depends(get_settings)]
ClientDep = Annotated[BuffClient, Depends(get_client)]
ServiceDep = Annotated[WatchlistService, Depends(get_service)]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def parse_goods_id(raw: str) -> str:
    try:
        return normalize_goods_id(raw)
    except GoodsIdError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def goods_out(snapshot: GoodsSnapshot) -> GoodsOut:
    return GoodsOut(
        goods_id=snapshot.goods_id,
        name=snapshot.name,
        steam_price_cny=snapshot.steam_price_cny,
        buff_min_price=snapshot.buff_min_price,
        sell_count=snapshot.sell_count,
        top_prices=list(snapshot.top_prices),
        icon_url=snapshot.icon_url,
        fetched_at=snapshot.fetched_at,
        cached=snapshot.cached,
    )


# ---------------------------------------------------------------------------
# 商品行情
# ---------------------------------------------------------------------------
@router.get(
    "/api/v1/goods/{goods_id:path}",
    response_model=GoodsOut,
    summary="获取单个饰品行情",
    responses=BUFF_ERROR_RESPONSES,
)
def get_goods(goods_id: str, service: ServiceDep) -> GoodsOut:
    gid = parse_goods_id(goods_id)
    started = time.perf_counter()
    snapshot = service.goods(gid)
    took_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "商品行情 path=%s goods_id=%s cached=%s took=%.1fms",
        f"/api/v1/goods/{gid}",
        gid,
        snapshot.cached,
        took_ms,
    )
    return goods_out(snapshot)


@router.get("/api/v1/goods/", include_in_schema=False)
def get_goods_without_id() -> None:
    """空 ID 走 422，而不是让路由落到 404。"""
    raise HTTPException(status_code=422, detail="商品 ID 必须是 1–12 位纯数字。")


# ---------------------------------------------------------------------------
# 关注列表
# ---------------------------------------------------------------------------
@router.get(
    "/api/v1/watchlist",
    response_model=WatchlistOut,
    summary="获取关注列表行情",
    responses=BUFF_ERROR_RESPONSES,
)
def get_watchlist(
    request: Request,
    service: ServiceDep,
    refresh: Annotated[bool, Query(description="为 true 时跳过缓存")] = False,
) -> WatchlistOut:
    result = service.watchlist(refresh=refresh)
    logger.info(
        "关注列表 path=%s count=%d ok=%d failed=%d refresh=%s took=%.1fms",
        request.url.path,
        result.count,
        len(result.items),
        len(result.failed),
        refresh,
        result.took_ms,
    )
    return WatchlistOut(
        count=result.count,
        items=[goods_out(item) for item in result.items],
        failed=[{"goods_id": item.goods_id, "reason": item.reason} for item in result.failed],
        took_ms=result.took_ms,
        fetched_at=result.fetched_at,
    )


@router.post(
    "/api/v1/watchlist",
    response_model=AddOut,
    status_code=201,
    summary="新增关注",
    responses={
        201: {"model": AddOut},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
        504: {"model": ErrorResponse},
    },
)
def add_watchlist(payload: AddRequest, settings: SettingsDep, service: ServiceDep) -> AddOut:
    gid = payload.goods_id
    store = service.store

    # 重复项和上限在访问 BUFF 之前就能判定，避免无谓的上游请求。
    if store.contains(gid):
        raise HTTPException(status_code=409, detail="该商品已在关注列表中")
    if store.count >= settings.max_items:
        raise HTTPException(status_code=409, detail=f"关注列表已满（上限 {settings.max_items}）")

    # 先确认商品真实存在，避免把无效 ID 写进关注列表。
    service.goods(gid)

    try:
        entry = store.add(gid)
    except DuplicateGoods as exc:
        raise HTTPException(status_code=409, detail="该商品已在关注列表中") from exc
    except WatchlistFull as exc:
        detail = f"关注列表已满（上限 {settings.max_items}）"
        raise HTTPException(status_code=409, detail=detail) from exc

    logger.info("关注列表新增成功 goods_id=%s added_at=%s", entry.goods_id, entry.added_at)
    return AddOut(goods_id=entry.goods_id, added_at=entry.added_at)


@router.delete(
    "/api/v1/watchlist/{goods_id}",
    status_code=204,
    summary="取消关注",
    responses={204: {"description": "删除成功"}, 404: {"model": ErrorResponse}},
)
def delete_watchlist(goods_id: str, service: ServiceDep) -> Response:
    gid = parse_goods_id(goods_id)
    store = service.store
    if not store.remove(gid):
        raise HTTPException(status_code=404, detail="该商品不在关注列表中")
    logger.info("关注列表删除成功 goods_id=%s", gid)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# 运维与页面
# ---------------------------------------------------------------------------
@router.get("/healthz", response_model=HealthOut, summary="健康检查")
def healthz(request: Request, settings: SettingsDep, client: ClientDep) -> HealthOut:
    return HealthOut(
        status="ok",
        watchlist_count=request.app.state.store.count,
        cache_entries=client.cache_entries,
    )


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request, settings: SettingsDep):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"max_items": settings.max_items},
    )


# ---------------------------------------------------------------------------
# 错误处理
# ---------------------------------------------------------------------------
BUFF_ERRORS: tuple[tuple[type[Exception], int, str], ...] = (
    (GoodsNotFound, 404, "未找到该商品"),
    (BuffTimeout, 504, "访问 BUFF 超时，请稍后重试"),
    (BuffRateLimited, 503, "BUFF 暂时限制访问，请稍后重试"),
    (BuffUpstreamError, 502, "BUFF 返回数据异常"),
)


def _validation_message(exc: RequestValidationError) -> str:
    for error in exc.errors():
        location = {str(part) for part in error.get("loc", ())}
        if "goods_id" in location:
            return "商品 ID 必须是 1–12 位纯数字。"
    return "请求参数不合法。"


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": _validation_message(exc)})

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail
        if detail == "Not Found":
            detail = "未找到该资源"
        elif detail == "Method Not Allowed":
            detail = "不支持该请求方法"
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": detail},
            headers=getattr(exc, "headers", None),
        )

    for exc_type, status_code, message in BUFF_ERRORS:

        def make_handler(
            exc_type: type[Exception], status_code: int, message: str
        ):
            async def handle_buff_error(request: Request, exc: Exception) -> JSONResponse:
                logger.warning(
                    "BUFF 请求失败 path=%s status=%d: %s", request.url.path, status_code, exc
                )
                return JSONResponse(status_code=status_code, content={"detail": message})

            return handle_buff_error

        app.add_exception_handler(exc_type, make_handler(exc_type, status_code, message))

    @app.exception_handler(ConfigError)
    async def handle_config_error(request: Request, exc: ConfigError) -> JSONResponse:
        logger.error("配置错误：%s", exc)
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    @app.exception_handler(WatchlistWriteError)
    async def handle_write_error(request: Request, exc: WatchlistWriteError) -> JSONResponse:
        logger.error("关注列表写入失败：%s", exc)
        return JSONResponse(status_code=500, content={"detail": "关注列表保存失败，请稍后重试"})

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未捕获异常 path=%s: %s", request.url.path, exc)
        return JSONResponse(status_code=500, content={"detail": "服务器内部错误，请稍后重试。"})
