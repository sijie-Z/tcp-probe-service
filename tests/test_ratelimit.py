"""app.ratelimit 的单元测试。

统一通过闭包变量注入可推进的 ``now``，不依赖真实时间，也不使用 sleep。
涉及小数秒的断言一律使用 ``pytest.approx`` 以容忍浮点误差。
"""

from __future__ import annotations

import pytest

from app.ratelimit import RateLimiter, TokenBucket


def test_burst_allows_capacity_then_rejects() -> None:
    """新 key 以满桶创建：burst 次突发全部放行，下一次拒绝。"""
    limiter = RateLimiter(rps=1.0, burst=3, clock=lambda: 0.0)

    decisions = [limiter.check("client-a")[0] for _ in range(4)]

    assert decisions == [True, True, True, False]


def test_refills_one_token_after_one_over_rps_seconds() -> None:
    """空桶经过 1/rps 秒恰好补充 1 个令牌，可再放行一次。"""
    now = 0.0
    limiter = RateLimiter(rps=2.0, burst=1, clock=lambda: now)

    assert limiter.check("client-a")[0]
    assert not limiter.check("client-a")[0]

    now = 0.5  # 1 / rps
    assert limiter.check("client-a")[0]


def test_retry_after_counts_down_until_next_token() -> None:
    """retry_after 从 1/rps 递减；攒够令牌后放行且等待为 0.0。"""
    now = 0.0
    limiter = RateLimiter(rps=4.0, burst=1, clock=lambda: now)

    assert limiter.check("client-a")[0]

    allowed, retry_after = limiter.check("client-a")
    assert not allowed
    assert retry_after == pytest.approx(1.0 / 4.0)

    now = 0.125  # 半个令牌间隔：已补充半个令牌
    allowed, retry_after = limiter.check("client-a")
    assert not allowed
    assert retry_after == pytest.approx(0.125)

    now = 0.25  # 恰好攒够 1 个令牌
    allowed, retry_after = limiter.check("client-a")
    assert allowed
    assert retry_after == 0.0


def test_token_bucket_retry_after_for_empty_bucket() -> None:
    """空桶的 retry_after 约等于 1/rps（直接验证 TokenBucket 契约）。"""
    bucket = TokenBucket(capacity=1.0, refill_rate=5.0, now=0.0)
    assert bucket.retry_after(0.0) == 0.0  # 满桶无需等待

    assert bucket.try_acquire(0.0)
    assert not bucket.try_acquire(0.0)
    assert bucket.retry_after(0.0) == pytest.approx(1.0 / 5.0)


def test_idle_bucket_refills_only_up_to_burst() -> None:
    """长时间空闲后令牌最多补满到 capacity，不会积累超额突发。"""
    now = 0.0
    limiter = RateLimiter(rps=10.0, burst=2, clock=lambda: now)

    assert limiter.check("client-a")[0]
    assert limiter.check("client-a")[0]

    now = 60.0  # 足以补充 600 个令牌，但桶容量只有 2
    decisions = [limiter.check("client-a")[0] for _ in range(3)]

    assert decisions == [True, True, False]


def test_keys_are_isolated() -> None:
    """不同 key 各自持有独立桶，互不消耗对方额度。"""
    limiter = RateLimiter(rps=1.0, burst=1, clock=lambda: 0.0)

    assert limiter.check("client-a")[0]
    assert not limiter.check("client-a")[0]
    assert limiter.check("client-b")[0]
    assert limiter.tracked_keys == 2


def test_sweep_removes_idle_keys_only() -> None:
    """sweep 只回收超过 idle_ttl 的 key，活跃 key 的桶状态原样保留。"""
    now = 0.0
    limiter = RateLimiter(rps=0.1, burst=1, idle_ttl_seconds=10.0, clock=lambda: now)

    assert limiter.check("idle")[0]
    now = 5.0
    assert limiter.check("active")[0]

    now = 11.0  # idle 距上次访问 11 秒 > 10；active 仅 6 秒
    assert limiter.sweep() == 1
    assert limiter.tracked_keys == 1

    # active 的桶未被回收：0.1 rps 下 6 秒只补 0.6 个令牌，仍不足以放行
    allowed, retry_after = limiter.check("active")
    assert not allowed
    assert retry_after == pytest.approx(4.0)

    # idle 的桶被回收后按新 key 重建，重新获得满桶
    assert limiter.check("idle")[0]


def test_check_triggers_lazy_sweep_every_1024_calls() -> None:
    """累计 1024 次 check 会自动触发清扫，无需显式调用 sweep。"""
    now = 0.0
    limiter = RateLimiter(rps=1.0, burst=4096, idle_ttl_seconds=10.0, clock=lambda: now)
    limiter.check("stale")

    now = 100.0
    for _ in range(1022):
        limiter.check("active")
    assert limiter.tracked_keys == 2  # 累计 1023 次 check，尚未触发清扫

    limiter.check("active")  # 第 1024 次 check 触发惰性清扫
    assert limiter.tracked_keys == 1


@pytest.mark.parametrize(
    ("rps", "burst", "idle_ttl"),
    [
        (0.0, 1, 600.0),
        (-1.0, 2, 600.0),
        (1.0, 0, 600.0),
        (1.0, 1, 0.0),
        (1.0, 1, -1.0),
    ],
)
def test_invalid_constructor_arguments_raise_value_error(
    rps: float, burst: int, idle_ttl: float
) -> None:
    """rps <= 0、burst < 1、idle_ttl_seconds <= 0 均应在构造时拒绝。"""
    with pytest.raises(ValueError, match="必须"):
        RateLimiter(rps=rps, burst=burst, idle_ttl_seconds=idle_ttl)
