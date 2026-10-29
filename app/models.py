"""领域模型与 API 数据结构。

约定：
- 请求模型（ProbeTarget / CreateTaskRequest）由 Pydantic 校验，字段未知直接拒绝；
- TargetResult / Task 同时承载服务内部状态与 API 响应，字段只增不改；
- Task 的并发安全依赖单事件循环：所有写入都发生在同一线程，无需加锁。
"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$"
)


class TargetStatus(str, Enum):
    """单个目标的探测结果状态。语义见 README「探测结果状态语义」。"""

    OPEN = "open"
    CLOSED = "closed"
    TIMEOUT = "timeout"
    DNS_ERROR = "dns_error"
    UNREACHABLE = "unreachable"
    INVALID = "invalid"
    ERROR = "error"
    CANCELLED = "cancelled"


class TaskStatus(str, Enum):
    """探测任务的生命周期状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED)


def _validate_host(value: str) -> str:
    """接受主机名或 IP 字面量（含带方括号的 IPv6 写法）。"""
    candidate = value.strip()
    bracketed = candidate.startswith("[") and candidate.endswith("]")
    literal = candidate[1:-1] if bracketed else candidate
    try:
        ipaddress.ip_address(literal)
        return candidate
    except ValueError:
        pass
    if not _HOSTNAME_RE.match(candidate):
        raise ValueError("host 不是合法的主机名或 IP 地址")
    return candidate


class ProbeTarget(BaseModel):
    """单个探测目标（请求体中的一项）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    host: Annotated[
        str, Field(min_length=1, max_length=253, description="主机名或 IP 字面量（IPv4/IPv6）")
    ]
    port: Annotated[int, Field(ge=1, le=65535, description="TCP 端口（1-65535）")]
    timeout_ms: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description="单目标探测超时（毫秒）；缺省用服务端默认值，不得超过服务端上限",
        ),
    ]

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        return _validate_host(value)


class CreateTaskRequest(BaseModel):
    """创建探测任务（批量）。"""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str | None, Field(default=None, max_length=100, description="任务名（可选）")]
    targets: Annotated[list[ProbeTarget], Field(min_length=1, description="探测目标列表")]


class TargetResult(BaseModel):
    """单个目标的探测结果。"""

    host: str
    port: int
    status: TargetStatus
    resolved_ip: str | None = Field(
        default=None, description="实际尝试/成功连接的 IP（主机名解析后）"
    )
    latency_ms: float | None = Field(
        default=None, description="TCP 握手耗时（毫秒），仅 open 时给出；不含 DNS 阶段"
    )
    error: str | None = Field(default=None, description="失败原因摘要（机器可读前缀 + 细节）")


class Task(BaseModel):
    """一次批量探测任务（内部状态 + API 响应）。"""

    id: str
    name: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    total: int
    completed: int = 0
    results: list[TargetResult | None] = Field(
        default_factory=list, description="与提交顺序一致的探测结果；未完成的槽位为 null"
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal


class TaskSummary(BaseModel):
    """任务列表项（不含结果明细，控制列表响应体积）。"""

    id: str
    name: str | None
    status: TaskStatus
    total: int
    completed: int
    created_at: datetime
    finished_at: datetime | None = None

    @classmethod
    def from_task(cls, task: Task) -> TaskSummary:
        return cls(
            id=task.id,
            name=task.name,
            status=task.status,
            total=task.total,
            completed=task.completed,
            created_at=task.created_at,
            finished_at=task.finished_at,
        )


class FieldError(BaseModel):
    field: str
    message: str


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    details: list[FieldError] | None = None


class ErrorResponse(BaseModel):
    """统一错误响应体：所有非 2xx 响应都采用该结构。"""

    error: ErrorBody
