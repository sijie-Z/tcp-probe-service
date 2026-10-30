"""探测任务编排。

设计要点：
- 提交即返回：任务创建后立即返回（202），后台协程执行，调用方轮询或用 wait=true 长轮询；
- 全局并发上限：所有任务共享一个信号量，限制同时在途的 TCP 连接数，防止文件描述符耗尽；
- 结果有序：结果列表与提交顺序一一对应，未完成的槽位为 null，完成后原地填充；
- 取消语义：取消会中断协调协程与在途探测，未完成的目标以 cancelled 状态补齐，已出的结果保留；
- 背压：活动任务数达到上限时直接拒绝（503），而不是让队列无限增长。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import datetime, timezone

from app.config import Settings
from app.metrics import Metrics
from app.models import (
    CreateTaskRequest,
    ProbeTarget,
    TargetResult,
    TargetStatus,
    Task,
    TaskStatus,
)
from app.prober import probe_tcp
from app.store import TaskStore

logger = logging.getLogger(__name__)


class TaskNotFoundError(Exception):
    """任务不存在或已过期（TTL 回收）。"""


class TaskAlreadyFinishedError(Exception):
    """任务已处于终态，无法取消。"""


class TaskCapacityError(Exception):
    """活动任务数达到上限（背压）。"""


class ProbeRequestError(Exception):
    """业务校验失败（如目标数超限、timeout 超上限）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProbeService:
    def __init__(
        self, settings: Settings, store: TaskStore, metrics: Metrics | None = None
    ) -> None:
        self._settings = settings
        self._store = store
        self._metrics = metrics
        self._recorded: set[str] = set()
        self._semaphore = asyncio.Semaphore(settings.probe_max_concurrency)
        self._running: dict[str, asyncio.Task[None]] = {}
        self._done: dict[str, asyncio.Event] = {}
        self._targets: dict[str, list[ProbeTarget]] = {}

    def submit(self, request: CreateTaskRequest) -> Task:
        """校验并创建任务，立即返回；执行在后台协程中进行。"""
        targets = request.targets
        if len(targets) > self._settings.task_max_targets:
            if self._metrics is not None:
                self._metrics.inc("tcp_probe_tasks_rejected_total", reason="invalid")
            raise ProbeRequestError(
                "TARGETS_TOO_MANY",
                f"单任务最多 {self._settings.task_max_targets} 个目标，收到 {len(targets)} 个",
            )
        for target in targets:
            if (
                target.timeout_ms is not None
                and target.timeout_ms > self._settings.probe_max_timeout_ms
            ):
                if self._metrics is not None:
                    self._metrics.inc("tcp_probe_tasks_rejected_total", reason="invalid")
                raise ProbeRequestError(
                    "TIMEOUT_TOO_LARGE",
                    f"timeout_ms 不能超过 {self._settings.probe_max_timeout_ms}"
                    f"（host={target.host}:{target.port}）",
                )
        if self._store.count_active() >= self._settings.task_max_active:
            if self._metrics is not None:
                self._metrics.inc("tcp_probe_tasks_rejected_total", reason="capacity")
            raise TaskCapacityError(
                f"活动任务数已达上限 {self._settings.task_max_active}，请稍后重试"
            )

        task = Task(
            id=uuid.uuid4().hex,
            name=request.name,
            total=len(targets),
            results=[None] * len(targets),
        )
        self._store.add(task)
        if self._metrics is not None:
            self._metrics.inc("tcp_probe_tasks_submitted_total")
        self._done[task.id] = asyncio.Event()
        self._targets[task.id] = targets
        self._running[task.id] = asyncio.create_task(
            self._run(task, targets), name=f"probe-task-{task.id}"
        )
        return task

    async def wait_for(self, task_id: str, timeout_ms: int) -> Task:
        """等待任务终态，最多 timeout_ms 毫秒；超时返回当前快照。"""
        task = self._store.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        event = self._done.get(task_id)
        if event is not None and not task.is_terminal:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(event.wait(), timeout_ms / 1000)
        return task

    async def cancel(self, task_id: str) -> Task:
        task = self._store.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.status is TaskStatus.CANCELLED:
            return task  # 幂等：重复取消已取消的任务直接返回当前快照
        if task.is_terminal:
            raise TaskAlreadyFinishedError(task_id)
        running = self._running.get(task_id)
        if running is not None:
            running.cancel()
            try:
                await running
            except asyncio.CancelledError:
                current = asyncio.current_task()
                # 区分「子任务被取消」与「调用方自己也被取消」（关停场景）：
                # 后者必须继续向上传播，不能吞掉。
                if current is not None and getattr(current, "cancelling", lambda: 0)() > 0:
                    raise
        self._finalize_cancelled(task)
        self._record_terminal(task)
        return task

    async def shutdown(self) -> None:
        """进程关停：取消所有在途任务并等待清理完成。"""
        running = dict(self._running)
        for coro_task in running.values():
            coro_task.cancel()
        if running:
            await asyncio.gather(*running.values(), return_exceptions=True)
        for task_id in running:
            task = self._store.get(task_id)
            if task is not None:
                self._finalize_cancelled(task)

    def _finalize_cancelled(self, task: Task) -> None:
        """兜底收敛为 cancelled。

        协程可能在获得调度前就被取消（协程体从未执行），此时 _run 的 finally
        不会运行；由这里补齐终态、结果槽位与完成事件，保证调用方永远能看到
        确定的终态和完整的的结果列表。
        """
        if task.is_terminal:
            return
        targets = self._targets.pop(task.id, None)
        if targets is not None:
            self._fill_missing(task, targets, TargetStatus.CANCELLED, "任务已取消")
        task.status = TaskStatus.CANCELLED
        task.finished_at = _utcnow()
        event = self._done.pop(task.id, None)
        if event is not None:
            event.set()
        self._running.pop(task.id, None)
        self._record_terminal(task)

    def _record_terminal(self, task: Task) -> None:
        """记录任务终态与目标结果指标（同一任务只记一次）。"""
        if self._metrics is None or task.id in self._recorded or not task.is_terminal:
            return
        self._recorded.add(task.id)
        self._metrics.inc("tcp_probe_tasks_finished_total", status=task.status.value)
        for result in task.results:
            if result is not None:
                self._metrics.inc("tcp_probe_targets_total", status=result.status.value)

    async def _run(self, task: Task, targets: list[ProbeTarget]) -> None:
        task.status = TaskStatus.RUNNING
        task.started_at = _utcnow()
        children = [
            asyncio.create_task(self._run_one(task, index, target))
            for index, target in enumerate(targets)
        ]
        try:
            await asyncio.gather(*children)
            task.status = TaskStatus.SUCCEEDED
        except asyncio.CancelledError:
            # gather 在首个取消回调时即返回，兄弟探测可能仍在跑：
            # 先确保全部子任务取消并收尾，再填终态，避免终态后被写结果。
            await self._cancel_children(children)
            self._fill_missing(task, targets, TargetStatus.CANCELLED, "任务已取消")
            task.status = TaskStatus.CANCELLED
            raise  # 保留取消语义，由 cancel()/shutdown() 侧收敛
        except Exception as exc:  # 防御性兜底：probe_tcp 已分类预期异常，这里只防内部错误
            await self._cancel_children(children)
            logger.exception("task_failed", extra={"task_id": task.id})
            task.error = f"{type(exc).__name__}: {exc}"
            self._fill_missing(task, targets, TargetStatus.ERROR, "任务内部错误")
            task.status = TaskStatus.FAILED
        finally:
            task.finished_at = _utcnow()
            event = self._done.pop(task.id, None)
            if event is not None:
                event.set()
            self._running.pop(task.id, None)
            self._targets.pop(task.id, None)
            self._record_terminal(task)

    @staticmethod
    async def _cancel_children(children: list[asyncio.Task[None]]) -> None:
        for child in children:
            child.cancel()
        if children:
            await asyncio.gather(*children, return_exceptions=True)

    async def _run_one(self, task: Task, index: int, target: ProbeTarget) -> None:
        timeout_ms = target.timeout_ms or self._settings.probe_default_timeout_ms
        async with self._semaphore:
            try:
                result = await probe_tcp(target.host, target.port, timeout_ms)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - probe_tcp 内部已做兜底
                logger.exception(
                    "probe_unexpected_error",
                    extra={"host": target.host, "port": target.port},
                )
                result = TargetResult(
                    host=target.host,
                    port=target.port,
                    status=TargetStatus.ERROR,
                    error=f"{type(exc).__name__}: {exc}",
                )
        task.results[index] = result
        task.completed += 1

    @staticmethod
    def _fill_missing(
        task: Task, targets: list[ProbeTarget], status: TargetStatus, reason: str
    ) -> None:
        for index, slot in enumerate(task.results):
            if slot is None:
                target = targets[index]
                task.results[index] = TargetResult(
                    host=target.host, port=target.port, status=status, error=reason
                )
        task.completed = sum(1 for slot in task.results if slot is not None)
