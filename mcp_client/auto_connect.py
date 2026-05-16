"""AutoConnectDiscovery — 已废弃。

Chrome 144+ 的 --autoConnect 参数原生支持自动发现用户 Chrome，
不再需要手动读取 DevToolsActivePort 文件。
保留此文件仅为向后兼容，新代码请使用 MCPManager.switch_to_shared()。
"""

import logging
import warnings
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class AutoConnectDiscovery:
    """已废弃：Chrome 144+ 的 --autoConnect 原生支持自动发现。"""

    def __init__(self, profile_path: Optional[Path] = None, channel: str = "stable"):
        warnings.warn(
            "AutoConnectDiscovery 已废弃，请使用 MCPManager.switch_to_shared()",
            DeprecationWarning,
            stacklevel=2,
        )

    def discover(self) -> Optional[Tuple[int, str]]:
        return None

    def get_browser_url(self) -> Optional[str]:
        return None

    def is_remote_debugging_enabled(self) -> bool:
        return False
