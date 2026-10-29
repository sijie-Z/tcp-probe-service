"""配置解析与校验边界测试。"""

from __future__ import annotations

import pytest

from app.config import ConfigError, Settings


def test_from_env_rejects_nan_rps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_RPS", "nan")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_from_env_rejects_inf_idle_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_IDLE_TTL_SECONDS", "inf")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_from_env_rejects_invalid_bool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "maybe")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_direct_settings_rejects_non_finite_rps() -> None:
    with pytest.raises(ConfigError):
        Settings(rate_limit_rps=float("nan"))
    with pytest.raises(ConfigError):
        Settings(rate_limit_rps=float("inf"))


def test_direct_settings_rejects_too_small_rps_when_enabled() -> None:
    # 极小 rps 会让 Retry-After 超出合理等待窗口（甚至溢出为 inf），应当拒绝；
    # 下限 1/3600（约 0.000278）保证限流头反映真实等待时间
    with pytest.raises(ConfigError):
        Settings(rate_limit_rps=1e-9)
    with pytest.raises(ConfigError):
        Settings(rate_limit_rps=1e-4)
    assert Settings(rate_limit_rps=3e-4).rate_limit_rps == 3e-4


def test_zero_rps_allowed_only_when_disabled() -> None:
    settings = Settings(rate_limit_enabled=False, rate_limit_rps=0)
    assert settings.rate_limit_rps == 0
