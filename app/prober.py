"""TCP 连通性探测引擎。

核心语义（与 README「探测结果状态语义」一致）：

预算模型
    ``timeout_ms`` 从 ``probe_tcp`` 进入时开始计时，同一份预算覆盖 DNS 解析与
    全部地址的握手尝试。deadline 用 ``time.monotonic()`` 计算（不受系统时钟
    回拨影响）；每次尝试的剩余预算 = deadline - now，剩余 ≤ 0 时立即停止并
    「按已收集的结论收敛」——没有明确结论则记为 timeout。

状态优先级
    open > closed > timeout > unreachable。closed 一旦出现即为最终结论（主机
    可达、端口明确拒绝），无需继续尝试其余地址；dns_error / invalid 是提前
    返回的确定性失败；error 仅用于未预期异常的兜底。

延迟口径
    latency_ms 只统计 TCP 握手阶段（``time.perf_counter``），不含 DNS 解析，
    且仅 open 时给出。

取消传播
    ``asyncio.CancelledError`` 原样向上传播，探测层绝不吞掉。

可测试缝
    - ``_connect_once``：唯一发起真实连接的协程，测试替换它即可模拟超时/取消；
    - ``classify_socket_error``：纯函数，异常 → 状态的完整映射。
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time

from app.models import TargetResult, TargetStatus

_MAX_ADDRINFOS = 8

_SockAddr = tuple[str, int] | tuple[str, int, int, int]
_AddrInfo = tuple[int, int, int, str, _SockAddr]

# 与 app.models 的主机名校验规则保持一致：probe_tcp 可能被绕过 Pydantic 直接
# 调用，入口处必须独立完成同样的校验。
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$"
)


def _ip_literal(candidate: str) -> str | None:
    """识别 IP 字面量（兼容 ``[::1]`` 方括号写法），返回去括号后的地址串。"""
    bracketed = candidate.startswith("[") and candidate.endswith("]")
    literal = candidate[1:-1] if bracketed else candidate
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        return None
    return literal


def _invalid_reason(host: object, port: object) -> str | None:
    """校验 host/port；非法时返回带机器可读前缀的原因，合法时返回 None。

    规则与 ``app.models._validate_host`` / ``ProbeTarget`` 一致：host 先 strip，
    接受 IP 字面量（含 [IPv6] 写法）或主机名正则，port 必须在 1-65535。
    """
    if not isinstance(host, str):
        return "invalid_host: host 必须是字符串"
    candidate = host.strip()
    if not candidate:
        return "invalid_host: host 不能为空"
    if _ip_literal(candidate) is None and not _HOSTNAME_RE.match(candidate):
        return f"invalid_host: {candidate!r} 不是合法的 IP 或主机名"
    if isinstance(port, bool) or not isinstance(port, int):
        return "invalid_port: port 必须是整数"
    if not 1 <= port <= 65535:
        return f"invalid_port: {port} 超出 1-65535"
    return None


def classify_socket_error(exc: BaseException) -> TargetStatus:
    """把连接阶段的异常映射为探测状态（纯函数，无 I/O）。

    映射规则：
    - ``ConnectionRefusedError`` → CLOSED：对端 RST，端口明确拒绝；
    - ``TimeoutError`` / ``asyncio.TimeoutError`` → TIMEOUT；
    - ``socket.gaierror`` → DNS_ERROR；
    - ``ConnectionResetError`` → UNREACHABLE；
    - ``OSError``（含 errno.ENETUNREACH / EHOSTUNREACH / EHOSTDOWN / ENETDOWN
      等显式不可达，以及其他无法进一步归类的 OSError）→ UNREACHABLE：
      连接阶段的 OSError 无法支持比「网络层不可达」更确定的结论；
    - 其它异常 → ERROR。
    """
    if isinstance(exc, ConnectionRefusedError):
        return TargetStatus.CLOSED
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return TargetStatus.TIMEOUT
    if isinstance(exc, socket.gaierror):
        return TargetStatus.DNS_ERROR
    if isinstance(exc, ConnectionResetError):
        return TargetStatus.UNREACHABLE
    if isinstance(exc, OSError):
        # 显式不可达 errno（ENETUNREACH/EHOSTUNREACH/EHOSTDOWN/ENETDOWN）与
        # 其余 OSError 同归 unreachable。
        return TargetStatus.UNREACHABLE
    return TargetStatus.ERROR


async def _connect_once(
    family: int,
    socktype: int,
    proto: int,
    sockaddr: _SockAddr,
    timeout_seconds: float,
) -> socket.socket:
    """对单个地址发起一次非阻塞 TCP 握手（``loop.sock_connect`` + wait_for）。

    成功返回已连接的 socket（调用方负责关闭）；超时抛 ``TimeoutError``；
    其它错误原样抛出。异常路径统一关闭 socket，``CancelledError`` 清理后
    原样传播。

    该协程是探测器的唯一连接入口，也是主要测试缝：替换它即可在不联网的
    情况下模拟超时、拒绝与取消。
    """
    loop = asyncio.get_running_loop()
    sock = socket.socket(family, socktype, proto)
    sock.setblocking(False)
    try:
        await asyncio.wait_for(loop.sock_connect(sock, sockaddr), timeout_seconds)
    except BaseException:
        # wait_for 超时抛 TimeoutError、任务取消抛 CancelledError——两条路径都
        # 必须先释放 fd，再把异常原样交还给调用方。
        sock.close()
        raise
    return sock


async def probe_tcp(host: str, port: int, timeout_ms: int) -> TargetResult:
    """探测 ``host:port`` 的 TCP 连通性并返回结构化结果。

    预算模型
        ``timeout_ms`` 从函数进入时开始计时，覆盖 DNS 解析 + 全部地址尝试；
        deadline 用 ``time.monotonic()`` 计算。每次尝试的剩余预算 =
        deadline - now，剩余 ≤ 0 时按已收集结论收敛：有超时证据则 TIMEOUT，
        否则若已有不可达证据则 UNREACHABLE，完全没有结论（例如预算在首次
        尝试前就耗尽）也记为 TIMEOUT。

    状态判定（优先级 open > closed > timeout > unreachable）
        - open：TCP 三次握手成功。latency_ms 只计握手阶段
          （``time.perf_counter``），不含 DNS；resolved_ip 为成功连接的地址。
        - closed：任一地址返回 ECONNREFUSED——主机可达、端口明确拒绝，是
          确定性结论，立即返回，不再尝试其余地址。
        - timeout：预算内没有任何地址给出明确答复，且至少一个地址尝试超时。
        - dns_error：主机名解析失败（``socket.gaierror`` / 空解析结果），
          立即返回。
        - unreachable：网络层明确不可达（ENETUNREACH / EHOSTUNREACH /
          EHOSTDOWN / ENETDOWN、ConnectionResetError 等 OSError）。
        - invalid：host/port 非法，不发起任何网络 I/O；规则与
          ``app.models._validate_host`` 一致（strip、[::1] 方括号 IPv6、
          IP 字面量或主机名正则；port ∈ 1-65535）。
        - error：未预期异常兜底，应几乎不出现。

    实现要点
        - IP 字面量跳过 DNS 直接连接；主机名用
          ``loop.getaddrinfo(..., type=SOCK_STREAM)``，解析同样消耗预算
          （``asyncio.wait_for`` 包裹；线程内的系统解析无法真正中断，
          超时只是放弃等待）。
        - 多地址按 getaddrinfo 顺序尝试，最多取前 8 个；每个地址新建非阻塞
          socket，经 ``_connect_once`` 握手。
        - resolved_ip 在 closed/timeout/unreachable 时尽量填第一个尝试过的
          地址。
        - ``asyncio.CancelledError`` 原样向上传播，任务取消依赖它。
    """
    entry = time.monotonic()
    host_text = host.strip() if isinstance(host, str) else str(host)
    port_value = port if isinstance(port, int) and not isinstance(port, bool) else 0
    try:
        invalid = _invalid_reason(host, port)
        if invalid is not None:
            return TargetResult(
                host=host_text,
                port=port_value,
                status=TargetStatus.INVALID,
                error=invalid,
            )

        # 预算从函数进入时刻算起，校验耗掉的少量时间同样计入。
        deadline = entry + timeout_ms / 1000.0
        loop = asyncio.get_running_loop()
        literal = _ip_literal(host_text)
        if literal is not None:
            if ":" in literal:
                family = socket.AF_INET6
                # Windows Proactor 的 sock_connect 要求 IPv6 完整四元组；
                # 只传 (host, port) 会得到 WinError 10022（无效参数）。
                sockaddr: _SockAddr = (literal, port, 0, 0)
            else:
                family = socket.AF_INET
                sockaddr = (literal, port)
            addr_infos: list[_AddrInfo] = [
                (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)
            ]
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return TargetResult(
                    host=host_text,
                    port=port,
                    status=TargetStatus.TIMEOUT,
                    error="timeout: 预算耗尽，未开始 DNS 解析",
                )
            try:
                addr_infos = await asyncio.wait_for(
                    loop.getaddrinfo(host_text, port, type=socket.SOCK_STREAM),
                    remaining,
                )
            except (asyncio.TimeoutError, TimeoutError):
                return TargetResult(
                    host=host_text,
                    port=port,
                    status=TargetStatus.TIMEOUT,
                    error=f"timeout: {timeout_ms}ms 预算内 DNS 解析未完成",
                )
            except socket.gaierror as exc:
                return TargetResult(
                    host=host_text,
                    port=port,
                    status=TargetStatus.DNS_ERROR,
                    error=f"dns_error: {host_text}: {exc}",
                )
            if not addr_infos:
                return TargetResult(
                    host=host_text,
                    port=port,
                    status=TargetStatus.DNS_ERROR,
                    error=f"dns_error: {host_text!r} 解析结果为空",
                )

        first_ip: str | None = None
        attempts = 0
        saw_timeout = False
        first_unreachable_error: str | None = None

        for family, socktype, proto, _canonname, sockaddr in addr_infos[:_MAX_ADDRINFOS]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break  # 预算耗尽：按已收集结论收敛
            ip = str(sockaddr[0])
            if first_ip is None:
                first_ip = ip
            attempts += 1
            started = time.perf_counter()
            try:
                connected = await _connect_once(family, socktype, proto, sockaddr, remaining)
            except Exception as exc:
                status = classify_socket_error(exc)
                if status is TargetStatus.CLOSED:
                    # 主机可达 + 端口明确拒绝 = 确定性结论，无需尝试其余地址。
                    return TargetResult(
                        host=host_text,
                        port=port,
                        status=TargetStatus.CLOSED,
                        resolved_ip=first_ip,
                        error=f"connection_refused: {ip}:{port}",
                    )
                if status is TargetStatus.TIMEOUT:
                    saw_timeout = True
                    continue
                if status is TargetStatus.UNREACHABLE:
                    if first_unreachable_error is None:
                        first_unreachable_error = f"{type(exc).__name__}: {exc}"
                    continue
                # 连接阶段出现非网络类异常：按未预期错误收敛。
                return TargetResult(
                    host=host_text,
                    port=port,
                    status=TargetStatus.ERROR,
                    resolved_ip=first_ip,
                    error=f"error: {type(exc).__name__}: {exc}",
                )
            latency_ms = round((time.perf_counter() - started) * 1000, 3)
            connected.close()
            return TargetResult(
                host=host_text,
                port=port,
                status=TargetStatus.OPEN,
                resolved_ip=ip,
                latency_ms=latency_ms,
            )

        # 优先级：timeout 高于 unreachable；两者皆无则说明预算在首次尝试前耗尽。
        if saw_timeout:
            return TargetResult(
                host=host_text,
                port=port,
                status=TargetStatus.TIMEOUT,
                resolved_ip=first_ip,
                error=f"timeout: {attempts} 个地址在 {timeout_ms}ms 预算内均无明确答复",
            )
        if first_unreachable_error is not None:
            return TargetResult(
                host=host_text,
                port=port,
                status=TargetStatus.UNREACHABLE,
                resolved_ip=first_ip,
                error=f"unreachable: {first_unreachable_error}",
            )
        return TargetResult(
            host=host_text,
            port=port,
            status=TargetStatus.TIMEOUT,
            resolved_ip=first_ip,
            error="timeout: 预算耗尽，未发起连接尝试",
        )
    except asyncio.CancelledError:
        # 取消是控制流而非失败：必须原样传播，任务取消依赖它。
        raise
    except Exception as exc:
        return TargetResult(
            host=host_text,
            port=port_value,
            status=TargetStatus.ERROR,
            error=f"error: {type(exc).__name__}: {exc}",
        )
