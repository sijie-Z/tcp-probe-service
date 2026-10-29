"""JSON 结构化日志。

输出单行 JSON，便于采集与按 request_id 串联请求。字段固定：
ts / level / logger / msg + 可选 request_id 与访问日志字段
（method/path/status/duration_ms/client_ip）。
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

request_id_var: ContextVar[str] = ContextVar("request_id", default="")

_ACCESS_KEYS = ("method", "path", "status", "duration_ms", "client_ip", "task_id", "host", "port")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        for key in _ACCESS_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    """配置根 logger；重复调用幂等（只替换本模块自己的 handler）。"""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, "_tcp_probe_handler", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._tcp_probe_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)
