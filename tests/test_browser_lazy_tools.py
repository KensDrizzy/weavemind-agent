"""浏览器工具按需挂载测试（mcp.lazy_browser_tools）。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import HumanMessage

import settings
from core.agent_loop import AgentLoop


def _tool(name):
    return SimpleNamespace(name=name)


def _make_loop(tools, mounted=False):
    loop = AgentLoop.__new__(AgentLoop)
    loop._browser_tools_mounted = mounted
    loop._all_available_tools = list(tools)
    loop.tools = list(tools)
    loop.mcp_manager = None
    loop.llm = MagicMock()
    loop.llm.bind_tools = MagicMock(return_value="bound")
    return loop


@pytest.fixture(autouse=True)
def _lazy_on(monkeypatch):
    """确保 lazy_browser_tools 开关为 True，不受本地 config.yaml 影响。"""
    real_get = settings.get

    def fake_get(key, default=None):
        if key == "mcp.lazy_browser_tools":
            return True
        return real_get(key, default)

    monkeypatch.setattr(settings, "get", fake_get)


class TestApplyBrowserToolFilter:
    def test_hides_chrome_tools_when_not_mounted(self):
        tools = [_tool("Read"), _tool("click"), _tool("navigate_page"),
                 _tool("take_snapshot"), _tool("browser_connect")]
        loop = _make_loop(tools, mounted=False)
        filtered = loop._apply_browser_tool_filter(loop._all_available_tools)
        names = {t.name for t in filtered}
        assert names == {"Read", "browser_connect"}  # 控制工具始终保留

    def test_returns_all_when_mounted(self):
        tools = [_tool("Read"), _tool("click")]
        loop = _make_loop(tools, mounted=True)
        filtered = loop._apply_browser_tool_filter(loop._all_available_tools)
        assert {t.name for t in filtered} == {"Read", "click"}

    def test_noop_without_chrome_tools(self):
        tools = [_tool("Read"), _tool("Write")]
        loop = _make_loop(tools, mounted=False)
        filtered = loop._apply_browser_tool_filter(loop._all_available_tools)
        assert len(filtered) == 2


class TestMaybeMountBrowserTools:
    def test_mounts_on_url_intent(self):
        tools = [_tool("Read"), _tool("click"), _tool("new_page")]
        loop = _make_loop(tools)
        loop._maybe_mount_browser_tools([
            HumanMessage(content="打开 https://example.com 帮我看看")
        ])
        assert loop._browser_tools_mounted is True
        assert {t.name for t in loop.tools} == {"Read", "click", "new_page"}
        loop.llm.bind_tools.assert_called_once()

    def test_mounts_on_chinese_browser_intent(self):
        tools = [_tool("Read"), _tool("take_screenshot")]
        loop = _make_loop(tools)
        loop._maybe_mount_browser_tools([HumanMessage(content="帮我截取这个网页的内容")])
        assert loop._browser_tools_mounted is True

    def test_no_mount_without_intent(self):
        tools = [_tool("Read"), _tool("click")]
        loop = _make_loop(tools)
        loop._maybe_mount_browser_tools([HumanMessage(content="帮我重构这个函数")])
        assert loop._browser_tools_mounted is False
        assert {t.name for t in loop.tools} == {"Read", "click"}  # 未重绑

    def test_mounts_when_shared_mode_active(self):
        tools = [_tool("Read"), _tool("click")]
        loop = _make_loop(tools)
        mcp = MagicMock()
        mcp._chrome_server_name = "chrome-devtools"
        mcp.get_chrome_mode = MagicMock(return_value="shared")
        loop.mcp_manager = mcp
        loop._maybe_mount_browser_tools([HumanMessage(content="继续")])
        assert loop._browser_tools_mounted is True

    def test_mount_is_one_way(self):
        tools = [_tool("Read"), _tool("click")]
        loop = _make_loop(tools)
        loop._maybe_mount_browser_tools([HumanMessage(content="https://a.com")])
        assert loop._browser_tools_mounted is True
        # 下一轮无意图也不再卸下
        loop._maybe_mount_browser_tools([HumanMessage(content="改个变量名")])
        assert loop._browser_tools_mounted is True
        assert loop.llm.bind_tools.call_count == 1

    def test_noop_when_no_chrome_tools_available(self):
        tools = [_tool("Read")]
        loop = _make_loop(tools)
        loop._maybe_mount_browser_tools([HumanMessage(content="https://a.com")])
        assert loop._browser_tools_mounted is False
        loop.llm.bind_tools.assert_not_called()
