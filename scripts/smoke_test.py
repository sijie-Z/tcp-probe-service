#!/usr/bin/env python3
"""tcp-probe-service 端到端冒烟测试（只依赖 Python 标准库）。

对运行中的服务依次验证：
  1. 服务可达（不可达时提示启动方式，退出码 1）
  2. GET /healthz 健康检查，以及 X-Request-ID 的透传 / 缺省时自动生成
  3. 同步批量探测（wait=true）：open / closed / dns_error 三种结果与结果顺序
  4. 异步任务：POST 返回 202，轮询任务详情直到终态
  5. 错误契约：404/405/422 统一信封、字段级 details、request_id、非法请求 id 替换
  6. 限流：连续请求应触发 429 且带 Retry-After（未触发时跳过，不算失败）

用法：
    python scripts/smoke_test.py [--base-url http://127.0.0.1:8000]
    python -m scripts.smoke_test

服务运行在 Docker 容器里时，容器内的 127.0.0.1 不是宿主机，需要指定探测目标主机：
    python scripts/smoke_test.py --target-host host.docker.internal
（此时脚本会把测试监听端口绑定到所有网卡；Docker Desktop 默认支持该域名，
Linux 下需在 compose 配置 extra_hosts，本仓库 docker-compose.yml 已配置。）

退出码：0 表示全部通过（跳过不算失败）；1 表示存在失败项或服务不可达。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from typing import Any, NamedTuple

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
# 探测目标默认指向服务所在机器的回环地址（冒烟脚本与服务同机运行时成立）
DEFAULT_TARGET_HOST = "127.0.0.1"

# 任务终态；与 app.models.TaskStatus 保持一致
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})

# dns_error 目标：RFC 2606 保留的 .invalid 顶级域，保证无法解析
DNS_ERROR_HOST = "smoke-test-host.invalid"
# dns_error 目标使用的端口（域名解析失败，端口值无关紧要）
DNS_ERROR_PORT = 80

WAIT_TIMEOUT_MS = 8000       # 同步等待任务完成的上限（毫秒）
POLL_TIMEOUT_SECONDS = 10.0  # 异步任务轮询上限（秒）
RATE_LIMIT_MAX_ATTEMPTS = 30  # 限流探测最多连续 POST 次数
# 单目标探测超时。不贴着下限取值：在 Windows 上连接"已关闭的本地端口"时，
# 内核回 RST 实测可能延迟约 2 秒（非阻塞 connect 在 2 秒内会表现为超时），
# 超时值太小会把 closed 误判成 timeout
PROBE_TIMEOUT_MS = 5000

# 显式禁用代理：冒烟对象是本机/内网服务，若环境里配置了 HTTP_PROXY，
# 走代理反而会把 127.0.0.1 的请求转发出去导致误报"不可达"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class HttpResponse(NamedTuple):
    """一次 HTTP 调用的结果（4xx/5xx 也按普通响应返回，不抛异常）。"""

    status: int
    headers: Any
    body: Any


class Reporter:
    """打印每一步结果并统计，末尾输出汇总。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def ok(self, message: str) -> None:
        self.passed += 1
        print(f"[通过] {message}")

    def fail(self, message: str) -> None:
        self.failed += 1
        print(f"[失败] {message}")

    def skip(self, message: str) -> None:
        self.skipped += 1
        print(f"[跳过] {message}")

    def summary(self) -> int:
        print("-" * 60)
        print(f"汇总：通过 {self.passed}，失败 {self.failed}，跳过 {self.skipped}")
        if self.failed:
            print("结果：存在失败项，冒烟测试未通过")
            return 1
        print("结果：全部通过")
        return 0


def _brief(value: Any, limit: int = 200) -> str:
    """把任意响应体压成一行短文本，便于在失败信息里定位问题。"""
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def http_json(
    method: str,
    url: str,
    payload: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> HttpResponse:
    """发一次 HTTP 请求并按 JSON 解析响应体；4xx/5xx 不抛异常。"""
    data = None
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            status = response.status
            response_headers = response.headers
            raw = response.read()
    except urllib.error.HTTPError as exc:  # 4xx/5xx：读出响应体后按普通响应处理
        status = exc.code
        response_headers = exc.headers
        raw = exc.read()
    try:
        body = json.loads(raw.decode("utf-8")) if raw else None
    except (ValueError, UnicodeDecodeError):
        body = None
    return HttpResponse(status, response_headers, body)


def _retry_after_seconds(response: HttpResponse, default: float = 1.0) -> float:
    """解析 429 响应的 Retry-After（秒）；缺失或非法时回退默认值。"""
    if response.headers is None:
        return default
    value = response.headers.get("Retry-After")
    if value is None:
        return default
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def api_request(
    method: str,
    url: str,
    payload: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    attempts: int = 3,
) -> HttpResponse:
    """带 429 退避重试的 API 请求（限流探测步骤本身不使用它）。

    本脚本最后一步会故意打满令牌桶；紧接着重跑脚本时，前面的请求可能先吃到
    429。对 429 按 Retry-After 等待后重试，保证冒烟测试可以连续重复执行。
    """
    response = http_json(method, url, payload=payload, headers=headers, timeout=timeout)
    for _ in range(attempts - 1):
        if response.status != 429:
            return response
        time.sleep(_retry_after_seconds(response))
        response = http_json(method, url, payload=payload, headers=headers, timeout=timeout)
    return response


def _bind_address_for(target_host: str) -> str:
    """open 目标的监听地址：回环目标只绑回环（IPv6 用 ::1），否则绑所有网卡。

    服务在 Docker 容器里时，它看到的 127.0.0.1 是容器自己，必须改成
    host.docker.internal 之类的宿主机地址，此时监听端口也要绑到所有网卡才可达。
    """
    if target_host in ("127.0.0.1", "localhost"):
        return "127.0.0.1"
    if target_host == "::1":
        return "::1"
    return "0.0.0.0"


def _listen_local(bind_host: str) -> socket.socket:
    """创建 open 目标：绑定本机随机端口并 listen，全程不 accept。

    为什么"listen 但不 accept"也算 open：TCP 三次握手由内核完成——
    只要连接还排在内核 listen 队列（backlog）里、没有被占满，内核就会对 SYN
    回 SYN-ACK，客户端 connect() 随即成功返回。accept() 只决定应用何时从队列
    取走连接，与"该端口是否可连接"无关。端口传 0 表示交给内核分配空闲端口。
    """
    family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        listener.bind((bind_host, 0))
        listener.listen(8)
    except OSError:
        listener.close()
        raise
    return listener


def _closed_local_port() -> int:
    """创建 closed 目标：绑定拿到一个空闲端口后立即关闭，制造"无人监听"状态。

    关闭到服务真正发起探测之间存在被其它进程抢占该端口的极小窗口，
    冒烟场景可接受；真被抢占时会表现为该步骤的状态断言失败。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def check_reachable(base_url: str, reporter: Reporter) -> bool:
    """入口预检：服务连不上就没有继续的必要，直接提示启动方式。"""
    try:
        http_json("GET", f"{base_url}/healthz", timeout=3.0)
    except (OSError, ValueError) as exc:
        reporter.fail(f"服务不可达：{base_url}（{exc.__class__.__name__}: {exc}）")
        print("请先启动服务（make run 或 docker compose up -d）")
        return False
    reporter.ok(f"服务可达：{base_url}")
    return True


def check_healthz(base_url: str, target_host: str, reporter: Reporter) -> None:
    """健康检查：GET /healthz 应返回 200 且 body.status == "ok"。"""
    _ = target_host  # 本步骤与服务位置无关，仅为统一各步骤的函数签名
    response = api_request("GET", f"{base_url}/healthz", timeout=5.0)
    body = response.body if isinstance(response.body, dict) else {}
    if response.status == 200 and body.get("status") == "ok":
        reporter.ok(
            f"健康检查：GET /healthz -> 200，status=ok，"
            f"uptime_seconds={body.get('uptime_seconds')}"
        )
    else:
        reporter.fail(f"健康检查：HTTP {response.status}，响应体 {_brief(response.body)}")


def check_request_id(base_url: str, target_host: str, reporter: Reporter) -> None:
    """X-Request-ID：调用方传入合法值时原样透传，未传时服务自动生成。"""
    _ = target_host  # 本步骤与服务位置无关，仅为统一各步骤的函数签名
    custom = "smoke-test-request-id"
    response = api_request(
        "GET", f"{base_url}/healthz", headers={"X-Request-ID": custom}, timeout=5.0
    )
    echoed = response.headers.get("X-Request-ID") if response.headers is not None else None
    if response.status != 200 or echoed != custom:
        reporter.fail(
            f"X-Request-ID 透传：发送 {custom!r}，响应头为 {echoed!r}（HTTP {response.status}）"
        )
        return
    plain = api_request("GET", f"{base_url}/healthz", timeout=5.0)
    generated = plain.headers.get("X-Request-ID") if plain.headers is not None else None
    if not generated:
        reporter.fail("X-Request-ID：未携带请求头时，响应中没有服务自动生成的 X-Request-ID")
        return
    reporter.ok(f"X-Request-ID：自定义值 {custom} 原样透传，缺省时服务自动生成（{generated}）")


def check_error_contract(base_url: str, target_host: str, reporter: Reporter) -> None:
    """错误契约：404 / 405 / 422 统一错误信封、details、request_id，以及非法请求 id 替换。"""

    def code_is(body: Any, expected: str) -> bool:
        if not isinstance(body, dict):
            return False
        error = body.get("error")
        return isinstance(error, dict) and error.get("code") == expected

    problems: list[str] = []

    missing = api_request("GET", f"{base_url}/api/v1/tasks/does-not-exist", timeout=5.0)
    if missing.status != 404 or not code_is(missing.body, "TASK_NOT_FOUND"):
        problems.append(f"未知任务应为 404 TASK_NOT_FOUND，实际 HTTP {missing.status}")

    unknown_path = api_request("GET", f"{base_url}/definitely-not-a-route", timeout=5.0)
    if unknown_path.status != 404 or not code_is(unknown_path.body, "NOT_FOUND"):
        problems.append(f"未知路径应为 404 NOT_FOUND，实际 HTTP {unknown_path.status}")

    bad_method = api_request(
        "PUT", f"{base_url}/api/v1/tasks", payload={"targets": []}, timeout=5.0
    )
    if bad_method.status != 405 or not code_is(bad_method.body, "METHOD_NOT_ALLOWED"):
        problems.append(f"PUT 应为 405 METHOD_NOT_ALLOWED，实际 HTTP {bad_method.status}")

    invalid = api_request(
        "POST",
        f"{base_url}/api/v1/tasks",
        payload={"targets": [{"host": "bad host!", "port": 80}]},
        timeout=5.0,
    )
    if invalid.status != 422 or not code_is(invalid.body, "VALIDATION_FAILED"):
        problems.append(f"非法 host 应为 422 VALIDATION_FAILED，实际 HTTP {invalid.status}")
    else:
        error = invalid.body.get("error", {}) if isinstance(invalid.body, dict) else {}
        if not isinstance(error.get("details"), list) or not error.get("details"):
            problems.append("422 响应缺少字段级 details")
        if not error.get("request_id"):
            problems.append("422 响应缺少 request_id")

    sanitized = api_request(
        "GET",
        f"{base_url}/healthz",
        headers={"X-Request-ID": "bad id with spaces"},
        timeout=5.0,
    )
    echoed = sanitized.headers.get("X-Request-ID") if sanitized.headers is not None else None
    if not echoed or echoed == "bad id with spaces":
        problems.append(f"非法 X-Request-ID 应被替换，实际 {echoed!r}")

    if problems:
        reporter.fail("错误契约（404/405/422 信封与请求 id）：" + "；".join(problems))
        return
    reporter.ok(
        "错误契约：404/405/422 均为统一错误信封，details/request_id 齐全，非法请求 id 已替换"
    )



def check_batch_sync(base_url: str, target_host: str, reporter: Reporter) -> None:
    """同步批量探测：一次提交 open / closed / dns_error 三类目标，验证全部结果。"""
    listener = _listen_local(_bind_address_for(target_host))
    targets = [
        {"host": target_host, "port": listener.getsockname()[1], "timeout_ms": PROBE_TIMEOUT_MS},
        {"host": target_host, "port": _closed_local_port(), "timeout_ms": PROBE_TIMEOUT_MS},
        {"host": DNS_ERROR_HOST, "port": DNS_ERROR_PORT, "timeout_ms": PROBE_TIMEOUT_MS},
    ]
    try:
        response = api_request(
            "POST",
            f"{base_url}/api/v1/tasks?wait=true&wait_timeout_ms={WAIT_TIMEOUT_MS}",
            payload={"name": "smoke-sync", "targets": targets},
            timeout=WAIT_TIMEOUT_MS / 1000 + 12,  # 服务端最多等 8 秒，留足网络余量
        )
    finally:
        listener.close()  # 探测已结束，open 目标即可释放

    body = response.body if isinstance(response.body, dict) else {}
    problems: list[str] = []
    if response.status != 200:
        problems.append(f"HTTP {response.status}（期望 200，即等待窗口内任务已完成）")
    if body.get("status") != "succeeded":
        problems.append(f"任务状态 {body.get('status')!r}（期望 'succeeded'）")

    latency: Any = None
    results = body.get("results")
    if not isinstance(results, list) or len(results) != len(targets):
        problems.append(f"results 不是长度为 {len(targets)} 的列表：{_brief(results)}")
    else:
        for index, (target, result) in enumerate(zip(targets, results, strict=True)):
            if not isinstance(result, dict):
                problems.append(f"results[{index}] 不是对象：{_brief(result)}")
                continue
            # 结果必须与提交顺序一一对应（按 host + port 核对）
            if (result.get("host"), result.get("port")) != (target["host"], target["port"]):
                problems.append(
                    f"results[{index}] 顺序与提交不一致："
                    f"{result.get('host')}:{result.get('port')}，"
                    f"期望 {target['host']}:{target['port']}"
                )
        actual = [item.get("status") if isinstance(item, dict) else None for item in results]
        expected = ["open", "closed", "dns_error"]
        if actual != expected:
            problems.append(f"目标状态 {actual}（期望 {expected}）")
        first = results[0] if results and isinstance(results[0], dict) else {}
        latency = first.get("latency_ms")

    if not isinstance(latency, (int, float)):
        problems.append(f"open 目标 latency_ms 缺失或非数值：{latency!r}")

    if problems:
        reporter.fail("同步批量探测（wait=true）：" + "；".join(problems))
        if target_host in ("127.0.0.1", "localhost", "::1"):
            print(
                "提示：若服务运行在 Docker 容器内，容器看到的 127.0.0.1 是容器自己，"
                "请加 --target-host host.docker.internal 重试"
            )
        return
    reporter.ok(
        "同步批量探测（wait=true）：HTTP 200，任务 succeeded，"
        "结果顺序与提交一致，状态 open/closed/dns_error 均符合预期，"
        f"open 目标延迟 {latency}ms"
    )


def check_async(base_url: str, target_host: str, reporter: Reporter) -> None:
    """异步模式：不等待直接提交（应返回 202），再轮询任务详情直到终态。"""
    # 用已关闭的本地端口做目标：结果确定、完成快，不依赖外网
    target = {"host": target_host, "port": _closed_local_port()}
    response = api_request(
        "POST",
        f"{base_url}/api/v1/tasks",
        payload={"name": "smoke-async", "targets": [target]},
        timeout=10.0,
    )
    body = response.body if isinstance(response.body, dict) else {}
    if response.status != 202:
        reporter.fail(
            f"异步任务：提交返回 HTTP {response.status}（期望 202），"
            f"响应体 {_brief(response.body)}"
        )
        return
    task_id = body.get("id")
    if not isinstance(task_id, str) or not task_id:
        reporter.fail(f"异步任务：提交响应缺少任务 id，响应体 {_brief(response.body)}")
        return

    started = time.monotonic()
    status = body.get("status")
    polls = 0
    while status not in TERMINAL_STATUSES and time.monotonic() - started < POLL_TIMEOUT_SECONDS:
        time.sleep(0.2)
        poll = api_request("GET", f"{base_url}/api/v1/tasks/{task_id}", timeout=10.0)
        if poll.status != 200:
            reporter.fail(
                f"异步任务：轮询任务详情返回 HTTP {poll.status}，响应体 {_brief(poll.body)}"
            )
            return
        status = (poll.body or {}).get("status") if isinstance(poll.body, dict) else None
        polls += 1

    elapsed = time.monotonic() - started
    if status != "succeeded":
        reporter.fail(
            f"异步任务：{POLL_TIMEOUT_SECONDS:.0f} 秒内未达到 succeeded（最后状态 {status!r}）"
        )
        return
    reporter.ok(f"异步任务：提交返回 202，轮询 {polls} 次、{elapsed:.2f}s 后任务 succeeded")


def check_rate_limit(base_url: str, target_host: str, reporter: Reporter) -> None:
    """限流探测：快速连续提交单目标任务，期望在 30 次内出现 429 + Retry-After。

    这是脚本里唯一对 429 不做重试的步骤（重试会让限流永远测不出来）。
    30 次全部放行说明服务可能关闭了限流或 burst 较大，按"跳过"处理，不算失败。
    """
    target = {"host": target_host, "port": _closed_local_port()}
    for attempt in range(1, RATE_LIMIT_MAX_ATTEMPTS + 1):
        response = http_json(
            "POST", f"{base_url}/api/v1/tasks", payload={"targets": [target]}, timeout=10.0
        )
        if response.status == 429:
            retry_after = (
                response.headers.get("Retry-After") if response.headers is not None else None
            )
            if retry_after is None:
                reporter.fail(f"限流：第 {attempt} 次请求返回 429，但响应缺少 Retry-After 头")
            else:
                reporter.ok(f"限流：第 {attempt} 次连续请求触发 429，Retry-After={retry_after}")
            return
        if response.status not in (200, 202):
            reporter.fail(
                f"限流：第 {attempt} 次请求返回意外状态码 {response.status}，"
                f"响应体 {_brief(response.body)}"
            )
            return
    reporter.skip("未触发限流（服务可能关闭了限流或 burst 较大）")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="tcp-probe-service 端到端冒烟测试（只依赖 Python 标准库）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="运行中服务的地址")
    parser.add_argument(
        "--target-host",
        default=DEFAULT_TARGET_HOST,
        help="服务探测目标时使用的主机名；服务运行在 Docker 容器内时"
        "用 host.docker.internal（详见模块文档）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # 统一以 UTF-8 输出：Windows 控制台本身即 UTF-8（PEP 528），
    # 这里主要覆盖重定向到管道/文件时回落 GBK 的情况，避免中文乱码；
    # errors="replace" 保证极端情况下也不会因编码异常而崩溃。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    args = parse_args(argv)
    base_url = args.base_url.strip().rstrip("/")
    if "://" not in base_url:  # 允许只写 127.0.0.1:8000
        base_url = "http://" + base_url
    target_host = args.target_host.strip()

    print(f"冒烟测试目标：{base_url}（探测目标主机：{target_host}）")
    reporter = Reporter()

    # 预检不通过时没必要继续：提示启动方式后直接失败退出
    if not check_reachable(base_url, reporter):
        return 1

    steps = (check_healthz, check_request_id, check_batch_sync, check_async,
             check_error_contract, check_rate_limit)
    for step in steps:
        try:
            step(base_url, target_host, reporter)
        except Exception as exc:  # noqa: BLE001 - 单步异常记失败，不中断后续检查
            reporter.fail(f"{step.__name__} 执行异常：{exc.__class__.__name__}: {exc}")

    return reporter.summary()


if __name__ == "__main__":
    sys.exit(main())
