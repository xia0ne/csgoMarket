"""接口契约：pydantic v2 模型与商品 ID、时间的公共校验函数。"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

GOODS_ID_PATTERN = re.compile(r"^\d{1,12}$")
MAX_GOODS_ID_LENGTH = 12


class GoodsIdError(ValueError):
    """商品 ID 格式非法。"""


def normalize_goods_id(raw: object) -> str:
    """把商品 ID 规范成 1–12 位纯数字字符串。

    同时拒绝布尔值、浮点数、带空白或其他字符的输入，避免 ``../`` 之类的
    内容被拼进 URL 或关注列表文件。
    """
    if isinstance(raw, bool):
        raise GoodsIdError("商品 ID 必须是 1–12 位纯数字。")
    if isinstance(raw, int):
        value = str(raw)
    elif isinstance(raw, str):
        value = raw
    else:
        raise GoodsIdError("商品 ID 必须是 1–12 位纯数字。")

    if not GOODS_ID_PATTERN.fullmatch(value):
        raise GoodsIdError("商品 ID 必须是 1–12 位纯数字。")
    return value


def utc_now_iso() -> str:
    """当前 UTC 时间的 ISO 8601 表示，精确到秒，例如 2026-11-18T09:30:00Z。"""
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


class GoodsOut(BaseModel):
    """单个饰品的最新行情。"""

    model_config = ConfigDict(extra="forbid")

    goods_id: str = Field(description="BUFF 商品 ID")
    name: str = Field(description="饰品名称")
    steam_price_cny: float | None = Field(description="Steam 参考价（人民币），缺失时为 null")
    buff_min_price: float | None = Field(description="BUFF 最低在售价，缺失时为 null")
    sell_count: int = Field(description="BUFF 在售数量")
    top_prices: list[float] = Field(description="前五个卖单价格，不足五个时按实际数量返回")
    icon_url: str = Field(description="饰品图标地址")
    fetched_at: str = Field(description="行情获取时间（UTC，ISO 8601）")
    cached: bool = Field(description="是否命中进程内缓存")


class FailedItem(BaseModel):
    """关注列表中拉取失败的一项。"""

    goods_id: str
    reason: str


class WatchlistOut(BaseModel):
    """关注列表行情。"""

    count: int = Field(description="请求涉及的商品总数（成功 + 失败）")
    items: list[GoodsOut]
    failed: list[FailedItem]
    took_ms: float = Field(description="服务端并发拉取耗时（毫秒）")
    fetched_at: str = Field(description="本次拉取的完成时间（UTC，ISO 8601）")


class AddRequest(BaseModel):
    """新增关注请求体，``goods_id`` 同时接受字符串和 JSON 数字。"""

    model_config = ConfigDict(extra="forbid")

    goods_id: Annotated[str, BeforeValidator(normalize_goods_id)] = Field(
        description="BUFF 商品 ID，1–12 位数字"
    )


class AddOut(BaseModel):
    goods_id: str
    added_at: str = Field(description="加入关注列表的时间（UTC，ISO 8601）")


class HealthOut(BaseModel):
    status: str
    watchlist_count: int
    cache_entries: int


class ErrorResponse(BaseModel):
    detail: str
