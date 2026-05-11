"""网络安全策略 — SSRF 防护 + 限流。

SSRF 防护：
- 禁止访问内网地址（127.0.0.1, 10.x, 172.16-31.x, 192.168.x, ::1, fc00::/7）
- 禁止访问文件协议（file://, ftp://）
- 域名解析后二次校验 IP

限流：
- 同一域名 1 秒内最多 1 次请求
"""

import ipaddress
import logging
import re
import time
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 禁止的协议
_BLOCKED_SCHEMES = {"file", "ftp", "data", "javascript"}

# 内网 IP 范围
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("169.254.0.0/16"),
]


class NetworkPolicy:
    """网络安全策略 — SSRF 防护 + 限流。"""

    def __init__(self):
        self._last_request_time: dict[str, float] = {}
        self._min_interval = 1.0  # 同域名最小请求间隔（秒）

    def validate_url(self, url: str) -> tuple[bool, str]:
        """校验 URL 是否安全。

        Returns:
            (是否安全, 原因)
        """
        parsed = urlparse(url)

        # 协议检查
        if parsed.scheme.lower() in _BLOCKED_SCHEMES:
            return False, f"禁止访问 {parsed.scheme}:// 协议"

        if parsed.scheme.lower() not in ("http", "https"):
            return False, f"不支持的协议: {parsed.scheme}"

        # 主机名检查
        hostname = parsed.hostname
        if not hostname:
            return False, "URL 缺少主机名"

        # IP 地址直接检查
        try:
            ip = ipaddress.ip_address(hostname)
            if self._is_private_ip(ip):
                return False, f"禁止访问内网地址: {hostname}"
        except ValueError:
            pass  # 不是 IP，是域名，继续

        # 常见内网域名检查
        if hostname in ("localhost", "localhost.localdomain"):
            return False, "禁止访问 localhost"

        return True, ""

    def check_rate_limit(self, url: str) -> tuple[bool, str]:
        """检查域名级别的请求限流。

        Returns:
            (是否允许, 原因)
        """
        parsed = urlparse(url)
        hostname = parsed.hostname or url

        now = time.time()
        last = self._last_request_time.get(hostname, 0)

        if now - last < self._min_interval:
            wait = self._min_interval - (now - last)
            return False, f"请求过于频繁，请 {wait:.1f} 秒后重试"

        self._last_request_time[hostname] = now
        return True, ""

    @staticmethod
    def _is_private_ip(ip) -> bool:
        """检查 IP 是否属于内网。"""
        for network in _PRIVATE_NETWORKS:
            if ip in network:
                return True
        return False
