"""CDP 双模式功能测试 — AutoConnectDiscovery、BrowserGuard、ChromeSessionManager。"""

import pytest
import tempfile
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from mcp_client.auto_connect import AutoConnectDiscovery
from mcp_client.browser_guard import BrowserGuard, PageRiskLevel, PageCheckResult
from mcp_client.session_manager import ChromeSessionManager, ChromeMode, ChromeSession


# ── AutoConnectDiscovery 测试 ──────────────────────────────────


class TestAutoConnectDiscovery:
    """AutoConnectDiscovery 单元测试。"""

    def test_discover_no_file(self):
        """DevToolsActivePort 文件不存在时应返回 None。"""
        discovery = AutoConnectDiscovery(profile_path=Path("/nonexistent/path"))
        assert discovery.discover() is None

    def test_discover_valid_file(self):
        """有效 DevToolsActivePort 文件应返回 (port, ws_path)。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            port_file = Path(tmpdir) / "DevToolsActivePort"
            port_file.write_text("9222\n/devtools/browser/abc-123\n")

            discovery = AutoConnectDiscovery(profile_path=Path(tmpdir))
            result = discovery.discover()

            assert result is not None
            assert result[0] == 9222
            assert result[1] == "/devtools/browser/abc-123"

    def test_discover_empty_file(self):
        """空文件应返回 None。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            port_file = Path(tmpdir) / "DevToolsActivePort"
            port_file.write_text("")

            discovery = AutoConnectDiscovery(profile_path=Path(tmpdir))
            assert discovery.discover() is None

    def test_discover_single_line_file(self):
        """只有一行的文件应返回 None（需要至少两行）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            port_file = Path(tmpdir) / "DevToolsActivePort"
            port_file.write_text("9222\n")

            discovery = AutoConnectDiscovery(profile_path=Path(tmpdir))
            assert discovery.discover() is None

    def test_get_browser_url(self):
        """get_browser_url 应返回 http://localhost:{port}。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            port_file = Path(tmpdir) / "DevToolsActivePort"
            port_file.write_text("9333\n/devtools/browser/xyz\n")

            discovery = AutoConnectDiscovery(profile_path=Path(tmpdir))
            url = discovery.get_browser_url()

            assert url == "http://localhost:9333"

    def test_get_browser_url_no_file(self):
        """文件不存在时 get_browser_url 应返回 None。"""
        discovery = AutoConnectDiscovery(profile_path=Path("/nonexistent"))
        assert discovery.get_browser_url() is None

    def test_is_remote_debugging_enabled(self):
        """文件存在时 is_remote_debugging_enabled 应返回 True。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            port_file = Path(tmpdir) / "DevToolsActivePort"
            port_file.write_text("9222\n/devtools/browser/test\n")

            discovery = AutoConnectDiscovery(profile_path=Path(tmpdir))
            assert discovery.is_remote_debugging_enabled() is True

    def test_is_remote_debugging_not_enabled(self):
        """文件不存在时 is_remote_debugging_enabled 应返回 False。"""
        discovery = AutoConnectDiscovery(profile_path=Path("/nonexistent"))
        assert discovery.is_remote_debugging_enabled() is False

    def test_discover_invalid_port(self):
        """端口号非数字时应返回 None。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            port_file = Path(tmpdir) / "DevToolsActivePort"
            port_file.write_text("not_a_number\n/devtools/browser/test\n")

            discovery = AutoConnectDiscovery(profile_path=Path(tmpdir))
            assert discovery.discover() is None


# ── BrowserGuard 测试 ──────────────────────────────────────────


class TestBrowserGuard:
    """BrowserGuard 单元测试。"""

    def test_safe_page(self):
        """普通 URL 应返回 SAFE。"""
        guard = BrowserGuard()
        result = guard.check_page("https://example.com/page")
        assert result.risk_level == PageRiskLevel.SAFE

    def test_sensitive_alipay(self):
        """支付宝 URL 应返回 SENSITIVE。"""
        guard = BrowserGuard()
        result = guard.check_page("https://www.alipay.com/pay")
        assert result.risk_level == PageRiskLevel.SENSITIVE
        assert "alipay" in result.matched_pattern

    def test_sensitive_github_settings(self):
        """GitHub 设置页面应返回 SENSITIVE。"""
        guard = BrowserGuard()
        result = guard.check_page("https://github.com/settings/security")
        assert result.risk_level == PageRiskLevel.SENSITIVE

    def test_sensitive_aws_console(self):
        """AWS 控制台应返回 SENSITIVE。"""
        guard = BrowserGuard()
        result = guard.check_page("https://us-east-1.console.aws.amazon.com/ec2")
        assert result.risk_level == PageRiskLevel.SENSITIVE

    def test_safe_github_repo(self):
        """GitHub 仓库页面应返回 SAFE（不是 settings）。"""
        guard = BrowserGuard()
        result = guard.check_page("https://github.com/user/repo")
        assert result.risk_level == PageRiskLevel.SAFE

    def test_check_tool_use_close_user_page(self):
        """关闭用户标签页应被阻止。"""
        guard = BrowserGuard()
        allowed, reason = guard.check_tool_use("close_page", "", is_agent_page=False)
        assert allowed is False
        assert "保护用户标签页" in reason

    def test_check_tool_use_close_agent_page(self):
        """关闭 Agent 自己的标签页应被允许。"""
        guard = BrowserGuard()
        allowed, reason = guard.check_tool_use("close_page", "", is_agent_page=True)
        assert allowed is True

    def test_check_tool_use_write_on_safe_page(self):
        """安全页面上的写操作应被允许。"""
        guard = BrowserGuard()
        allowed, reason = guard.check_tool_use("click", "https://example.com/page")
        assert allowed is True

    def test_check_tool_use_write_on_sensitive_page(self):
        """敏感页面上的写操作应被允许但需标记。"""
        guard = BrowserGuard()
        allowed, reason = guard.check_tool_use("click", "https://www.alipay.com/pay")
        assert allowed is True
        assert reason is not None  # 有标记信息

    def test_needs_confirmation_sensitive_write(self):
        """敏感页面写操作需要确认。"""
        guard = BrowserGuard()
        needs, msg = guard.needs_confirmation("click", "https://www.alipay.com/pay")
        assert needs is True
        assert "敏感页面" in msg

    def test_needs_confirmation_safe_write(self):
        """安全页面写操作不需要确认。"""
        guard = BrowserGuard()
        needs, msg = guard.needs_confirmation("click", "https://example.com/page")
        assert needs is False

    def test_needs_confirmation_read_on_sensitive(self):
        """敏感页面读操作不需要确认。"""
        guard = BrowserGuard()
        needs, msg = guard.needs_confirmation("take_screenshot", "https://www.alipay.com/pay")
        assert needs is False

    def test_custom_patterns_file(self):
        """自定义规则文件应被加载。"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("# 自定义规则\n")
            f.write("*://*.mycompany.com/*\n")
            f.write("*://admin.internal.com/*\n")
            f.name

        try:
            guard = BrowserGuard(custom_patterns_file=f.name)
            # 自定义规则应被加载
            result = guard.check_page("https://app.mycompany.com/dashboard")
            assert result.risk_level == PageRiskLevel.SENSITIVE
        finally:
            os.unlink(f.name)

    def test_custom_patterns_file_not_found(self):
        """规则文件不存在时不应报错。"""
        guard = BrowserGuard(custom_patterns_file="/nonexistent/patterns.txt")
        # 应正常工作，只有默认规则
        result = guard.check_page("https://www.alipay.com/pay")
        assert result.risk_level == PageRiskLevel.SENSITIVE

    def test_empty_url_safe(self):
        """空 URL 应返回 SAFE。"""
        guard = BrowserGuard()
        result = guard.check_page("")
        assert result.risk_level == PageRiskLevel.SAFE

    def test_is_write_tool(self):
        """写型工具判断。"""
        guard = BrowserGuard()
        assert guard.is_write_tool("click") is True
        assert guard.is_write_tool("fill") is True
        assert guard.is_write_tool("evaluate_script") is True
        assert guard.is_write_tool("take_screenshot") is False
        assert guard.is_write_tool("list_pages") is False

    def test_is_read_tool(self):
        """读型工具判断。"""
        guard = BrowserGuard()
        assert guard.is_read_tool("take_screenshot") is True
        assert guard.is_read_tool("take_snapshot") is True
        assert guard.is_read_tool("click") is False


# ── ChromeSessionManager 测试 ──────────────────────────────────


class TestChromeSessionManager:
    """ChromeSessionManager 单元测试。"""

    def test_initial_state(self):
        """初始状态应为 None。"""
        manager = ChromeSessionManager(mcp_manager=MagicMock())
        assert manager.current_mode is None
        assert manager.is_shared is False
        assert manager.is_isolated is False

    def test_start_isolated(self):
        """启动 isolated 模式应设置会话状态。"""
        manager = ChromeSessionManager(mcp_manager=MagicMock())
        # start_isolated 是 async，用 asyncio.run 测试
        import asyncio
        success = asyncio.run(manager.start_isolated())
        assert success is True
        assert manager.current_mode == ChromeMode.ISOLATED
        assert manager.is_isolated is True
        assert manager.is_shared is False

    def test_start_isolated_idempotent(self):
        """重复启动 isolated 模式应幂等。"""
        manager = ChromeSessionManager(mcp_manager=MagicMock())
        import asyncio
        asyncio.run(manager.start_isolated())
        success = asyncio.run(manager.start_isolated())
        assert success is True
        assert manager.current_mode == ChromeMode.ISOLATED

    def test_record_and_check_agent_page(self):
        """记录和检查 Agent 标签页。"""
        manager = ChromeSessionManager(mcp_manager=MagicMock())
        manager._session = ChromeSession(mode=ChromeMode.ISOLATED)

        assert manager.is_agent_page("page-1") is False
        manager.record_agent_page("page-1")
        assert manager.is_agent_page("page-1") is True
        assert manager.is_agent_page("page-2") is False

    def test_detect_need_login_url(self):
        """URL 包含 login 关键词应检测为需要登录。"""
        manager = ChromeSessionManager(mcp_manager=MagicMock())
        assert manager.detect_need_login("", "https://github.com/login") is True
        assert manager.detect_need_login("", "https://example.com/signin") is True
        assert manager.detect_need_login("", "https://example.com/auth") is True
        assert manager.detect_need_login("", "https://example.com/home") is False

    def test_detect_need_login_content(self):
        """页面内容包含登录特征应检测为需要登录。"""
        manager = ChromeSessionManager(mcp_manager=MagicMock())
        content = '<input type="password"><input name="email">'
        assert manager.detect_need_login(content) is True

    def test_detect_need_login_chinese(self):
        """中文登录页面应被检测。"""
        manager = ChromeSessionManager(mcp_manager=MagicMock())
        assert manager.detect_need_login("请登录您的账号") is True
        assert manager.detect_need_login("欢迎回来") is False

    def test_get_status_text(self):
        """状态文本应包含模式信息。"""
        manager = ChromeSessionManager(mcp_manager=MagicMock())
        manager._session = ChromeSession(mode=ChromeMode.ISOLATED)
        text = manager.get_status_text()
        assert "isolated" in text

        manager._session = ChromeSession(
            mode=ChromeMode.SHARED,
            browser_url="http://localhost:9222",
        )
        text = manager.get_status_text()
        assert "shared" in text
        assert "localhost:9222" in text

    def test_get_status_text_no_session(self):
        """无会话时状态文本应为'未启动'。"""
        manager = ChromeSessionManager(mcp_manager=MagicMock())
        assert manager.get_status_text() == "未启动"


# ── ChromeSession 数据模型测试 ──────────────────────────────────


class TestChromeSession:
    """ChromeSession 数据模型测试。"""

    def test_default_values(self):
        """默认值应正确。"""
        session = ChromeSession(mode=ChromeMode.ISOLATED)
        assert session.user_data_dir is None
        assert session.browser_url is None
        assert session.page_ids == set()

    def test_page_ids_mutability(self):
        """page_ids 应可变。"""
        session = ChromeSession(mode=ChromeMode.ISOLATED)
        session.page_ids.add("page-1")
        assert "page-1" in session.page_ids


# ── PermissionPolicy Chrome 扩展测试 ──────────────────────────


class TestPermissionPolicyChrome:
    """PermissionPolicy Chrome 扩展测试。"""

    def test_set_browser_guard(self):
        """set_browser_guard 应正确注入。"""
        from permissions.policy import PermissionPolicy
        policy = PermissionPolicy()
        guard = BrowserGuard()
        policy.set_browser_guard(guard)
        assert policy._browser_guard is guard

    def test_needs_chrome_confirmation_no_guard(self):
        """无 BrowserGuard 时不需要确认。"""
        from permissions.policy import PermissionPolicy
        policy = PermissionPolicy()
        needs, msg = policy.needs_chrome_confirmation("click", "https://www.alipay.com/pay")
        assert needs is False

    def test_needs_chrome_confirmation_with_guard(self):
        """有 BrowserGuard 时敏感页面写操作需要确认。"""
        from permissions.policy import PermissionPolicy
        policy = PermissionPolicy()
        policy.set_browser_guard(BrowserGuard())
        needs, msg = policy.needs_chrome_confirmation("click", "https://www.alipay.com/pay")
        assert needs is True
        assert "敏感页面" in msg

    def test_needs_chrome_confirmation_safe_tool(self):
        """读型工具不需要确认。"""
        from permissions.policy import PermissionPolicy
        policy = PermissionPolicy()
        policy.set_browser_guard(BrowserGuard())
        needs, msg = policy.needs_chrome_confirmation("take_screenshot", "https://www.alipay.com/pay")
        assert needs is False

    def test_needs_chrome_confirmation_safe_page(self):
        """安全页面写操作不需要额外确认。"""
        from permissions.policy import PermissionPolicy
        policy = PermissionPolicy()
        policy.set_browser_guard(BrowserGuard())
        needs, msg = policy.needs_chrome_confirmation("click", "https://example.com/page")
        assert needs is False


# ── MCPManager CDP 扩展测试 ──────────────────────────────────


class TestMCPManagerCDP:
    """MCPManager CDP 扩展测试。"""

    def test_init_creates_browser_guard(self):
        """初始化时应创建 BrowserGuard。"""
        from mcp_client.manager import MCPManager
        manager = MCPManager(servers_config={})
        assert manager._browser_guard is not None

    def test_get_browser_guard(self):
        """get_browser_guard 应返回 BrowserGuard 实例。"""
        from mcp_client.manager import MCPManager
        manager = MCPManager(servers_config={})
        guard = manager.get_browser_guard()
        assert isinstance(guard, BrowserGuard)

    def test_get_session_manager_initially_none(self):
        """初始时 session_manager 应为 None。"""
        from mcp_client.manager import MCPManager
        manager = MCPManager(servers_config={})
        assert manager.get_session_manager() is None

    def test_get_chrome_mode_default(self):
        """默认 Chrome 模式应为 isolated。"""
        from mcp_client.manager import MCPManager
        manager = MCPManager(servers_config={})
        assert manager.get_chrome_mode() == "isolated"

    def test_is_shared_mode_default(self):
        """默认不应为 shared 模式。"""
        from mcp_client.manager import MCPManager
        manager = MCPManager(servers_config={})
        assert manager.is_shared_mode() is False

    def test_check_browser_tool_no_guard(self):
        """无 BrowserGuard 时应允许所有操作。"""
        from mcp_client.manager import MCPManager
        manager = MCPManager(servers_config={})
        manager._browser_guard = None
        allowed, reason = manager.check_browser_tool("close_page", "", "page-1")
        assert allowed is True

    def test_check_browser_tool_close_user_page(self):
        """关闭用户标签页应被阻止。"""
        from mcp_client.manager import MCPManager
        manager = MCPManager(servers_config={})
        allowed, reason = manager.check_browser_tool("close_page", "", "page-1")
        assert allowed is False
        assert "保护用户标签页" in reason

    def test_detect_need_login_no_session(self):
        """无 session_manager 时不应检测登录。"""
        from mcp_client.manager import MCPManager
        manager = MCPManager(servers_config={})
        assert manager.detect_need_login("login page") is False


# ── AgentLoop 自动切换测试 ──────────────────────────────────────


class TestAutoSwitchOnLogin:
    """AgentLoop._try_auto_switch_on_login 单元测试。"""

    def _make_agent_loop(self, mcp_manager=None):
        """创建带 mock MCPManager 的 AgentLoop。"""
        from core.agent_loop import AgentLoop
        from permissions.policy import PermissionPolicy
        from tools.registry import ToolRegistry

        tool_registry = ToolRegistry()
        policy = PermissionPolicy()

        loop = AgentLoop(
            tool_registry=tool_registry,
            permission_policy=policy,
            mcp_manager=mcp_manager,
        )
        return loop

    def test_non_chrome_tool_no_switch(self):
        """非 Chrome 工具不应触发自动切换。"""
        mcp_manager = MagicMock()
        mcp_manager.is_shared_mode.return_value = False
        loop = self._make_agent_loop(mcp_manager)

        switched, msg = loop._try_auto_switch_on_login(
            "WebSearch", "some result", {}
        )
        assert switched is False
        assert msg == ""

    def test_shared_mode_no_switch(self):
        """已处于 shared 模式不应触发切换。"""
        mcp_manager = MagicMock()
        mcp_manager.is_shared_mode.return_value = True
        loop = self._make_agent_loop(mcp_manager)

        switched, msg = loop._try_auto_switch_on_login(
            "navigate_page", "login page content", {"url": "https://example.com/login"}
        )
        assert switched is False
        assert msg == ""

    def test_already_switched_no_repeat(self):
        """本轮已切换过不应重复切换。"""
        mcp_manager = MagicMock()
        mcp_manager.is_shared_mode.return_value = False
        loop = self._make_agent_loop(mcp_manager)
        loop._auto_switched_to_shared = True

        switched, msg = loop._try_auto_switch_on_login(
            "navigate_page", "login page content", {"url": "https://example.com/login"}
        )
        assert switched is False
        assert msg == ""

    def test_no_login_detected_no_switch(self):
        """未检测到登录页不应触发切换。"""
        mcp_manager = MagicMock()
        mcp_manager.is_shared_mode.return_value = False
        mcp_manager.detect_need_login.return_value = False
        loop = self._make_agent_loop(mcp_manager)

        switched, msg = loop._try_auto_switch_on_login(
            "navigate_page", "normal page content", {"url": "https://example.com"}
        )
        assert switched is False
        assert msg == ""

    def test_auto_switch_disabled_in_config(self):
        """配置禁用自动切换时不应触发。"""
        mcp_manager = MagicMock()
        mcp_manager.is_shared_mode.return_value = False
        mcp_manager.detect_need_login.return_value = True
        loop = self._make_agent_loop(mcp_manager)

        with patch("settings.get", return_value=False):
            switched, msg = loop._try_auto_switch_on_login(
                "navigate_page", "login page content", {"url": "https://example.com/login"}
            )
            assert switched is False
            assert msg == ""

    def test_no_remote_debugging_no_switch(self):
        """用户 Chrome 未开启远程调试时不应切换（但检测到了登录页）。"""
        mcp_manager = MagicMock()
        mcp_manager.is_shared_mode.return_value = False
        mcp_manager.detect_need_login.return_value = True
        loop = self._make_agent_loop(mcp_manager)

        with patch("mcp_client.auto_connect.AutoConnectDiscovery") as mock_discovery_cls:
            mock_discovery = MagicMock()
            mock_discovery.is_remote_debugging_enabled.return_value = False
            mock_discovery_cls.return_value = mock_discovery

            with patch("settings.get", return_value=True):
                switched, msg = loop._try_auto_switch_on_login(
                    "navigate_page", "login page content", {"url": "https://example.com/login"}
                )
                assert switched is False
                assert msg == ""  # 不返回提示信息，只记录日志

    def test_auto_switch_success(self):
        """检测到登录页且 Chrome 远程调试可用时应自动切换。"""
        mcp_manager = MagicMock()
        mcp_manager.is_shared_mode.return_value = False
        mcp_manager.detect_need_login.return_value = True
        mcp_manager.switch_to_shared.return_value = True
        mcp_manager.get_connection.return_value = MagicMock(_loop=MagicMock(is_running=MagicMock(return_value=False)))

        loop = self._make_agent_loop(mcp_manager)

        with patch("mcp_client.auto_connect.AutoConnectDiscovery") as mock_discovery_cls:
            mock_discovery = MagicMock()
            mock_discovery.is_remote_debugging_enabled.return_value = True
            mock_discovery_cls.return_value = mock_discovery

            with patch("asyncio.run", return_value=True):
                with patch.object(loop, "_refresh_tools_after_switch"):
                    switched, msg = loop._try_auto_switch_on_login(
                        "navigate_page", "login page content", {"url": "https://example.com/login"}
                    )
                    assert switched is True
                    assert "自动切换" in msg
                    assert "shared 模式" in msg
                    assert loop._auto_switched_to_shared is True

    def test_auto_switch_failure(self):
        """切换失败时应返回提示信息。"""
        mcp_manager = MagicMock()
        mcp_manager.is_shared_mode.return_value = False
        mcp_manager.detect_need_login.return_value = True
        mcp_manager.switch_to_shared.return_value = False
        mcp_manager.get_connection.return_value = MagicMock(_loop=MagicMock(is_running=MagicMock(return_value=False)))

        loop = self._make_agent_loop(mcp_manager)

        with patch("mcp_client.auto_connect.AutoConnectDiscovery") as mock_discovery_cls:
            mock_discovery = MagicMock()
            mock_discovery.is_remote_debugging_enabled.return_value = True
            mock_discovery_cls.return_value = mock_discovery

            with patch("asyncio.run", return_value=False):
                switched, msg = loop._try_auto_switch_on_login(
                    "navigate_page", "login page content", {"url": "https://example.com/login"}
                )
                assert switched is False
                assert "切换失败" in msg

    def test_no_mcp_manager_no_switch(self):
        """无 MCPManager 时不应触发切换。"""
        loop = self._make_agent_loop(mcp_manager=None)

        switched, msg = loop._try_auto_switch_on_login(
            "navigate_page", "login page content", {"url": "https://example.com/login"}
        )
        assert switched is False
        assert msg == ""

    def test_url_from_tool_args(self):
        """工具参数中的 url 应被提取用于登录页检测。"""
        mcp_manager = MagicMock()
        mcp_manager.is_shared_mode.return_value = False
        # URL 包含 login 关键词，detect_need_login 应返回 True
        mcp_manager.detect_need_login.return_value = True
        loop = self._make_agent_loop(mcp_manager)

        with patch("mcp_client.auto_connect.AutoConnectDiscovery") as mock_discovery_cls:
            mock_discovery = MagicMock()
            mock_discovery.is_remote_debugging_enabled.return_value = True
            mock_discovery_cls.return_value = mock_discovery

            with patch("asyncio.run", return_value=True):
                with patch.object(loop, "_auto_switch_to_shared", return_value=True) as mock_switch:
                    with patch("settings.get", return_value=True):
                        switched, msg = loop._try_auto_switch_on_login(
                            "navigate_page", "page content", {"url": "https://github.com/login"}
                        )
                        # detect_need_login 应被调用，且 url 参数来自 tool_args
                        mcp_manager.detect_need_login.assert_called_once_with(
                            "page content", "https://github.com/login"
                        )
