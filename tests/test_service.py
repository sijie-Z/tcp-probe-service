"""ProbeService 任务编排单元测试。

探测函数打桩，聚焦编排语义：结果顺序、取消补齐、背压、参数校验与关停。
"""

from __future__ import annotations

import asyncio

import pytest

import app.service as service_module
from app.config import Settings
from app.models import (
    CreateTaskRequest,
    ProbeTarget,
    TargetResult,
    TargetStatus,
    TaskStatus,
)
from app.service import ProbeRequestError, ProbeService, TaskCapacityError
from app.store import TaskStore


def _settings(**overrides) -> Settings:
    return Settings(rate_limit_enabled=False, **overrides)


def _service(**overrides) -> ProbeService:
    settings = _settings(**overrides)
    return ProbeService(settings, TaskStore(settings))


def _target(host: str, port: int = 80, timeout_ms: int | None = None) -> ProbeTarget:
    return ProbeTarget(host=host, port=port, timeout_ms=timeout_ms)


def _request(*targets: ProbeTarget) -> CreateTaskRequest:
    return CreateTaskRequest(name="test", targets=list(targets))


def _run(coro):
    return asyncio.run(coro)


def test_batch_runs_and_preserves_submission_order(monkeypatch) -> None:
    async def fake_probe(host: str, port: int, timeout_ms: int) -> TargetResult:
        await asyncio.sleep(0.05 if host == "slow" else 0.01)
        return TargetResult(host=host, port=port, status=TargetStatus.OPEN, latency_ms=1.0)

    monkeypatch.setattr(service_module, "probe_tcp", fake_probe)

    async def scenario():
        service = _service()
        task = service.submit(_request(_target("slow"), _target("fast")))
        return await service.wait_for(task.id, 2000)

    task = _run(scenario())
    assert task.status is TaskStatus.SUCCEEDED
    assert [result.host for result in task.results] == ["slow", "fast"]
    assert task.completed == 2


def test_cancel_marks_unfinished_targets_cancelled(monkeypatch) -> None:
    async def slow_probe(host: str, port: int, timeout_ms: int) -> TargetResult:
        await asyncio.sleep(30)
        return TargetResult(host=host, port=port, status=TargetStatus.OPEN)

    monkeypatch.setattr(service_module, "probe_tcp", slow_probe)

    async def scenario():
        service = _service()
        task = service.submit(_request(_target("a"), _target("b")))
        await asyncio.sleep(0.05)  # 让任务进入 running、两个探测都已挂起
        return await service.cancel(task.id)

    task = _run(scenario())
    assert task.status is TaskStatus.CANCELLED
    assert task.completed == 2
    assert [result.status for result in task.results] == [
        TargetStatus.CANCELLED,
        TargetStatus.CANCELLED,
    ]


def test_cancel_before_task_starts_finalizes_terminal_state(monkeypatch) -> None:
    """协程获得调度前即被取消（_run 的 finally 不会执行）时，兜底逻辑仍须收敛终态。"""

    async def slow_probe(host: str, port: int, timeout_ms: int) -> TargetResult:
        await asyncio.sleep(30)
        return TargetResult(host=host, port=port, status=TargetStatus.OPEN)

    monkeypatch.setattr(service_module, "probe_tcp", slow_probe)

    async def scenario():
        service = _service()
        task = service.submit(_request(_target("a"), _target("b")))
        # 提交之后立即取消、中间没有任何 await：协调协程尚未获得调度
        return await service.cancel(task.id)

    task = _run(scenario())
    assert task.status is TaskStatus.CANCELLED
    assert task.finished_at is not None
    assert task.completed == 2
    assert [result.status for result in task.results] == [
        TargetStatus.CANCELLED,
        TargetStatus.CANCELLED,
    ]


def test_cancel_is_idempotent(monkeypatch) -> None:
    """重复取消已取消的任务应直接返回当前快照，而不是 409。"""

    async def slow_probe(host: str, port: int, timeout_ms: int) -> TargetResult:
        await asyncio.sleep(30)
        return TargetResult(host=host, port=port, status=TargetStatus.OPEN)

    monkeypatch.setattr(service_module, "probe_tcp", slow_probe)

    async def scenario():
        service = _service()
        task = service.submit(_request(_target("a")))
        await asyncio.sleep(0.02)
        first = await service.cancel(task.id)
        second = await service.cancel(task.id)
        return first, second

    first, second = _run(scenario())
    assert first.status is TaskStatus.CANCELLED
    assert second.status is TaskStatus.CANCELLED
    assert second.completed == second.total


def test_wait_for_returns_running_snapshot_on_timeout(monkeypatch) -> None:
    async def slow_probe(host: str, port: int, timeout_ms: int) -> TargetResult:
        await asyncio.sleep(30)
        return TargetResult(host=host, port=port, status=TargetStatus.OPEN)

    monkeypatch.setattr(service_module, "probe_tcp", slow_probe)

    async def scenario():
        service = _service()
        task = service.submit(_request(_target("a")))
        snapshot = await service.wait_for(task.id, 50)
        status_at_timeout = snapshot.status
        await service.shutdown()
        return status_at_timeout, snapshot

    status_at_timeout, snapshot = _run(scenario())
    assert status_at_timeout is TaskStatus.RUNNING
    # wait_for 超时返回的是活对象；随后的 shutdown 会让同一任务收敛为 cancelled
    assert snapshot.status is TaskStatus.CANCELLED


def test_active_task_capacity_backpressure(monkeypatch) -> None:
    async def slow_probe(host: str, port: int, timeout_ms: int) -> TargetResult:
        await asyncio.sleep(30)
        return TargetResult(host=host, port=port, status=TargetStatus.OPEN)

    monkeypatch.setattr(service_module, "probe_tcp", slow_probe)

    async def scenario():
        service = _service(task_max_active=1)
        service.submit(_request(_target("a")))
        await asyncio.sleep(0.01)
        with pytest.raises(TaskCapacityError):
            service.submit(_request(_target("b")))
        await service.shutdown()

    _run(scenario())


def test_reject_too_many_targets() -> None:
    async def scenario():
        service = _service(task_max_targets=2)
        with pytest.raises(ProbeRequestError) as exc_info:
            service.submit(_request(_target("a"), _target("b"), _target("c")))
        return exc_info.value

    error = _run(scenario())
    assert error.code == "TARGETS_TOO_MANY"


def test_reject_timeout_above_max() -> None:
    async def scenario():
        service = _service(probe_default_timeout_ms=500, probe_max_timeout_ms=1000)
        with pytest.raises(ProbeRequestError) as exc_info:
            service.submit(_request(_target("a", timeout_ms=5000)))
        return exc_info.value

    error = _run(scenario())
    assert error.code == "TIMEOUT_TOO_LARGE"


def test_shutdown_cancels_running_tasks(monkeypatch) -> None:
    async def slow_probe(host: str, port: int, timeout_ms: int) -> TargetResult:
        await asyncio.sleep(30)
        return TargetResult(host=host, port=port, status=TargetStatus.OPEN)

    monkeypatch.setattr(service_module, "probe_tcp", slow_probe)

    async def scenario():
        service = _service()
        task = service.submit(_request(_target("a")))
        await asyncio.sleep(0.05)
        await service.shutdown()
        return task

    task = _run(scenario())
    assert task.status is TaskStatus.CANCELLED
    assert task.finished_at is not None
