"""运行期配置：全部来自环境变量，相对路径统一相对项目根目录解析。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# 项目根目录（app/ 的上一级）。相对路径一律相对这里解析，
# 因此无论从哪个工作目录启动 uvicorn，指向的都是同一份文件。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_WATCHLIST_PATH = "data/watchlist.json"
DEFAULT_CACHE_TTL = 30.0
DEFAULT_MAX_WORKERS = 6
DEFAULT_MAX_ITEMS = 200
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 10.0

# 旧版把关注列表写在 script/submit.buff，首次启动时据此自动迁移。
LEGACY_WATCHLIST_PATH = PROJECT_ROOT / "script" / "submit.buff"


class ConfigError(RuntimeError):
    """环境变量取值非法。"""


@dataclass(frozen=True, slots=True)
class Settings:
    """不可变的运行期配置。

    ``legacy_path`` 默认是 ``None``：只有从环境变量构建配置时才会指向
    ``script/submit.buff`` 做一次性迁移。这样测试可以放心使用临时目录，
    不会意外把仓库里的示例数据搬进测试用的关注列表。
    """

    watchlist_path: Path
    cache_ttl: float = DEFAULT_CACHE_TTL
    http_timeout: tuple[float, float] = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT)
    max_workers: int = DEFAULT_MAX_WORKERS
    cors_origins: tuple[str, ...] = ()
    buff_cookie: str | None = None
    max_items: int = DEFAULT_MAX_ITEMS
    legacy_path: Path | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """从环境变量构建配置；非法值抛出带中文说明的 ConfigError。"""
        source = os.environ if env is None else env

        watchlist_path = resolve_path(
            source.get("CSGO_WATCHLIST_PATH", DEFAULT_WATCHLIST_PATH), "CSGO_WATCHLIST_PATH"
        )
        cache_ttl = _env_float(
            "CSGO_CACHE_TTL", DEFAULT_CACHE_TTL, minimum=0.0, maximum=3600.0, env=source
        )
        http_timeout = _env_timeout(source)
        max_workers = _env_int(
            "CSGO_MAX_WORKERS", DEFAULT_MAX_WORKERS, minimum=1, maximum=32, env=source
        )
        max_items = _env_int(
            "CSGO_MAX_ITEMS", DEFAULT_MAX_ITEMS, minimum=1, maximum=2000, env=source
        )

        raw_origins = source.get("CSGO_CORS_ORIGINS", "")
        origins = tuple(item.strip() for item in raw_origins.split(",") if item.strip())

        raw_cookie = source.get("BUFF_COOKIE", "").strip()

        return cls(
            watchlist_path=watchlist_path,
            cache_ttl=cache_ttl,
            http_timeout=http_timeout,
            max_workers=max_workers,
            cors_origins=origins,
            buff_cookie=raw_cookie or None,
            max_items=max_items,
            legacy_path=LEGACY_WATCHLIST_PATH,
        )


def resolve_path(raw: str, name: str) -> Path:
    """把配置里的相对路径解析成基于项目根目录的绝对路径。"""
    value = raw.strip()
    if not value:
        raise ConfigError(f"环境变量 {name} 不能为空。")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
    env: Mapping[str, str],
) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是整数，当前值为 {raw!r}。") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"环境变量 {name} 需在 {minimum}–{maximum} 之间，当前值为 {value}。")
    return value


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
    env: Mapping[str, str],
) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是数字，当前值为 {raw!r}。") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"环境变量 {name} 需在 {minimum}–{maximum} 之间，当前值为 {value}。")
    return value


def _env_timeout(env: Mapping[str, str]) -> tuple[float, float]:
    """解析 CSGO_HTTP_TIMEOUT。

    支持 ``10``（连接与读取都用 10 秒）或 ``5,10``（连接 5 秒、读取 10 秒）。
    """
    raw = env.get("CSGO_HTTP_TIMEOUT")
    if raw is None or raw.strip() == "":
        return (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT)

    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if len(parts) not in (1, 2):
        raise ConfigError(
            f"环境变量 CSGO_HTTP_TIMEOUT 应为单个数字或“连接,读取”，当前值为 {raw!r}。"
        )
    values: list[float] = []
    for part in parts:
        try:
            value = float(part)
        except ValueError as exc:
            raise ConfigError(
                f"环境变量 CSGO_HTTP_TIMEOUT 必须是数字，当前值为 {part!r}。"
            ) from exc
        if not 0 < value <= 120:
            raise ConfigError(
                f"环境变量 CSGO_HTTP_TIMEOUT 的每一项需在 0–120 秒之间，当前值为 {value}。"
            )
        values.append(value)
    if len(values) == 1:
        return (values[0], values[0])
    return (values[0], values[1])
