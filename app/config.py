"""应用配置。

所有配置项都可以通过环境变量覆盖（12-factor），默认值保证开箱即用。
非法取值在启动时立即抛错，避免带着错误配置运行。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})
_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


class ConfigError(ValueError):
    """环境变量取值非法。"""


def _read_str(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _read_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"环境变量 {name}={raw!r} 不是合法整数") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"环境变量 {name}={value} 不能小于 {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"环境变量 {name}={value} 不能大于 {maximum}")
    return value


def _read_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"环境变量 {name}={raw!r} 不是合法数字") from None
    if not math.isfinite(value):
        raise ConfigError(f"环境变量 {name}={raw!r} 必须是有限数字（不允许 nan/inf）")
    if minimum is not None and value < minimum:
        raise ConfigError(f"环境变量 {name}={value} 不能小于 {minimum}")
    return value


def _read_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    raise ConfigError(
        f"环境变量 {name}={raw!r} 不是合法布尔值（支持 1/0/true/false/yes/no/on/off）"
    )


@dataclass(frozen=True, slots=True)
class Settings:
    """服务配置（不可变）。字段含义见 README「配置项」表。"""

    # HTTP 服务
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # 探测引擎
    probe_default_timeout_ms: int = 3000
    probe_max_timeout_ms: int = 10000
    probe_max_concurrency: int = 100

    # 任务治理
    task_max_targets: int = 200
    task_max_active: int = 100
    task_ttl_seconds: int = 3600
    task_max_retained: int = 1000

    # 限流
    rate_limit_enabled: bool = True
    rate_limit_rps: float = 5.0
    rate_limit_burst: int = 10
    rate_limit_idle_ttl_seconds: float = 600.0

    # 请求体大小上限（字节）：超过直接 413，避免超大 JSON 占用内存
    max_body_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        if self.log_level not in _LOG_LEVELS:
            raise ConfigError(
                f"LOG_LEVEL={self.log_level!r} 非法，可选：{', '.join(sorted(_LOG_LEVELS))}"
            )
        if self.probe_default_timeout_ms > self.probe_max_timeout_ms:
            raise ConfigError("PROBE_DEFAULT_TIMEOUT_MS 不能大于 PROBE_MAX_TIMEOUT_MS")
        if self.rate_limit_enabled and self.rate_limit_rps <= 0:
            raise ConfigError("限流开启时 RATE_LIMIT_RPS 必须大于 0")
        if not math.isfinite(self.rate_limit_rps) or self.rate_limit_rps < 0:
            raise ConfigError("RATE_LIMIT_RPS 必须是有限非负数（不允许 nan/inf）")
        # 过小的 rps 会让 retry_after 超出合理等待窗口（甚至溢出为 inf），直接拒绝；
        # 下限取 1/3600：保证限流响应中的 Retry-After 反映真实等待时间且不超过 3600 秒
        if self.rate_limit_enabled and self.rate_limit_rps < 1.0 / 3600.0:
            raise ConfigError("限流开启时 RATE_LIMIT_RPS 不得小于 1/3600（约 0.000278）")
        if (
            not math.isfinite(self.rate_limit_idle_ttl_seconds)
            or self.rate_limit_idle_ttl_seconds <= 0
        ):
            raise ConfigError("RATE_LIMIT_IDLE_TTL_SECONDS 必须是有限正数（不允许 nan/inf）")
        if self.max_body_bytes < 1:
            raise ConfigError("MAX_BODY_BYTES 必须为正整数")

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            host=_read_str("APP_HOST", "0.0.0.0"),
            port=_read_int("APP_PORT", 8000, minimum=1, maximum=65535),
            log_level=_read_str("LOG_LEVEL", "INFO").upper(),
            probe_default_timeout_ms=_read_int("PROBE_DEFAULT_TIMEOUT_MS", 3000, minimum=1),
            probe_max_timeout_ms=_read_int("PROBE_MAX_TIMEOUT_MS", 10000, minimum=1),
            probe_max_concurrency=_read_int("PROBE_MAX_CONCURRENCY", 100, minimum=1),
            task_max_targets=_read_int("TASK_MAX_TARGETS", 200, minimum=1),
            task_max_active=_read_int("TASK_MAX_ACTIVE", 100, minimum=1),
            task_ttl_seconds=_read_int("TASK_TTL_SECONDS", 3600, minimum=1),
            task_max_retained=_read_int("TASK_MAX_RETAINED", 1000, minimum=1),
            rate_limit_enabled=_read_bool("RATE_LIMIT_ENABLED", True),
            rate_limit_rps=_read_float("RATE_LIMIT_RPS", 5.0, minimum=0),
            rate_limit_burst=_read_int("RATE_LIMIT_BURST", 10, minimum=1),
            rate_limit_idle_ttl_seconds=_read_float(
                "RATE_LIMIT_IDLE_TTL_SECONDS", 600.0, minimum=1
            ),
            max_body_bytes=_read_int("MAX_BODY_BYTES", 1_048_576, minimum=1),
        )
