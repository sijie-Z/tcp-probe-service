"""app.prober 的单元测试。

七个场景与实现契约一一对应：open / closed / invalid / dns_error / timeout /
异常分类映射 / CancelledError 传播。全部使用同步测试函数 + ``asyncio.run``，
不依赖 pytest-asyncio。
"""

from __future__ import annotations

import asyncio
import errno
import socket
import time
import uuid

import pytest

import app.prober as prober
from app.models import TargetResult, TargetStatus
from app.prober import classify_socket_error, probe_tcp


async def _hold_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """读到 EOF 后再关闭，避免服务端在探测端完成握手前提前 RST。"""
    await reader.read()
    writer.close()


def test_probe_open_loopback() -> None:
    """监听中的端口必须判为 open，latency 只反映握手阶段。"""

    async def scenario() -> TargetResult:
        server = await asyncio.start_server(_hold_connection, "127.0.0.1", 0)
        host, port = server.sockets[0].getsockname()[:2]
        try:
            return await probe_tcp(host, port, 2000)
        finally:
            server.close()
            await server.wait_closed()

    result = asyncio.run(scenario())

    assert result.status is TargetStatus.OPEN
    assert result.resolved_ip == "127.0.0.1"
    assert result.latency_ms is not None
    assert result.latency_ms > 0
    assert result.error is None


def test_probe_open_ipv6_literal() -> None:
    """IPv6 字面量（含 [::1] 方括号写法）必须跳过 DNS 并正确完成握手。"""
    try:
        listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        listener.bind(("::1", 0))
        listener.listen(4)
    except OSError:
        pytest.skip("当前环境不支持 IPv6 回环")
    port = listener.getsockname()[1]

    async def scenario() -> TargetResult:
        try:
            return await probe_tcp("[::1]", port, 3000)
        finally:
            listener.close()

    result = asyncio.run(scenario())
    assert result.status is TargetStatus.OPEN
    assert result.resolved_ip == "::1"
    assert result.latency_ms is not None


def test_probe_closed_port() -> None:
    """端口先占位再释放（无监听）时，ECONNREFUSED 必须判为 closed。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as placeholder:
        placeholder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        placeholder.bind(("127.0.0.1", 0))
        placeholder.listen(1)
        port = placeholder.getsockname()[1]

    # 本机安全软件可能把 RST 延迟约 2s 才送达，预算给足以仍然拿到真实 ECONNREFUSED。
    result = asyncio.run(probe_tcp("127.0.0.1", port, 5000))

    assert result.status is TargetStatus.CLOSED
    assert result.resolved_ip == "127.0.0.1"
    assert result.latency_ms is None


def test_probe_invalid_inputs() -> None:
    """非法 host/port 必须判为 invalid，且快速返回（不发起网络 I/O）。"""
    cases = [("bad host!", 80), ("127.0.0.1", 0), ("127.0.0.1", 65536), ("", 80)]
    for host, port in cases:
        started = time.perf_counter()
        result = asyncio.run(probe_tcp(host, port, 3000))
        elapsed = time.perf_counter() - started
        assert result.status is TargetStatus.INVALID, (host, port)
        assert result.resolved_ip is None, (host, port)
        assert result.error is not None and result.error.startswith("invalid_"), (host, port)
        assert elapsed < 0.5, f"{host!r}:{port} 校验不应发起网络 I/O（耗时 {elapsed:.3f}s）"


def test_probe_dns_error() -> None:
    """RFC 2606 保留 TLD .invalid 保证 NXDOMAIN，必须判为 dns_error。"""
    host = f"{uuid.uuid4().hex}.invalid"
    result = asyncio.run(probe_tcp(host, 80, 3000))

    assert result.status is TargetStatus.DNS_ERROR
    assert result.resolved_ip is None
    assert result.error is not None and result.error.startswith("dns_error")


def test_probe_timeout_when_connect_always_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """所有地址握手都超时时，必须判为 timeout 并保留首个尝试地址。"""

    async def fake_connect_once(*args: object, **kwargs: object) -> None:
        raise TimeoutError("simulated handshake timeout")

    monkeypatch.setattr(prober, "_connect_once", fake_connect_once)

    result = asyncio.run(probe_tcp("127.0.0.1", 80, 500))

    assert result.status is TargetStatus.TIMEOUT
    assert result.resolved_ip == "127.0.0.1"
    assert result.error is not None and result.error.startswith("timeout")


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ConnectionRefusedError(errno.ECONNREFUSED, "refused"), TargetStatus.CLOSED),
        (TimeoutError("handshake timeout"), TargetStatus.TIMEOUT),
        (asyncio.TimeoutError("budget exhausted"), TargetStatus.TIMEOUT),
        (socket.gaierror(socket.EAI_NONAME, "name unknown"), TargetStatus.DNS_ERROR),
        (ConnectionResetError(errno.ECONNRESET, "reset by peer"), TargetStatus.UNREACHABLE),
        (OSError(errno.ENETUNREACH, "network unreachable"), TargetStatus.UNREACHABLE),
        (OSError(errno.EHOSTUNREACH, "host unreachable"), TargetStatus.UNREACHABLE),
        (OSError(errno.EACCES, "permission denied"), TargetStatus.UNREACHABLE),
        (ValueError("unexpected"), TargetStatus.ERROR),
    ],
    ids=[
        "connection-refused",
        "timeout",
        "asyncio-timeout",
        "gaierror",
        "connection-reset",
        "enetunreach",
        "ehostunreach",
        "other-oserror",
        "non-oserror",
    ],
)
def test_classify_socket_error_mapping(exc: BaseException, expected: TargetStatus) -> None:
    """异常 → 状态的完整映射。"""
    assert classify_socket_error(exc) is expected


def test_probe_propagates_cancelled_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """任务取消必须抛 CancelledError，绝不能被探测层吞成普通结果。"""

    async def hanging_connect_once(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(prober, "_connect_once", hanging_connect_once)

    async def scenario() -> None:
        task = asyncio.create_task(probe_tcp("127.0.0.1", 80, 5000))
        await asyncio.sleep(0.05)  # 确保已进入连接阶段
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()

    asyncio.run(scenario())
