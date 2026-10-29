"""RateLimiter 内存治理边界：清扫冷却与硬上限。"""

from __future__ import annotations

import app.ratelimit as ratelimit_module
from app.ratelimit import RateLimiter


def test_sweep_respects_cooldown_after_key_threshold() -> None:
    now = [1000.0]
    limiter = RateLimiter(rps=10, burst=2, idle_ttl_seconds=600, clock=lambda: now[0])
    for index in range(4100):
        limiter.check(f"k{index}")

    sweep_calls = 0
    original_sweep = limiter._sweep

    def counting_sweep(ts: float) -> int:
        nonlocal sweep_calls
        sweep_calls += 1
        return original_sweep(ts)

    limiter._sweep = counting_sweep  # type: ignore[method-assign]

    for index in range(10):
        limiter.check(f"burst{index}")
    assert sweep_calls == 0, "1 秒冷却窗口内不应重复执行全表清扫"

    now[0] += 2.0  # 超过冷却窗口
    limiter.check("after-cooldown")
    assert sweep_calls == 1


def test_hard_cap_evicts_least_recently_seen(monkeypatch) -> None:
    # 同时下调“触发清扫”的 key 阈值，把两个常量都缩到测试规模
    monkeypatch.setattr(ratelimit_module, "_MAX_TRACKED_KEYS", 5)
    monkeypatch.setattr(ratelimit_module, "_SWEEP_KEY_THRESHOLD", 2)
    now = [1000.0]
    limiter = RateLimiter(rps=10, burst=2, idle_ttl_seconds=600, clock=lambda: now[0])
    for index in range(8):
        now[0] += 0.01
        limiter.check(f"k{index}")

    now[0] += 2.0  # 冷却到期，触发清扫；此时 ttl 未到，只能靠硬上限淘汰
    limiter.check("trigger")
    assert limiter.tracked_keys <= 5
