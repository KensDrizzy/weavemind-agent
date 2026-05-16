"""ChromeLauncher — Chrome 调试端口检测工具。

仅保留端口检测功能，不再负责启动/停止 Chrome 进程。
--isolated 模式由 MCP Server 自行管理 Chrome 实例，
--autoConnect 模式连接用户已打开的 Chrome（Chrome 144+）。
"""

import logging
import platform
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ChromeLauncher:
    """Chrome 调试端口检测工具。"""

    def __init__(self, port: int = 9222):
        self.port = port

    def is_running(self) -> bool:
        """检查 Chrome 是否在调试端口运行。"""
        return self._check_port()

    def _check_port(self) -> bool:
        """检查调试端口是否有 Chrome DevTools 协议服务运行。"""
        import urllib.request
        for host in ("127.0.0.1", "localhost", "::1"):
            try:
                url = f"http://{host}:{self.port}/json/version"
                if host == "::1":
                    url = f"http://[::1]:{self.port}/json/version"
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=2) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                continue
        return False