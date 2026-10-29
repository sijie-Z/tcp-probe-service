"""HTTP 层集成测试：TestClient + 真实本地 TCP 监听器，覆盖状态码与错误信封。"""

from __future__ import annotations

import socket
import time

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings


def _settings(**overrides) -> Settings:
    # 默认预算 3000ms：本机安全软件（火绒等）会把未监听端口的 RST 延迟约 2 秒送达，
    # 预算必须大于该窗口，closed/dns_error 才能拿到真实结论而不是 timeout。
    defaults = dict(
        rate_limit_enabled=False,
        probe_default_timeout_ms=3000,
        probe_max_timeout_ms=5000,
        task_max_targets=5,
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture()
def client():
    app = create_app(_settings())
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def open_port():
    """本机监听端口：connect 由内核完成握手（不要求 accept），足以产生 open 结果。"""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()


@pytest.fixture()
def closed_port():
    """绑定后立即关闭的端口：连接应得到 RST（closed）。"""
    probe_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe_socket.bind(("127.0.0.1", 0))
    port = probe_socket.getsockname()[1]
    probe_socket.close()
    return port


def test_healthz_echoes_valid_request_id(client: TestClient) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": "abc-123_ok:1"})
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["X-Request-ID"] == "abc-123_ok:1"


def test_invalid_request_id_is_replaced(client: TestClient) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": "bad id with spaces"})
    assert response.status_code == 200
    generated = response.headers["X-Request-ID"]
    assert generated != "bad id with spaces"
    assert len(generated) == 32


def test_wait_mode_batch_results(client: TestClient, open_port: int, closed_port: int) -> None:
    payload = {
        "name": "demo",
        "targets": [
            {"host": "127.0.0.1", "port": open_port},
            {"host": "127.0.0.1", "port": closed_port},
        ],
    }
    response = client.post("/api/v1/tasks?wait=true&wait_timeout_ms=5000", json=payload)
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "succeeded"
    assert body["completed"] == 2
    statuses = [result["status"] for result in body["results"]]
    assert statuses == ["open", "closed"]
    assert body["results"][0]["resolved_ip"] == "127.0.0.1"
    assert body["results"][0]["latency_ms"] is not None


def test_async_mode_poll_until_succeeded(client: TestClient, open_port: int) -> None:
    response = client.post(
        "/api/v1/tasks", json={"targets": [{"host": "127.0.0.1", "port": open_port}]}
    )
    assert response.status_code == 202
    task_id = response.json()["id"]

    deadline = time.time() + 5
    task = response.json()
    while time.time() < deadline:
        task = client.get(f"/api/v1/tasks/{task_id}").json()
        if task["status"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.02)

    assert task["status"] == "succeeded"
    assert task["results"][0]["status"] == "open"


def test_dns_error_result(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tasks?wait=true&wait_timeout_ms=5000",
        json={"targets": [{"host": "no-such-host-a1b2c3.invalid", "port": 80}]},
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["status"] == "dns_error"


def test_validation_rejects_bad_host(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tasks", json={"targets": [{"host": "bad host!", "port": 80}]}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert any("host" in detail["field"] for detail in body["error"]["details"])


def test_validation_rejects_empty_targets(client: TestClient) -> None:
    response = client.post("/api/v1/tasks", json={"targets": []})
    assert response.status_code == 422


def test_validation_rejects_unknown_field(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tasks",
        json={"targets": [{"host": "example.com", "port": 80}], "extra": 1},
    )
    assert response.status_code == 422


def test_malformed_json_body(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tasks", content="{not-json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422


def test_too_many_targets(client: TestClient, open_port: int) -> None:
    targets = [{"host": "127.0.0.1", "port": open_port} for _ in range(6)]
    response = client.post("/api/v1/tasks", json={"targets": targets})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TARGETS_TOO_MANY"


def test_timeout_above_server_max(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tasks",
        json={"targets": [{"host": "127.0.0.1", "port": 80, "timeout_ms": 20000}]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TIMEOUT_TOO_LARGE"


def test_unknown_task_returns_404_envelope(client: TestClient) -> None:
    response = client.get("/api/v1/tasks/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TASK_NOT_FOUND"


def test_list_tasks(client: TestClient, open_port: int) -> None:
    client.post(
        "/api/v1/tasks?wait=true&wait_timeout_ms=5000",
        json={"targets": [{"host": "127.0.0.1", "port": open_port}]},
    )
    response = client.get("/api/v1/tasks?limit=10")
    assert response.status_code == 200
    items = response.json()
    assert len(items) >= 1
    assert items[0]["total"] == 1
    assert "results" not in items[0]


def test_cancel_finished_task_conflict(client: TestClient, open_port: int) -> None:
    response = client.post(
        "/api/v1/tasks?wait=true&wait_timeout_ms=5000",
        json={"targets": [{"host": "127.0.0.1", "port": open_port}]},
    )
    task_id = response.json()["id"]

    response = client.delete(f"/api/v1/tasks/{task_id}")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TASK_ALREADY_FINISHED"


def test_rate_limit_429_and_healthz_exempt() -> None:
    app = create_app(
        _settings(rate_limit_enabled=True, rate_limit_rps=0.01, rate_limit_burst=2)
    )
    payload = {"targets": [{"host": "127.0.0.1", "port": 1}]}
    with TestClient(app) as rate_limited_client:
        for _ in range(2):
            assert rate_limited_client.post("/api/v1/tasks", json=payload).status_code == 202

        response = rate_limited_client.post("/api/v1/tasks", json=payload)
        assert response.status_code == 429
        body = response.json()
        assert body["error"]["code"] == "RATE_LIMITED"
        assert "Retry-After" in response.headers
        # 429 也必须带 X-Request-ID，且与响应体一致
        assert response.headers["X-Request-ID"] == body["error"]["request_id"]

        for _ in range(5):
            assert rate_limited_client.get("/healthz").status_code == 200


def test_capacity_backpressure_returns_503_with_retry_after() -> None:
    app = create_app(_settings(task_max_active=1))
    slow_payload = {"targets": [{"host": "192.0.2.1", "port": 80, "timeout_ms": 5000}]}
    with TestClient(app) as busy_client:
        assert busy_client.post("/api/v1/tasks", json=slow_payload).status_code == 202
        response = busy_client.post("/api/v1/tasks", json=slow_payload)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "TASK_CAPACITY_EXCEEDED"
    assert response.headers["Retry-After"] == "2"


def test_validation_rejects_bool_port(client: TestClient) -> None:
    # Pydantic 默认 lax 模式会把 true 当成 1，必须显式拒绝
    response = client.post(
        "/api/v1/tasks", json={"targets": [{"host": "127.0.0.1", "port": True}]}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_delete_is_idempotent_for_cancelled_task(client: TestClient) -> None:
    payload = {"targets": [{"host": "192.0.2.1", "port": 80, "timeout_ms": 5000}]}
    task_id = client.post("/api/v1/tasks", json=payload).json()["id"]

    first = client.delete(f"/api/v1/tasks/{task_id}")
    assert first.status_code == 200
    assert first.json()["status"] == "cancelled"

    second = client.delete(f"/api/v1/tasks/{task_id}")
    assert second.status_code == 200
    assert second.json()["status"] == "cancelled"


def test_unhandled_error_returns_envelope_with_request_id() -> None:
    app = create_app(_settings())

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    with TestClient(app, raise_server_exceptions=False) as error_client:
        response = error_client.get("/boom", headers={"X-Request-ID": "trace-500"})

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["error"]["request_id"] == "trace-500"
    assert response.headers["X-Request-ID"] == "trace-500"
