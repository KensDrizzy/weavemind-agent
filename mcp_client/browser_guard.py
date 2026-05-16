"""BrowserGuard — 浏览器敏感页面保护和状态追踪。

职责：
1. 执行前检查：拦截对敏感页面（银行、支付等）的危险操作
2. 执行后更新：记录导航 URL、新标签页，检测登录页面
3. 登录检测：识别需要登录的页面，触发模式切换提示
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# 敏感页面 URL 模式
DEFAULT_SENSITIVE_PATTERNS = [
    r"(?i)(banking|bank\.|netbank|icbc|abchina|boc|ccb|cmbchina|spdb|cib|cebbank|psbc|bankcomm)",
    r"(?i)(pay(?:ment|pal)|alipay|wxpay|checkout|billing|checkout\.stripe)",
    r"(?i)(trading|invest|stock|fund|crypto|wallet)",
    r"(?i)(password|passwd|credential|secret|2fa|otp)",
    r"(?i)(admin|manage|dashboard|console|cpanel)",
    r"(?i)(login|signin|auth|sso|oauth|cas\.)",
    r"(?i)(private|confidential|internal)",
    r"(?i)(gov\.|government|tax|irs)",
    r"(?i)(medical|health|ehr|patient)",
    r"(?i)(hr|employee|payroll|salary)",
]

# 需要确认的写操作工具
WRITE_TOOLS = {
    "click",
    "type",
    "select_option",
    "fill",
    "hover",
    "drag",
    "key_press",
    "keyboard_press",
    "execute_js",
    "evaluate_js",
    "run_script",
}

# 导航类工具（需要记录 URL）
NAVIGATION_TOOLS = {"navigate_page", "navigate", "new_page", "go_back", "go_forward"}

# 新标签页工具
NEW_TAB_TOOLS = {"new_page"}

# 登录页面检测关键词
LOGIN_URL_KEYWORDS = ["login", "signin", "sign-in", "auth", "sso", "oauth", "cas", "登录", "登陆"]
LOGIN_CONTENT_PATTERNS = [
    r'(?i)<input[^>]*type\s*=\s*["\']?password["\']?',
    r'(?i)(sign\s*in|log\s*in|登录|登陆|signin|login)',
    r'(?i)<form[^>]*class\s*=\s*["\'][^"\']*login[^"\']*["\']',
    r'(?i)需要登录|请先登录|请登录|需要注册|请先注册|请先登入',
    r'(?i)401\s*(unauthorized|未授权)?',
    r'(?i)403\s*(forbidden|禁止访问|无权限)?',
    r'(?i)unauthorized|未授权|无权限访问|权限不足',
    r'(?i)access\s*denied|forbidden|permission\s*denied',
    r'(?i)请先登录后|登录后查看|登录后才能|登录后继续',
    r'(?i)authentication\s*required|login\s*required',
    r'(?i)账号.*登录|密码.*登录|手机.*登录|邮箱.*登录',
]


class BrowserGuard:
    """浏览器敏感页面保护和状态追踪。"""

    def __init__(self, custom_patterns_file: Optional[str] = None):
        self._patterns = list(DEFAULT_SENSITIVE_PATTERNS)
        self._compiled_patterns = [re.compile(p) for p in self._patterns]
        self._custom_patterns_file = custom_patterns_file

        # 运行时状态
        self.last_navigated_url: Optional[str] = None
        self.agent_opened_tabs: set = set()

        self._load_custom_patterns()

    def _load_custom_patterns(self):
        """加载用户自定义敏感页面模式。"""
        if not self._custom_patterns_file:
            return
        try:
            from pathlib import Path
            path = Path(self._custom_patterns_file)
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            self._patterns.append(line)
                            self._compiled_patterns.append(re.compile(line))
                logger.info("已加载 %d 条自定义敏感页面规则", len(self._patterns) - len(DEFAULT_SENSITIVE_PATTERNS))
        except Exception as e:
            logger.warning("加载自定义敏感页面规则失败: %s", e)

    def check_tool_use(
        self,
        tool_name: str,
        url: str = "",
        is_agent_page: bool = False,
    ) -> tuple:
        """执行前检查：判断工具调用是否被允许。

        Returns:
            (allowed, reason): allowed=True 表示允许，reason 为拒绝原因
        """
        # 敏感页面上的写操作需要额外确认
        if url and self._is_sensitive_url(url):
            if tool_name in WRITE_TOOLS:
                return False, f"敏感页面 {url} 上的写操作被拦截，请确认操作意图"

        return True, None

    def needs_confirmation(self, tool_name: str, url: str = "") -> tuple:
        """检查是否需要用户确认。

        Returns:
            (needs_confirm, reason): needs_confirm=True 表示需要确认
        """
        # 敏感页面上的写操作需要逐次确认
        if url and self._is_sensitive_url(url) and tool_name in WRITE_TOOLS:
            return True, f"敏感页面上的写操作需要逐次确认"

        return False, None

    def apply_after_execution(self, tool_name: str, args: dict, result: str):
        """执行后更新状态。

        记录导航 URL、新标签页 ID，供后续检查使用。
        """
        # 记录导航 URL
        if tool_name in NAVIGATION_TOOLS:
            url = args.get("url", "")
            if url:
                self.last_navigated_url = url

        # 记录新标签页
        if tool_name in NEW_TAB_TOOLS:
            page_id = self._extract_page_id(result)
            if page_id:
                self.agent_opened_tabs.add(page_id)

        # 从结果中提取导航后的 URL
        if tool_name in NAVIGATION_TOOLS:
            result_url = self._extract_url_from_result(result)
            if result_url:
                self.last_navigated_url = result_url

    def detect_login_page(self, page_content: str, url: str = "") -> bool:
        """检测页面是否需要登录。

        检测策略：
        1. URL 包含登录关键词
        2. 页面内容包含登录表单特征
        3. 页面内容包含明确的登录提示文本
        4. HTTP 状态码 401/403

        Args:
            page_content: 页面内容（文本或 HTML）
            url: 当前页面 URL

        Returns:
            bool: 是否检测到需要登录
        """
        if not page_content and not url:
            return False

        # URL 检测
        if url:
            url_lower = url.lower()
            for keyword in LOGIN_URL_KEYWORDS:
                if keyword in url_lower:
                    return True

        if not page_content:
            return False

        # 内容检测
        content_str = str(page_content).lower()
        for pattern in LOGIN_CONTENT_PATTERNS:
            if re.search(pattern, content_str):
                return True

        return False

    def _is_sensitive_url(self, url: str) -> bool:
        """检查 URL 是否匹配敏感页面模式。"""
        for pattern in self._compiled_patterns:
            if pattern.search(url):
                return True
        return False

    def _extract_page_id(self, result: str) -> Optional[str]:
        """从工具执行结果中提取页面 ID。"""
        if not result:
            return None
        # 匹配 chrome-devtools-mcp 返回的 page ID 格式
        match = re.search(r'page[-_]?([A-Za-z0-9_-]{6,})', str(result))
        if match:
            return match.group(0)
        return None

    def _extract_url_from_result(self, result: str) -> Optional[str]:
        """从工具执行结果中提取 URL。"""
        if not result:
            return None
        # 匹配结果中的 URL
        match = re.search(r'https?://[^\s<>"\')\]]+', str(result))
        if match:
            return match.group(0)
        return None