"""AutoConnectDiscovery — Chrome DevTools autoConnect 自动发现机制。

原理：
1. 用户开启 Chrome 远程调试后，Chrome 将端口号和 WebSocket 路径写入 DevToolsActivePort 文件
2. autoConnect 读取该文件获取连接信息
3. 不扫描固定端口，避免冲突和误连

文件位置：
- macOS: ~/Library/Application Support/Google/Chrome/DevToolsActivePort
- Linux: ~/.config/google-chrome/DevToolsActivePort
- Windows: %LOCALAPPDATA%/Google/Chrome/User Data/DevToolsActivePort

文件格式（两行）：
  \\d+                    # 端口号（随机分配）
  /devtools/browser/...  # WebSocket 路径
"""

import logging
import platform
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class AutoConnectDiscovery:
    """Chrome DevTools 自动发现器。"""

    # 各操作系统的默认 Chrome 用户数据目录
    DEFAULT_PROFILE_PATHS = {
        "Darwin": Path.home() / "Library/Application Support/Google/Chrome",
        "Linux": Path.home() / ".config/google-chrome",
        "Windows": Path(Path.home(), "AppData", "Local", "Google", "Chrome", "User Data"),
    }

    DEVTOOLS_PORT_FILENAME = "DevToolsActivePort"

    def __init__(self, profile_path: Optional[Path] = None, channel: str = "stable"):
        """
        Args:
            profile_path: Chrome 用户数据目录，None 则使用默认路径
            channel: Chrome 通道 (stable/beta/dev/canary)，影响路径
        """
        self._profile_path = profile_path or self._get_default_profile_path(channel)

    def _get_default_profile_path(self, channel: str) -> Path:
        """获取默认 Chrome 用户数据目录。"""
        system = platform.system()
        base_path = self.DEFAULT_PROFILE_PATHS.get(system)

        if not base_path:
            raise RuntimeError(f"不支持的操作系统: {system}")

        # 处理不同 channel 的路径差异
        if channel != "stable":
            if system == "Darwin":
                base_path = base_path.parent / f"Google Chrome {channel.capitalize()}"
            elif system == "Linux":
                base_path = Path.home() / f".config/google-chrome-{channel}"

        return base_path

    def discover(self) -> Optional[Tuple[int, str]]:
        """
        发现 Chrome DevTools 连接信息。

        Returns:
            (端口号, WebSocket路径) 或 None
        """
        port_file = self._profile_path / self.DEVTOOLS_PORT_FILENAME

        if not port_file.exists():
            return None

        try:
            content = port_file.read_text().strip()
            lines = [line.strip() for line in content.split('\n') if line.strip()]

            if len(lines) < 2:
                return None

            port = int(lines[0])
            ws_path = lines[1]

            return port, ws_path

        except (ValueError, IOError, OSError) as e:
            logger.debug("读取 DevToolsActivePort 失败: %s", e)
            return None

    def get_browser_url(self) -> Optional[str]:
        """
        获取用于 --browserUrl 参数的完整 HTTP URL。

        Returns:
            http://localhost:{port} 或 None
        """
        result = self.discover()
        if not result:
            return None

        port, _ws_path = result
        # chrome-devtools-mcp 的 --browserUrl 接受 HTTP URL（内部自行升级为 WebSocket）
        return f"http://localhost:{port}"

    def is_remote_debugging_enabled(self) -> bool:
        """检查 Chrome 是否开启了远程调试。"""
        return self.discover() is not None
