"""ChromeLauncher — 自动管理 Chrome 进程生命周期。

在 MCPManager 初始化 Chrome DevTools MCP Server 前，
检查 Chrome 是否已在调试端口运行；若未运行且配置了 auto_start，
则自动启动 Chrome with --remote-debugging-port。

设计原则：
  - 轻量：只做端口检测 + 进程启动，不做多余的事
  - 安全：默认 auto_start=false，避免意外启动浏览器
  - 幂等：Chrome 已运行时直接返回成功，不重复启动
"""

import logging
import platform
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ChromeLauncher:
    """自动管理 Chrome 进程生命周期。"""

    def __init__(
        self,
        port: int = 9222,
        headless: bool = False,
        executable: Optional[str] = None,
    ):
        self.port = port
        self.headless = headless
        self.executable = executable or self._find_chrome()
        self._process: Optional[subprocess.Popen] = None

    # ── Chrome 路径自动检测 ──────────────────────────────────────

    @staticmethod
    def _find_chrome() -> str:
        """根据操作系统自动查找 Chrome/Chromium 路径。"""
        system = platform.system()
        paths = []

        if system == "Darwin":  # macOS
            paths = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
                "/opt/homebrew/bin/chromium",
            ]
        elif system == "Linux":
            paths = [
                "/usr/bin/google-chrome",
                "/usr/bin/chromium",
                "/usr/bin/chromium-browser",
                "/snap/bin/chromium",
            ]
        elif system == "Windows":
            paths = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ]

        for path in paths:
            if Path(path).exists():
                return path

        raise RuntimeError(
            "无法找到 Chrome/Chromium，请在 config.yaml 的 mcp.servers.chrome.chrome.executable "
            "中手动指定路径"
        )

    # ── 端口检测 ────────────────────────────────────────────────

    def _check_port(self) -> bool:
        """检查调试端口是否有 Chrome DevTools 协议服务运行。

        不仅检查端口是否被占用，还验证 /json/version 端点
        是否返回有效响应（区分普通 Chrome 和调试模式 Chrome）。

        Chrome 可能绑定在 IPv4 (127.0.0.1) 或 IPv6 (::1)，
        需要同时检查两者。
        """
        import urllib.request
        for host in ("127.0.0.1", "localhost", "::1"):
            try:
                url = f"http://{host}:{self.port}/json/version"
                # IPv6 地址需要用 [::1] 格式
                if host == "::1":
                    url = f"http://[::1]:{self.port}/json/version"
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=2) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                continue
        return False

    # ── 启动 Chrome ─────────────────────────────────────────────
    
    def start(self) -> bool:
        """启动 Chrome with remote debugging。

        如果 Chrome 已在调试端口运行，直接返回 True。
        否则启动新 Chrome 进程并等待端口就绪。

        注意：如果已有 Chrome 实例在运行（未开启调试端口），
        新启动的 Chrome 会把请求转发给已有实例然后退出，
        --remote-debugging-port 不会生效。
        此时需要使用 --user-data-dir 指定独立目录来启动第二个实例。

        Returns:
            bool: Chrome 是否可用（已运行或新启动成功）
        """
        if self._check_port():
            logger.info("Chrome 已运行在端口 %d", self.port)
            return True

        args = [
            self.executable,
            f"--remote-debugging-port={self.port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-default-apps",
            "--disable-extensions",
        ]

        if self.headless:
            args.append("--headless=new")

        # 使用独立的用户数据目录，避免与已有 Chrome 实例冲突
        # （已有实例会拦截新启动的 Chrome，导致调试端口不生效）
        user_data_dir = Path.home() / ".weavemind" / "chrome_profile"
        user_data_dir.mkdir(parents=True, exist_ok=True)
        args.append(f"--user-data-dir={user_data_dir}")

        try:
            self._process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("正在启动 Chrome (PID=%d, user-data-dir=%s)...", self._process.pid, user_data_dir)

            # 等待 Chrome 启动就绪（最多 10 秒）
            for i in range(20):
                time.sleep(0.5)
                if self._check_port():
                    logger.info("Chrome 已启动，调试端口: %d", self.port)
                    return True

            logger.error("Chrome 启动超时（10 秒内端口 %d 未就绪）", self.port)
            return False

        except FileNotFoundError:
            logger.error("Chrome 可执行文件不存在: %s", self.executable)
            return False
        except Exception as e:
            logger.error("启动 Chrome 失败: %s", e)
            return False

    # ── 停止 Chrome ─────────────────────────────────────────────

    def stop(self):
        """停止由本启动器启动的 Chrome 进程。"""
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
            logger.info("Chrome 已停止")

    # ── 状态查询 ────────────────────────────────────────────────

    def is_running(self) -> bool:
        """检查 Chrome 是否在调试端口运行。"""
        return self._check_port()

    @property
    def launched_by_us(self) -> bool:
        """是否由本启动器启动的 Chrome 进程。"""
        return self._process is not None
