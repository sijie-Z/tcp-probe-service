"""主机名校验（Pydantic 模型与探测引擎共用的单一事实来源）。

规则：
- 接受 IP 字面量（IPv4/IPv6，IPv6 兼容 ``[::1]`` 方括号写法）；
- 接受主机名：RFC 1123 兼容，且允许下划线（Docker Compose 服务名等内网命名常见）。
"""

from __future__ import annotations

import ipaddress
import re

HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9_-]{1,63}(?<!-)(?:\.(?!-)[A-Za-z0-9_-]{1,63}(?<!-))*\.?$"
)


def strip_brackets(candidate: str) -> str:
    """去掉 IPv6 字面量的方括号写法：``[::1]`` → ``::1``。"""
    if candidate.startswith("[") and candidate.endswith("]"):
        return candidate[1:-1]
    return candidate


def ip_literal(candidate: str) -> str | None:
    """识别 IP 字面量（兼容方括号 IPv6），返回去括号后的地址串；非 IP 返回 None。"""
    literal = strip_brackets(candidate)
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        return None
    return literal


def validate_host(value: str) -> str:
    """校验并返回主机名（strip 之后）。非法时抛 ValueError。"""
    candidate = value.strip()
    if ip_literal(candidate) is not None or HOSTNAME_RE.match(candidate):
        return candidate
    raise ValueError("host 不是合法的主机名或 IP 地址")
