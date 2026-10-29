"""TaskStore 单元测试：TTL 过期、容量淘汰、活动计数与列表顺序。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.models import Task, TaskStatus
from app.store import TaskStore


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


def _task(
    task_id: str,
    status: TaskStatus = TaskStatus.SUCCEEDED,
    finished_at: datetime | None = None,
    created_at: datetime | None = None,
    total: int = 1,
) -> Task:
    task = Task(id=task_id, total=total, results=[None] * total)
    task.status = status
    if finished_at is not None:
        task.finished_at = finished_at
    if created_at is not None:
        task.created_at = created_at
    return task


def test_add_get_roundtrip() -> None:
    store = TaskStore(_settings())
    task = _task("t1", status=TaskStatus.RUNNING)
    store.add(task)
    assert store.get("t1") is task
    assert store.get("missing") is None


def test_count_active_excludes_terminal() -> None:
    store = TaskStore(_settings())
    store.add(_task("a", status=TaskStatus.RUNNING))
    store.add(_task("b", status=TaskStatus.PENDING))
    store.add(_task("c", status=TaskStatus.SUCCEEDED, finished_at=datetime.now(timezone.utc)))
    assert store.count_active() == 2


def test_ttl_expires_finished_tasks() -> None:
    now = [1000.0]
    store = TaskStore(_settings(task_ttl_seconds=10), clock=lambda: now[0])
    finished = datetime.fromtimestamp(1000.0, tz=timezone.utc)
    store.add(_task("old", finished_at=finished))

    now[0] = 1005.0
    assert store.get("old") is not None

    now[0] = 1011.0
    assert store.get("old") is None
    assert store.size() == 0


def test_running_tasks_never_expire() -> None:
    now = [1000.0]
    store = TaskStore(_settings(task_ttl_seconds=10), clock=lambda: now[0])
    store.add(_task("run", status=TaskStatus.RUNNING))
    now[0] = 2000.0
    assert store.get("run") is not None


def test_capacity_evicts_oldest_finished_only() -> None:
    store = TaskStore(_settings(task_max_retained=2))
    base = datetime.now(timezone.utc)
    store.add(_task("run", status=TaskStatus.RUNNING))
    store.add(_task("f1", finished_at=base))
    store.add(_task("f2", finished_at=base + timedelta(seconds=1)))

    # run + f1 + f2 = 3 条 > 上限 2，最旧的终态任务 f1 应被淘汰
    assert store.get("f1") is None
    assert store.get("f2") is not None
    assert store.get("run") is not None


def test_list_newest_first() -> None:
    store = TaskStore(_settings())
    base = datetime.now(timezone.utc)
    store.add(_task("old", status=TaskStatus.RUNNING, created_at=base - timedelta(seconds=10)))
    store.add(_task("new", status=TaskStatus.RUNNING, created_at=base))

    assert [task.id for task in store.list(10)] == ["new", "old"]
