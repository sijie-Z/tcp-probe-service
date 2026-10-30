"""HTTP 层：应用工厂、路由、ASGI 中间件与统一错误处理。

中间件顺序（添加顺序的逆序执行）：
- RequestContextMiddleware（最外层）：生成/透传 X-Request-ID，记录结构化访问日志；
- RateLimitMiddleware：只对 /api/* 做按来源 IP 的令牌桶限流，超限 429 + Retry-After。

客户端 IP 取 TCP 对端地址（scope["client"]）。不信任 X-Forwarded-For：
若部署在反向代理之后，限流会退化为按代理 IP 计算，需要在代理层做真实来源限流（见 README）。
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.config import Settings
from app.log import configure_logging, request_id_var
from app.metrics import Metrics
from app.models import (
    CreateTaskRequest,
    ErrorBody,
    ErrorResponse,
    FieldError,
    Task,
    TaskSummary,
)
from app.ratelimit import RateLimiter
from app.service import (
    ProbeRequestError,
    ProbeService,
    TaskAlreadyFinishedError,
    TaskCapacityError,
    TaskNotFoundError,
)
from app.store import TaskStore

logger = logging.getLogger(__name__)

_REQUEST_ID_MAX_LEN = 64
_ALLOWED_REQUEST_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:"
)


def _sanitize_request_id(value: str | None) -> str:
    """透传调用方 request id（限长、限字符集），否则生成新的。"""
    if value and 0 < len(value) <= _REQUEST_ID_MAX_LEN and set(value) <= _ALLOWED_REQUEST_ID_CHARS:
        return value
    return uuid.uuid4().hex


def _error_response(
    status_code: int,
    code: str,
    message: str,
    request_id: str,
    details: list[FieldError] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(code=code, message=message, request_id=request_id, details=details)
    )
    return JSONResponse(
        status_code=status_code, content=jsonable_encoder(body), headers=headers
    )


class RequestContextMiddleware:
    """为每个 HTTP 请求生成/透传 X-Request-ID，并输出一条结构化访问日志。"""

    def __init__(self, app) -> None:  # noqa: ANN001 - ASGI app
        self.app = app

    async def __call__(self, scope, receive, send):  # noqa: ANN001 - ASGI 协议
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = _sanitize_request_id(headers.get("x-request-id"))
        token = request_id_var.set(request_id)
        scope.setdefault("state", {})["request_id"] = request_id

        status_code = 500
        start = time.perf_counter()
        client = scope.get("client")
        client_ip = client[0] if client else "-"

        async def send_wrapper(message):  # noqa: ANN001 - ASGI message
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "access",
                extra={
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "status": status_code,
                    "duration_ms": round(duration_ms, 1),
                    "client_ip": client_ip,
                },
            )
            request_id_var.reset(token)


class RateLimitMiddleware:
    """按来源 IP 的令牌桶限流，只作用于 /api/*；超限返回 429 与 Retry-After。"""

    def __init__(self, app, limiter: RateLimiter, metrics: Metrics | None = None) -> None:  # noqa: ANN001 - ASGI app
        self.app = app
        self.limiter = limiter
        self.metrics = metrics

    async def __call__(self, scope, receive, send):  # noqa: ANN001 - ASGI 协议
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        key = client[0] if client else "unknown"
        allowed, retry_after = self.limiter.check(key)
        if not allowed:
            if self.metrics is not None:
                self.metrics.inc("tcp_probe_rate_limited_total")
            # 上限 3600 秒：防止极小 rps 配置把 math.ceil 推到溢出
            retry_seconds = max(1, math.ceil(min(retry_after, 3600.0)))
            request_id = str(scope.get("state", {}).get("request_id") or "") or uuid.uuid4().hex
            response = _error_response(
                429,
                "RATE_LIMITED",
                f"请求过于频繁，请在 {retry_seconds} 秒后重试",
                request_id,
                headers={"Retry-After": str(retry_seconds)},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class BodySizeLimitMiddleware:
    """请求体大小限制：Content-Length 超过上限直接 413 统一信封，避免超大 JSON 占用内存。

    限制：只依据 Content-Length 头（常规 JSON 客户端都会发送）；
    无 Content-Length 的分块请求由 uvicorn 的连接层兜底。
    """

    def __init__(self, app, max_bytes: int, metrics: Metrics | None = None) -> None:  # noqa: ANN001 - ASGI app
        self.app = app
        self.max_bytes = max_bytes
        self.metrics = metrics

    async def __call__(self, scope, receive, send):  # noqa: ANN001 - ASGI 协议
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        raw = headers.get("content-length")
        if raw is not None:
            try:
                length = int(raw)
            except ValueError:
                length = None
            if length is not None and length > self.max_bytes:
                if self.metrics is not None:
                    self.metrics.inc("tcp_probe_body_rejected_total")
                request_id = str(scope.get("state", {}).get("request_id") or "") or uuid.uuid4().hex
                response = _error_response(
                    413,
                    "PAYLOAD_TOO_LARGE",
                    f"请求体超过大小上限（{self.max_bytes} 字节）",
                    request_id,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _register_exception_handlers(app: FastAPI) -> None:
    def request_id() -> str:
        return request_id_var.get()

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            FieldError(
                field=".".join(str(part) for part in err.get("loc", ()) if part != "body"),
                message=str(err.get("msg", "invalid")),
            )
            for err in exc.errors()
        ]
        return _error_response(422, "VALIDATION_FAILED", "请求参数校验失败", request_id(), details)

    @app.exception_handler(TaskNotFoundError)
    async def on_task_not_found(_request: Request, _exc: TaskNotFoundError) -> JSONResponse:
        return _error_response(404, "TASK_NOT_FOUND", "任务不存在或已过期", request_id())

    @app.exception_handler(TaskAlreadyFinishedError)
    async def on_task_finished(_request: Request, _exc: TaskAlreadyFinishedError) -> JSONResponse:
        return _error_response(409, "TASK_ALREADY_FINISHED", "任务已结束，无法取消", request_id())

    @app.exception_handler(TaskCapacityError)
    async def on_task_capacity(_request: Request, exc: TaskCapacityError) -> JSONResponse:
        return _error_response(
            503,
            "TASK_CAPACITY_EXCEEDED",
            str(exc),
            request_id(),
            headers={"Retry-After": "2"},
        )

    @app.exception_handler(ProbeRequestError)
    async def on_probe_request(_request: Request, exc: ProbeRequestError) -> JSONResponse:
        return _error_response(422, exc.code, str(exc), request_id())

    @app.exception_handler(StarletteHTTPException)
    async def on_http_exception(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            404: "NOT_FOUND",
            405: "METHOD_NOT_ALLOWED",
            415: "UNSUPPORTED_MEDIA_TYPE",
        }.get(exc.status_code, f"HTTP_{exc.status_code}")
        return _error_response(
            exc.status_code, code, str(exc.detail), request_id(), headers=exc.headers
        )

    @app.exception_handler(Exception)
    async def on_unhandled(request: Request, _exc: Exception) -> JSONResponse:
        # 该 handler 由最外层的 ServerErrorMiddleware 调用：此时请求中间件已退出、
        # ContextVar 已复位，且 500 响应不经过中间件的 send 包装。因此 request_id
        # 必须从 scope state 读取，并显式写入响应头，保证链路可关联。
        rid = str(getattr(request.state, "request_id", "") or request_id())
        logger.exception("unhandled_error")
        return _error_response(
            500, "INTERNAL_ERROR", "服务内部错误", rid, headers={"X-Request-ID": rid}
        )


def _register_routes(
    app: FastAPI, service: ProbeService, store: TaskStore, started_at: float, metrics: Metrics
) -> None:
    @app.get("/", tags=["运维"], summary="服务信息")
    async def index() -> dict:
        return {
            "service": "tcp-probe-service",
            "version": __version__,
            "docs": "/docs",
            "endpoints": [
                "POST /api/v1/tasks",
                "GET /api/v1/tasks",
                "GET /api/v1/tasks/{task_id}",
                "DELETE /api/v1/tasks/{task_id}",
                "GET /healthz",
                "GET /metrics",
            ],
        }

    @app.get("/healthz", tags=["运维"], summary="存活探针（不参与限流）")
    async def healthz() -> dict:
        return {"status": "ok", "uptime_seconds": round(time.monotonic() - started_at, 1)}

    @app.get("/metrics", tags=["运维"], summary="Prometheus 文本指标（不参与限流）")
    async def metrics_endpoint() -> PlainTextResponse:
        return PlainTextResponse(
            metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8"
        )

    @app.post(
        "/api/v1/tasks",
        response_model=Task,
        status_code=202,
        responses={200: {"model": Task, "description": "wait=true 且任务在等待窗口内完成"}},
        tags=["任务"],
        summary="创建探测任务（批量，可选同步等待）",
    )
    async def create_task(
        payload: CreateTaskRequest,
        response: Response,
        wait: bool = Query(
            False,
            description="true 时最多等待 wait_timeout_ms 毫秒；任务结束则直接返回结果",
        ),
        wait_timeout_ms: int = Query(
            10000,
            ge=1,
            le=60000,
            description="wait=true 时的最长等待时间（毫秒）",
        ),
    ) -> Task:
        task = service.submit(payload)
        if wait:
            task = await service.wait_for(task.id, wait_timeout_ms)
        response.status_code = 200 if task.is_terminal else 202
        return task

    @app.get(
        "/api/v1/tasks",
        response_model=list[TaskSummary],
        tags=["任务"],
        summary="列出最近任务（按创建时间倒序）",
    )
    async def list_tasks(limit: int = Query(20, ge=1, le=100)) -> list[TaskSummary]:
        return [TaskSummary.from_task(task) for task in store.list(limit)]

    @app.get(
        "/api/v1/tasks/{task_id}",
        response_model=Task,
        tags=["任务"],
        summary="查询任务状态与结果",
    )
    async def get_task(task_id: str) -> Task:
        task = store.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        return task

    @app.delete(
        "/api/v1/tasks/{task_id}",
        response_model=Task,
        tags=["任务"],
        summary="取消未结束的任务",
    )
    async def cancel_task(task_id: str) -> Task:
        return await service.cancel(task_id)


def create_app(settings: Settings | None = None) -> FastAPI:
    """应用工厂：测试可注入自定义 Settings，生产从环境变量读取。"""
    settings = settings or Settings.from_env()
    metrics = Metrics()
    store = TaskStore(settings)
    service = ProbeService(settings, store, metrics)
    limiter = (
        RateLimiter(
            settings.rate_limit_rps,
            settings.rate_limit_burst,
            settings.rate_limit_idle_ttl_seconds,
        )
        if settings.rate_limit_enabled
        else None
    )
    started_at = time.monotonic()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        logger.info("service_started")
        yield
        await service.shutdown()
        logger.info("service_stopped")

    app = FastAPI(
        title="简易 TCP 连接探测服务",
        version=__version__,
        description=(
            "HTTP 接口接收批量 TCP 连通性探测任务，asyncio 自研探测引擎"
            "（未使用任何第三方探测组件），按来源 IP 令牌桶限流。完整说明见 README。"
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.service = service

    if limiter is not None:
        app.add_middleware(RateLimitMiddleware, limiter=limiter, metrics=metrics)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes, metrics=metrics)
    app.add_middleware(RequestContextMiddleware)

    _register_exception_handlers(app)
    _register_routes(app, service, store, started_at, metrics)
    return app
