"""进程内任务存储。

单实例部署下的最简实现：任务只保存在内存中，进程重启即丢失（README「已知限制」有说明）。
两条治理规则把内存限制在可控范围：
- TTL：终态任务超过 ttl 秒后，在访问/写入时惰性清理；
- 保留上限：任务数超过 max_retained 时，按完成时间淘汰最旧的终态任务，运行中的任务永不淘汰。

单事件循环模型下不存在跨线程写竞争，无需加锁。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from app.config import Settings
from app.models import Task


class TaskStore:
    def __init__(self, settings: Settings, clock: Callable[[], float] = time.time) -> None:
        self._tasks: dict[str, Task] = {}
        self._ttl = settings.task_ttl_seconds
        self._max_retained = settings.task_max_retained
        self._clock = clock

    def add(self, task: Task) -> None:
        self._sweep_expired()
        self._tasks[task.id] = task
        self._trim_to_capacity()

    def get(self, task_id: str) -> Task | None:
        task = self._tasks.get(task_id)
        if task is not None and self._is_expired(task):
            del self._tasks[task_id]
            return None
        return task

    def list(self, limit: int) -> list[Task]:
        self._sweep_expired()
        ordered = sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)
        return ordered[:limit]

    def count_active(self) -> int:
        return sum(1 for task in self._tasks.values() if not task.is_terminal)

    def size(self) -> int:
        return len(self._tasks)

    def _is_expired(self, task: Task) -> bool:
        return (
            task.is_terminal
            and task.finished_at is not None
            and self._clock() - task.finished_at.timestamp() > self._ttl
        )

    def _sweep_expired(self) -> None:
        expired = [task_id for task_id, task in self._tasks.items() if self._is_expired(task)]
        for task_id in expired:
            del self._tasks[task_id]

    def _trim_to_capacity(self) -> None:
        overflow = len(self._tasks) - self._max_retained
        if overflow <= 0:
            return
        finished = sorted(
            (task for task in self._tasks.values() if task.is_terminal),
            key=lambda t: t.finished_at.timestamp() if t.finished_at else 0.0,
        )
        for task in finished[:overflow]:
            del self._tasks[task.id]
