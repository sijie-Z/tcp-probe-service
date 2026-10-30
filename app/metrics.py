"""进程内指标（Prometheus 文本格式，零依赖手写实现）。

只统计运行期计数（counter），不维护直方图；重启即归零，单实例部署下足够。
线程模型：所有自增都发生在事件循环线程内，无需加锁。
"""

from __future__ import annotations

from typing import Final

_FAMILY_HELP: Final[dict[str, str]] = {
    "tcp_probe_tasks_submitted_total": "已接收的探测任务数",
    "tcp_probe_tasks_rejected_total": "被拒绝的提交数（按原因）",
    "tcp_probe_tasks_finished_total": "进入终态的任务数（按状态）",
    "tcp_probe_targets_total": "目标探测结果数（按状态）",
    "tcp_probe_rate_limited_total": "被限流拒绝的请求数",
    "tcp_probe_body_rejected_total": "因请求体超限被拒绝的请求数",
}


class Metrics:
    """极简计数器集合：`inc(name, **labels)` 自增，`render()` 输出 Prometheus 文本格式。"""

    def __init__(self) -> None:
        self._values: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}

    def inc(self, name: str, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        self._values[key] = self._values.get(key, 0) + 1

    def value(self, name: str, **labels: str) -> int:
        return self._values.get((name, tuple(sorted(labels.items()))), 0)

    def render(self) -> str:
        lines: list[str] = []
        rendered_families: set[str] = set()
        for (name, labels), value in sorted(self._values.items()):
            if name not in rendered_families:
                lines.append(f"# HELP {name} {_FAMILY_HELP.get(name, '')}")
                lines.append(f"# TYPE {name} counter")
                rendered_families.add(name)
            label_text = ""
            if labels:
                label_text = "{" + ",".join(f'{key}="{val}"' for key, val in labels) + "}"
            lines.append(f"{name}{label_text} {value}")
        return "\n".join(lines) + "\n"
