"""BrowserGuard — 浏览器行为保护机制。

职责：
1. 敏感页面检测与保护
2. 标签页关闭权限控制
3. 工具调用前的安全检查

策略设计原则：
- 读型操作（快照、截图）风险低，允许
- 写型操作（点击、填写、执行脚本）在敏感页面需强制确认
- 不关闭非 Agent 创建的标签页
"""

import fnmatch
import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional, Set

logger = logging.getLogger(__name__)


class PageRiskLevel(Enum):
    """页面风险等级。"""
    SAFE = "safe"
    SENSITIVE = "sensitive"   # 敏感页面，写操作需确认
    CRITICAL = "critical"     # 关键页面（支付等），应禁止访问


@dataclass
class PageCheckResult:
    """页面检查结果。"""
    risk_level: PageRiskLevel
    matched_pattern: Optional[str] = None
    message: str = ""


class BrowserGuard:
    """
    浏览器行为保护器。

    默认敏感页面规则（参考 PaiCLI）：
    - 银行/支付: *.bank.*, *.alipay.com/*, *.paypal.com/*
    - 云服务控制台: *.console.cloud.google.com/*, *.console.aws.amazon.com/*
    - 代码仓库设置: github.com/settings/*
    - 企业内部: *.feishu.cn/admin/*, *.larksuite.com/admin/*
    """

    # 默认敏感页面模式（通配符格式）
    DEFAULT_SENSITIVE_PATTERNS = [
        "*://*.bank.*/*",
        "*://*.alipay.com/*",
        "*://*.paypal.com/*",
        "*://*.stripe.com/*",
        "*://github.com/settings/*",
        "*://*.feishu.cn/admin/*",
        "*://*.larksuite.com/admin/*",
        "*://*.console.cloud.google.com/*",
        "*://*.console.aws.amazon.com/*",
        "*://*.portal.azure.com/*",
    ]

    # 写型工具（在这些工具上触发敏感检查）
    WRITE_TOOLS: Set[str] = {
        "click", "drag", "fill", "fill_form",
        "handle_dialog", "hover", "press_key",
        "resize_page", "upload_file", "evaluate_script",
        "type_text",
    }

    # 读型工具（不受敏感规则限制）
    READ_TOOLS: Set[str] = {
        "take_screenshot", "take_snapshot",
        "list_pages", "list_console_messages",
        "list_network_requests", "get_console_message",
        "get_network_request",
    }

    def __init__(self, custom_patterns_file: Optional[str] = None):
        """
        Args:
            custom_patterns_file: 用户自定义敏感页面规则文件路径
        """
        self._patterns: List[str] = self.DEFAULT_SENSITIVE_PATTERNS.copy()
        self._compiled: List[re.Pattern] = [self._glob_to_regex(p) for p in self._patterns]

        # 加载自定义规则
        if custom_patterns_file:
            self._load_custom_patterns(custom_patterns_file)

    def _glob_to_regex(self, pattern: str) -> re.Pattern:
        """将 glob 通配符转换为正则表达式。"""
        regex = fnmatch.translate(pattern)
        return re.compile(regex, re.IGNORECASE)

    def _load_custom_patterns(self, filepath: str):
        """从文件加载自定义规则。"""
        path = Path(filepath)
        if not path.exists():
            return

        try:
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    # 跳过注释和空行
                    if not line or line.startswith('#'):
                        continue
                    self._patterns.append(line)
                    self._compiled.append(self._glob_to_regex(line))
            logger.info("加载了 %d 条自定义敏感页面规则", len(self._patterns) - len(self.DEFAULT_SENSITIVE_PATTERNS))
        except IOError as e:
            logger.warning("加载自定义敏感规则失败: %s", e)

    def check_page(self, url: str) -> PageCheckResult:
        """
        检查 URL 的风险等级。

        Args:
            url: 页面 URL

        Returns:
            页面检查结果
        """
        if not url:
            return PageCheckResult(risk_level=PageRiskLevel.SAFE)

        for pattern, compiled in zip(self._patterns, self._compiled):
            if compiled.match(url):
                return PageCheckResult(
                    risk_level=PageRiskLevel.SENSITIVE,
                    matched_pattern=pattern,
                    message=f"URL 匹配敏感规则: {pattern}"
                )

        return PageCheckResult(risk_level=PageRiskLevel.SAFE)

    def check_tool_use(
        self,
        tool_name: str,
        url: str = "",
        is_agent_page: bool = False,
    ) -> tuple:
        """
        检查工具调用是否被允许。

        Args:
            tool_name: 工具名称
            url: 当前页面 URL
            is_agent_page: 是否 Agent 创建的标签页

        Returns:
            (是否允许, 阻止原因)
        """
        # 1. 检查是否是关闭页面操作
        if tool_name in ("close_page", "close"):
            if not is_agent_page:
                return False, "保护用户标签页：不能关闭非 Agent 创建的标签页"

        # 2. 检查敏感页面的写操作
        if tool_name in self.WRITE_TOOLS:
            result = self.check_page(url)
            if result.risk_level == PageRiskLevel.SENSITIVE:
                # 允许但需标记（由 HITL 处理确认）
                return True, result.message

        return True, None

    def needs_confirmation(self, tool_name: str, url: str) -> tuple:
        """
        判断工具调用是否需要用户确认。

        Returns:
            (是否需要确认, 确认提示信息)
        """
        if not url:
            return False, None

        result = self.check_page(url)

        if result.risk_level == PageRiskLevel.SENSITIVE and tool_name in self.WRITE_TOOLS:
            return True, f"⚠️ 敏感页面检测到，{tool_name} 操作需要确认\n{result.message}"

        return False, None

    def is_write_tool(self, tool_name: str) -> bool:
        """检查是否为写型工具。"""
        return tool_name in self.WRITE_TOOLS

    def is_read_tool(self, tool_name: str) -> bool:
        """检查是否为读型工具。"""
        return tool_name in self.READ_TOOLS
