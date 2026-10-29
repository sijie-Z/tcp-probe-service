"""按 key 的令牌桶限流。

算法要点：
- 连续补充：每个桶只记录当前令牌数与上次结算时刻，任意时刻的令牌数为
  ``min(capacity, tokens + elapsed * rps)``，无需后台定时器；
- 独立桶：不同 key 互不影响，新 key 以满桶创建，允许 ``burst`` 次突发；
- 内存治理：key 可能来自不可信输入（如客户端 IP），字典不能无限增长。每 1024 次
  check 或桶数量超过 4096 时触发惰性清扫（两次清扫至少间隔 1 秒，避免桶数超阈值后
  每个请求都做全表扫描），回收 ``now - last_seen > idle_ttl_seconds`` 的空闲桶；
  桶数另有 10 万硬上限，超出时按字典顺序近似随机淘汰（O(n) 截断、无排序）保证内存有界。

并发前提：实例仅支持在单个事件循环（单线程）内由 HTTP 中间件同步调用；所有状态读写
都发生在同一线程，因此不引入锁。多线程或多进程共享同一实例不在支持范围内。
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

_SWEEP_CHECK_INTERVAL = 1024
_SWEEP_KEY_THRESHOLD = 4096
_SWEEP_MIN_INTERVAL_SECONDS = 1.0  # 两次自动清扫的最小间隔
_MAX_TRACKED_KEYS = 100_000  # 桶数量硬上限，超出按最久未活跃淘汰


class TokenBucket:
    """单个 key 的令牌桶，采用连续补充模型。

    参数:
        capacity: 桶容量（最大令牌数，即突发上限），必须 >= 1。
        refill_rate: 每秒补充的令牌数，必须 > 0。
        now: 创建时刻，与后续方法调用使用同一时钟。

    桶在创建时是满的。``try_acquire`` 先把距上次结算的时长折算成令牌（封顶到
    capacity），再尝试扣减；时钟回拨按零流逝处理，令牌数不会倒退。
    """

    __slots__ = ("_capacity", "_refill_rate", "_tokens", "_last_refill")

    def __init__(self, capacity: float, refill_rate: float, now: float) -> None:
        if capacity < 1:
            raise ValueError(f"capacity 必须大于等于 1，当前为 {capacity}")
        if refill_rate <= 0:
            raise ValueError(f"refill_rate 必须大于 0，当前为 {refill_rate}")
        self._capacity = float(capacity)
        self._refill_rate = float(refill_rate)
        self._tokens = float(capacity)
        self._last_refill = float(now)

    def try_acquire(self, now: float) -> bool:
        """尝试取走 1 个令牌；成功返回 True，令牌不足返回 False。"""
        self._refill(now)
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    def retry_after(self, now: float) -> float:
        """返回攒够 1 个令牌还需的秒数；令牌已够时返回 0.0。

        该方法只读，不改变桶状态，避免同一请求重复计算时产生副作用。
        """
        tokens = self._tokens_at(now)
        return max(0.0, (1.0 - tokens) / self._refill_rate)

    def _refill(self, now: float) -> None:
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
        self._last_refill = now

    def _tokens_at(self, now: float) -> float:
        elapsed = max(0.0, now - self._last_refill)
        return min(self._capacity, self._tokens + elapsed * self._refill_rate)


@dataclass(slots=True)
class _BucketEntry:
    """一个 key 的桶及其最近访问时刻（last_seen 用于空闲回收）。"""

    bucket: TokenBucket
    last_seen: float


class RateLimiter:
    """按 key 维护独立令牌桶的限流器。

    参数:
        rps: 每个 key 每秒补充的令牌数（稳态放行速率）。
        burst: 每个 key 的桶容量，即允许的突发请求数，必须 >= 1。
        idle_ttl_seconds: 桶的空闲存活时长；超过该时长未访问的 key 会被清扫。
        clock: 单调时钟，测试可注入；生产默认 ``time.monotonic``。

    单次 check 为 O(1)；每隔 1024 次 check 做一次 O(n) 清扫，摊还后仍为 O(1)。
    并发前提见模块 docstring：单事件循环内同步调用，无需加锁。
    """

    def __init__(
        self,
        rps: float,
        burst: int,
        idle_ttl_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rps <= 0 or not math.isfinite(rps):
            raise ValueError(f"rps 必须是有限正数，当前为 {rps}")
        if burst < 1:
            raise ValueError(f"burst 必须大于等于 1，当前为 {burst}")
        if idle_ttl_seconds <= 0 or not math.isfinite(idle_ttl_seconds):
            raise ValueError(f"idle_ttl_seconds 必须是有限正数，当前为 {idle_ttl_seconds}")

        self._rps = float(rps)
        self._burst = int(burst)
        self._idle_ttl = float(idle_ttl_seconds)
        self._clock = clock
        self._buckets: dict[str, _BucketEntry] = {}
        self._checks_since_sweep = 0
        self._last_sweep = self._clock()

    def check(self, key: str) -> tuple[bool, float]:
        """对 key 做一次限流判定，返回 (是否放行, 建议的重试等待秒数)。

        放行时等待秒数恒为 0.0。无论放行与否都会刷新 last_seen，
        保证仍在活跃使用的桶不会被清扫掉。
        """
        now = self._clock()
        entry = self._buckets.get(key)
        if entry is None:
            entry = _BucketEntry(TokenBucket(self._burst, self._rps, now), now)
            self._buckets[key] = entry
        else:
            entry.last_seen = now

        allowed = entry.bucket.try_acquire(now)
        retry_after = 0.0 if allowed else entry.bucket.retry_after(now)

        self._checks_since_sweep += 1
        due = (
            self._checks_since_sweep >= _SWEEP_CHECK_INTERVAL
            or len(self._buckets) > _SWEEP_KEY_THRESHOLD
        )
        # 冷却窗口：桶数超阈值后不能每个请求都做 O(n) 清扫
        if due and now - self._last_sweep >= _SWEEP_MIN_INTERVAL_SECONDS:
            self._sweep(now)
        return allowed, retry_after

    def sweep(self) -> int:
        """立即清扫空闲桶，返回回收的 key 数量。

        删除满足 ``now - last_seen > idle_ttl_seconds`` 的桶（严格大于）。
        check() 会按阈值自动调用；该方法也可由测试或运维手动触发。
        """
        return self._sweep(self._clock())

    @property
    def tracked_keys(self) -> int:
        """当前仍在跟踪的桶数量（测试与观测用）。"""
        return len(self._buckets)

    def _sweep(self, now: float) -> int:
        self._checks_since_sweep = 0
        self._last_sweep = now
        stale = [
            key
            for key, entry in self._buckets.items()
            if now - entry.last_seen > self._idle_ttl
        ]
        for key in stale:
            del self._buckets[key]
        if len(self._buckets) > _MAX_TRACKED_KEYS:
            # 硬上限：攻击者可无限轮换来源 IP，空闲清扫防不住全部增长。
            # 单次 O(n) 迭代按字典顺序截断（不做排序），只保证内存有界；
            # 精确 LRU 需要有序结构，代价更大，不在作业范围内。
            excess = len(self._buckets) - _MAX_TRACKED_KEYS
            for key in list(self._buckets.keys())[:excess]:
                del self._buckets[key]
        return len(stale)
