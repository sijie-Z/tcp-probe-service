#!/usr/bin/env python3
"""tcp-probe-service 轻量压测脚本（仅标准库，两套实现共用同一份）。

覆盖两组指标：
1. 批量探测：一个任务提交 N 个目标（wait=true），统计端到端耗时与结果分布；
2. 提交吞吐：C 个线程并发提交 K 个单目标任务，统计提交延迟分位数与 QPS。

探测目标使用本机临时监听端口（open，命中快），不依赖外网；
限流会干扰吞吐测试——请用 RATE_LIMIT_ENABLED=false 启动被测服务。

用法：
    python scripts/load_test.py --base-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import socket
import statistics
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# 显式禁用代理，避免 HTTP_PROXY 把本机请求转发出去导致误判
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(method: str, url: str, payload: object = None, timeout: float = 180.0):
    """发一次 JSON 请求；4xx/5xx 也按普通响应返回（不抛异常）。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, None


def percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(ratio * (len(ordered) - 1))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description="tcp-probe-service 轻量压测（标准库）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--batch-targets", type=int, default=100, help="批量任务的目标数（<=200）")
    parser.add_argument("--submit-requests", type=int, default=100,
                        help="吞吐测试的提交总数（过高会触发 TASK_MAX_ACTIVE 背压，属正常保护）")
    parser.add_argument("--concurrency", type=int, default=10, help="吞吐测试的并发线程数")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(256)
    port = listener.getsockname()[1]
    target = {"host": "127.0.0.1", "port": port}

    # 后台 accept：真实服务器会消费连接；不 accept 会让 backlog 堆积，
    # SYN 被丢弃后探测超时，会把吞吐测试变成背压测试（Java 版实测踩到过）
    stop_accept = threading.Event()

    def accept_loop() -> None:
        while not stop_accept.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            conn.close()

    acceptor = threading.Thread(target=accept_loop, daemon=True)
    acceptor.start()

    try:
        status, _ = http_json("GET", f"{base}/healthz", timeout=5.0)
        if status != 200:
            print(f"服务不可达：{base}（/healthz -> {status}）")
            print("请先启动服务；压测时建议 RATE_LIMIT_ENABLED=false")
            return 1

        print("== 批量探测 ==")
        targets = [dict(target) for _ in range(args.batch_targets)]
        started = time.perf_counter()
        status, task = http_json(
            "POST",
            f"{base}/api/v1/tasks?wait=true&wait_timeout_ms=60000",
            {"name": "load-batch", "targets": targets},
            timeout=180.0,
        )
        elapsed = time.perf_counter() - started
        if status != 200 or not isinstance(task, dict):
            print(f"批量探测失败：HTTP {status}")
            return 1
        results = task.get("results") or []
        open_count = sum(
            1 for result in results if isinstance(result, dict) and result.get("status") == "open"
        )
        print(
            f"目标数 {len(targets)}，任务状态 {task.get('status')}，"
            f"open {open_count}/{len(targets)}"
        )
        print(
            f"端到端耗时 {elapsed * 1000:.0f} ms，"
            f"平均 {elapsed * 1000 / max(1, len(targets)):.2f} ms/目标"
        )

        def submit(_: int) -> tuple[int, float]:
            begin = time.perf_counter()
            status_code, _body = http_json(
                "POST", f"{base}/api/v1/tasks", {"targets": [target]}, timeout=30.0
            )
            return status_code, (time.perf_counter() - begin) * 1000

        print()
        print("== 提交吞吐 ==")
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            outcomes = list(pool.map(submit, range(args.submit_requests)))
        wall = time.perf_counter() - started

        accepted = sum(1 for status_code, _ in outcomes if status_code == 202)
        throttled = sum(1 for status_code, _ in outcomes if status_code == 429)
        capacity = sum(1 for status_code, _ in outcomes if status_code == 503)
        latencies = [latency for status_code, latency in outcomes if status_code == 202]
        print(
            f"请求 {args.submit_requests}，并发 {args.concurrency}，"
            f"202 {accepted}，429 {throttled}，503 {capacity}，墙钟 {wall:.2f}s"
        )
        if latencies:
            print(
                f"提交延迟 p50 {percentile(latencies, 0.5):.1f} ms，"
                f"p95 {percentile(latencies, 0.95):.1f} ms，"
                f"平均 {statistics.fmean(latencies):.1f} ms"
            )
            print(f"提交 QPS {args.submit_requests / wall:.0f} req/s")
        if throttled:
            print("提示：出现 429——请以 RATE_LIMIT_ENABLED=false 启动服务后再压测")
        if capacity:
            print(
                "提示：出现 503——触发了 TASK_MAX_ACTIVE 背压（正常保护），"
                "可降低并发或调大 TASK_MAX_ACTIVE"
            )
        return 0
    finally:
        stop_accept.set()
        listener.close()
        acceptor.join(timeout=1.0)


if __name__ == "__main__":
    raise SystemExit(main())
