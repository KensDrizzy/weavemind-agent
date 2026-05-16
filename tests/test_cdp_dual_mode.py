"""CDP 双模式功能测试 — BrowserGuard、MCPManager 模式切换、内置浏览器工具。"""

import asyncio
import pytest
import tempfile
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from mcp_client.browser_guard import BrowserGuard
from mcp_client.manager import MCPManager


# ── BrowserGuard 测试 ──────────────────────────────────────────


class TestBrowserGuard:
    """BrowserGuard 单元测试。"""

    def test_detect_login_url_keywords(self):
        guard = BrowserGuard()
        assert guard.detect_login_page("", "https://github.com/login") is True
        assert guard.detect_login_page("", "https://example.com/signin") is True
        assert guard.detect_login_page("", "https://example.com/auth") is True
        assert guard.detect_login_page("", "https://example.com/home") is False

    def test_detect_login_content_password(self):
        guard = BrowserGuard()
        content = '<input type="password"><input name="email">'
        assert guard.detect_login_page(content) is True

    def test_detect_login_chinese(self):
        guard = BrowserGuard()
        assert guard.detect_login_page("请登录您的账号") is True
        assert guard.detect_login_page("欢迎回来") is False

    def test_detect_login_401_403(self):
        guard = BrowserGuard()
        assert guard.detect_login_page("401 unauthorized") is True
        assert guard.detect_login_page("403 forbidden") is True

    def test_apply_after_execution_navigate(self):
        guard = BrowserGuard()
        guard.apply_after_execution("navigate_page", {"url": "https://example.com/page"}, "")
        assert guard.last_navigated_url == "https://example.com/page"

    def test_apply_after_execution_new_page(self):
        guard = BrowserGuard()
        guard.apply_after_execution("new_page", {"url": "https://example.com"}, "page-abc123 opened")
        assert "page-abc123" in guard.agent_opened_tabs

    def test_apply_after_execution_result_url(self):
        guard = BrowserGuard()
        guard.apply_after_execution("navigate_page", {"url": "http://redirect.com"},
                                    "Navigated to https://final.com/page")
        assert guard.last_navigated_url == "https://final.com/page"

    def test_needs_confirmation_sensitive_write(self):
        guard = BrowserGuard()
        needs, msg = guard.needs_confirmation("click", "https://www.alipay.com/pay")
        assert needs is True

    def test_needs_confirmation_safe_write(self):
        guard = BrowserGuard()
        needs, msg = guard.needs_confirmation("click", "https://example.com/page")
        assert needs is False

    def test_needs_confirmation_read_on_sensitive(self):
        guard = BrowserGuard()
        needs, msg = guard.needs_confirmation("take_screenshot", "https://www.alipay.com/pay")
        assert needs is False

    def test_check_tool_use_blocks_sensitive_write(self):
        guard = BrowserGuard()
        allowed, reason = guard.check_tool_use("click", "https://www.alipay.com/pay")
        assert allowed is False

    def test_check_tool_use_allows_safe_page(self):
        guard = BrowserGuard()
        allowed, reason = guard.check_tool_use("click", "https://example.com/page")
        assert allowed is True

    def test_empty_url_safe(self):
        guard = BrowserGuard()
        assert guard.detect_login_page("", "") is False

    def test_custom_patterns_file(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("# 自定义规则\n")
            f.write("*://*.mycompany.com/*\n")
            f.name
        try:
            guard = BrowserGuard(custom_patterns_file=f.name)
            assert guard.detect_login_page("", "https://app.mycompany.com/dashboard") is False
        finally:
            os.unlink(f.name)


# ── MCPManager 模式切换测试 ──────────────────────────────────


class TestMCPManagerModeSwitch:
    """MCPManager 模式切换测试。"""

    def test_default_mode_is_isolated(self):
        manager = MCPManager(servers_config={})
        assert manager.get_chrome_mode() == "isolated"
        assert manager.is_isolated_mode() is True
        assert manager.is_shared_mode() is False

    def test_browser_guard_initialized(self):
        manager = MCPManager(servers_config={})
        guard = manager.get_browser_guard()
        assert isinstance(guard, BrowserGuard)

    def test_isolated_args_constant(self):
        assert "--isolated" in MCPManager.ISOLATED_ARGS
        assert "--autoConnect" in MCPManager.SHARED_AUTOCONNECT_ARGS

    def test_get_browser_status_text(self):
        manager = MCPManager(servers_config={})
        status = manager.get_browser_status_text()
        assert "isolated" in status

    def test_detect_need_login_via_manager(self):
        manager = MCPManager(servers_config={})
        assert manager.detect_need_login("请登录", "https://example.com/login") is True
        assert manager.detect_need_login("正常内容", "https://example.com/page") is False

    def test_apply_browser_after_execution_via_manager(self):
        manager = MCPManager(servers_config={})
        manager.apply_browser_after_execution("navigate_page", {"url": "https://example.com"}, "")
        guard = manager.get_browser_guard()
        assert guard.last_navigated_url == "https://example.com"


# ── 内置浏览器工具测试 ──────────────────────────────────────────


class TestBrowserTools:
    """内置浏览器工具测试。"""

    def test_create_browser_connect_tool(self):
        from mcp_client.browser_tools import create_browser_connect_tool
        mcp_manager = MCPManager(servers_config={})
        tool = create_browser_connect_tool(mcp_manager)
        assert tool.name == "browser_connect"
        assert "登录态" in tool.description

    def test_create_browser_disconnect_tool(self):
        from mcp_client.browser_tools import create_browser_disconnect_tool
        mcp_manager = MCPManager(servers_config={})
        tool = create_browser_disconnect_tool(mcp_manager)
        assert tool.name == "browser_disconnect"

    def test_create_browser_status_tool(self):
        from mcp_client.browser_tools import create_browser_status_tool
        mcp_manager = MCPManager(servers_config={})
        tool = create_browser_status_tool(mcp_manager)
        assert tool.name == "browser_status"

    def test_create_all_browser_tools(self):
        from mcp_client.browser_tools import create_all_browser_tools
        mcp_manager = MCPManager(servers_config={})
        tools = create_all_browser_tools(mcp_manager)
        assert len(tools) == 3
        assert tools[0].name == "browser_connect"
        assert tools[1].name == "browser_disconnect"
        assert tools[2].name == "browser_status"


# ── PermissionPolicy Chrome 扩展测试 ──────────────────────────


class TestPermissionPolicyChrome:
    """PermissionPolicy Chrome 扩展测试。"""

    def test_set_browser_guard(self):
        from permissions.policy import PermissionPolicy
        policy = PermissionPolicy()
        guard = BrowserGuard()
        policy.set_browser_guard(guard)
        assert policy._browser_guard is guard

    def test_needs_chrome_confirmation_no_guard(self):
        from permissions.policy import PermissionPolicy
        policy = PermissionPolicy()
        needs, msg = policy.needs_chrome_confirmation("click", "https://www.alipay.com/pay")
        assert needs is False

    def test_needs_chrome_confirmation_with_guard(self):
        from permissions.policy import PermissionPolicy
        policy = PermissionPolicy()
        policy.set_browser_guard(BrowserGuard())
        needs, msg = policy.needs_chrome_confirmation("click", "https://www.alipay.com/pay")
        assert needs is True

    def test_browser_connect_needs_confirmation(self):
        from permissions.policy import PermissionPolicy, BROWSER_CONNECT_TOOLS
        policy = PermissionPolicy()
        assert "browser_connect" in BROWSER_CONNECT_TOOLS
        assert policy.needs_confirmation("browser_connect", "default") is True