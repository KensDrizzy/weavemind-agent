"""内置浏览器工具 — browser_connect, browser_disconnect, browser_status。

这些工具不是由 MCP Server 提供的，而是 WeaveMind 内置工具，
用于控制 Chrome DevTools MCP Server 的 isolated/shared 模式切换。

参考 PaiCLI 的 ToolRegistry.registerBrowserTools() 设计。
"""

import asyncio
import logging
from typing import Optional, Type

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ── Args Schema ────────────────────────────────────────────────────


class BrowserConnectArgs(BaseModel):
    """browser_connect 参数。"""
    port: Optional[int] = Field(
        None,
        description="旧式端口连接的调试端口号（Chrome 143 及以下），留空则使用 autoConnect（Chrome 144+）",
    )


class BrowserDisconnectArgs(BaseModel):
    """browser_disconnect 参数。"""
    pass


class BrowserStatusArgs(BaseModel):
    """browser_status 参数。"""
    pass


# ── 工具实现 ──────────────────────────────────────────────────────


async def _browser_connect_impl(mcp_manager, port: Optional[int] = None) -> str:
    """连接用户已登录的 Chrome 浏览器。

    流程：
    1. 如果指定了 port，使用 --browserUrl 模式（先检测端口）
    2. 否则使用 --autoConnect 模式（Chrome 144+，依赖 DevToolsActivePort 文件发现）
    3. 重启 MCP Server 并重新注册工具
    """
    if not mcp_manager:
        return "[错误] MCPManager 未初始化"

    if mcp_manager.is_shared_mode():
        return "已在 shared 模式，无需重复连接"

    try:
        if port:
            # 旧式端口连接（需要预检测，因为依赖特定端口）
            from mcp_client.chrome_launcher import ChromeLauncher
            launcher = ChromeLauncher(port=port)
            if not launcher.is_running():
                return _chrome_launch_help(port)
            
            new_args = [
                "-y", "chrome-devtools-mcp@latest",
                "--browserUrl", f"http://localhost:{port}",
            ]
            success = await mcp_manager._restart_chrome_server(new_args, "shared")
        else:
            # autoConnect 模式（Chrome 144+）
            # 先检查 DevToolsActivePort 文件是否存在
            from mcp_client.manager import MCPManager
            port_info = MCPManager._read_devtools_active_port()
            if not port_info:
                return _autoconnect_help("DevToolsActivePort 文件不存在，Chrome 未开启远程调试。")
            
            logger.info("尝试通过 WebSocket 连接 Chrome（端口 %d）...", port_info[0])
            success = await mcp_manager.switch_to_shared()

        if success:
            return "✅ 已切换到 shared 模式，现在使用用户已登录的 Chrome 浏览器。工具列表已更新，请继续操作。"
        else:
            # 切换失败，获取详细错误信息
            error_detail = mcp_manager._last_restart_error if mcp_manager else None
            
            # 尝试用 DevToolsActivePort 文件内容手动连接
            fallback_result = await _try_fallback_browser_url(mcp_manager)
            if fallback_result:
                return fallback_result
            
            # fallback 也失败，返回诊断信息（包含具体错误）
            return _autoconnect_help(error_detail)
    except Exception as e:
        logger.error("browser_connect 执行失败: %s", e)
        return f"[错误] 切换失败: {e}\n\n{_autoconnect_help(str(e))}"


async def _try_fallback_browser_url(mcp_manager) -> Optional[str]:
    """尝试从 DevToolsActivePort 文件读取信息，用 --browserUrl 方式连接。"""
    import os
    from pathlib import Path
    
    # 尝试读取 DevToolsActivePort 文件
    possible_paths = [
        Path.home() / "Library/Application Support/Google/Chrome/DevToolsActivePort",
        Path.home() / ".config/google-chrome/DevToolsActivePort",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data/DevToolsActivePort" if os.environ.get("LOCALAPPDATA") else None,
    ]
    
    for path in possible_paths:
        if path and path.exists():
            try:
                content = path.read_text().strip()
                lines = content.split("\n")
                if len(lines) >= 2:
                    port = lines[0]
                    ws_path = lines[1]
                    browser_url = f"http://localhost:{port}"
                    
                    logger.info("尝试 fallback 连接: %s (WebSocket: %s)", browser_url, ws_path)
                    
                    new_args = [
                        "-y", "chrome-devtools-mcp@latest",
                        "--browserUrl", browser_url,
                    ]
                    success = await mcp_manager._restart_chrome_server(new_args, "shared")
                    
                    if success:
                        return f"✅ 已切换到 shared 模式（通过 DevToolsActivePort: {browser_url}）。工具列表已更新，请继续操作。"
                    else:
                        return None
            except Exception as e:
                logger.warning("读取 DevToolsActivePort 失败: %s", e)
                continue
    
    return None


def _autoconnect_help(error_detail: Optional[str] = None) -> str:
    """生成 autoConnect 模式的帮助信息，包含 DevToolsActivePort 诊断。"""
    # 尝试读取 DevToolsActivePort 文件进行诊断
    diag_info = _diagnose_devtools_active_port()
    
    # 构建错误信息部分
    error_section = ""
    if error_detail:
        error_section = f"""
**详细错误信息：**
```
{error_detail}
```

"""
    
    return f"""❌ 无法通过 autoConnect 连接到 Chrome。
{error_section}
autoConnect 模式通过读取 Chrome 的 DevToolsActivePort 文件获取端口和 WebSocket 路径。

{diag_info}

解决方法：

**步骤 1：确认 Chrome 已开启远程调试**
1. 在 Chrome 地址栏输入：chrome://inspect/#remote-debugging
2. 确认已勾选 ☑️ "Allow remote debugging for this browser instance"
3. 确认显示 "Server running at: 127.0.0.1:9222"

**步骤 2：检查 DevToolsActivePort 文件**
文件通常位于：
- macOS: ~/Library/Application Support/Google/Chrome/DevToolsActivePort
- Linux: ~/.config/google-chrome/DevToolsActivePort
- Windows: %LOCALAPPDATA%\\Google\\Chrome\\User Data\\DevToolsActivePort

**步骤 3：如果已勾选但仍然失败**
尝试重启 Chrome，然后重复步骤 1。

**替代方案：使用显式端口连接**
如果 autoConnect 持续失败，可以绕过文件发现，直接用端口方式：
1. 确认 Chrome 远程调试已开启（chrome://inspect/#remote-debugging 已勾选）
2. 执行：browser_connect --port 9222"""


def _diagnose_devtools_active_port() -> str:
    """诊断 DevToolsActivePort 文件状态。"""
    import os
    from pathlib import Path
    
    # 常见路径
    possible_paths = [
        # macOS
        Path.home() / "Library/Application Support/Google/Chrome/DevToolsActivePort",
        Path.home() / "Library/Application Support/Google/Chrome Canary/DevToolsActivePort",
        # Linux
        Path.home() / ".config/google-chrome/DevToolsActivePort",
        Path.home() / ".config/chromium/DevToolsActivePort",
        # Windows (通过环境变量)
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data/DevToolsActivePort" if os.environ.get("LOCALAPPDATA") else None,
    ]
    
    found_files = []
    for path in possible_paths:
        if path and path.exists():
            try:
                content = path.read_text().strip()
                lines = content.split("\n")
                port = lines[0] if lines else "unknown"
                ws_path = lines[1] if len(lines) > 1 else "unknown"
                found_files.append(f"  ✓ {path}\n    端口: {port}, WebSocket: {ws_path}")
            except Exception as e:
                found_files.append(f"  ✗ {path} (读取失败: {e})")
    
    if found_files:
        return f"""**DevToolsActivePort 文件诊断：**
发现以下文件：
{chr(10).join(found_files)}

如果文件存在但 autoConnect 仍失败，可能是 chrome-devtools-mcp 版本问题或权限问题。"""
    else:
        paths_checked = "\n".join([f"  ✗ {p}" for p in possible_paths if p])
        return f"""**DevToolsActivePort 文件诊断：**
未找到任何 DevToolsActivePort 文件。
已检查路径：
{paths_checked}

这通常意味着 Chrome 未开启远程调试，或使用了非默认的 profile 路径。"""


def _chrome_launch_help(port: int) -> str:
    """生成旧式端口连接的帮助信息。"""
    return f"""❌ 无法连接到 Chrome 调试端口 {port}。

可能原因：
1. Chrome 未运行
2. Chrome 未开启调试端口

解决方法：

先用以下命令启动 Chrome：

macOS:
  open -na "Google Chrome" --args --remote-debugging-port={port}

Linux:
  google-chrome --remote-debugging-port={port}

Windows:
  start chrome.exe --remote-debugging-port={port}

然后重新执行：browser_connect --port {port}"""


async def _browser_disconnect_impl(mcp_manager) -> str:
    """切回 isolated 模式。"""
    if not mcp_manager:
        return "[错误] MCPManager 未初始化"

    if mcp_manager.is_isolated_mode():
        return "已在 isolated 模式，无需切换"

    try:
        success = await mcp_manager.switch_to_isolated()
        if success:
            return "已切换回 isolated 模式，使用独立浏览器（无登录态）。工具列表已更新。"
        else:
            return "[错误] 切换回 isolated 模式失败，请检查 MCP Server 状态"
    except Exception as e:
        logger.error("browser_disconnect 执行失败: %s", e)
        return f"[错误] 切换失败: {e}"


def _browser_status_impl(mcp_manager) -> str:
    """获取浏览器状态。"""
    if not mcp_manager:
        return "[错误] MCPManager 未初始化"
    return mcp_manager.get_browser_status_text()


# ── 工具创建 ──────────────────────────────────────────────────────


def create_browser_connect_tool(mcp_manager):
    """创建 browser_connect 工具实例。"""

    def sync_func(**kwargs) -> str:
        port = kwargs.get("port")
        loop = mcp_manager._mcp_loop
        if loop and loop.is_running():
            # 在持久 MCP 事件循环上执行异步操作
            future = asyncio.run_coroutine_threadsafe(
                _browser_connect_impl(mcp_manager, port), loop
            )
            try:
                return future.result(timeout=90)
            except Exception as e:
                logger.error("browser_connect 执行失败: %s", e)
                return f"[错误] {e}"
        else:
            # 无持久循环时回退到 asyncio.run（仅用于测试等场景）
            try:
                return asyncio.run(_browser_connect_impl(mcp_manager, port))
            except Exception as e:
                logger.error("browser_connect 执行失败: %s", e)
                return f"[错误] {e}"

    async def async_func(**kwargs) -> str:
        port = kwargs.get("port")
        return await _browser_connect_impl(mcp_manager, port)

    return StructuredTool.from_function(
        func=sync_func,
        coroutine=async_func,
        name="browser_connect",
        description=(
            "连接已允许远程调试的本机 Chrome 浏览器，复用其登录态。\n"
            "使用时机：browser 工具返回登录页/401/403/需要登录时。\n"
            "前置条件：**必须先关闭当前页面**（close_page），再调用此工具。\n"
            "执行流程：\n"
            "1. close_page 关闭 isolated 模式的旧页面\n"
            "2. 调用 browser_connect 检测并切换模式\n"
            "3. 如果返回成功，用 new_page/navigate_page 重新打开 URL\n"
            "4. 如果返回失败（Chrome 未开启远程调试），停止并告知用户如何开启\n"
            "注意：Chrome 144+ 请先在 chrome://inspect/#remote-debugging 勾选 Allow remote debugging"
        ),
        args_schema=BrowserConnectArgs,
    )


def create_browser_disconnect_tool(mcp_manager):
    """创建 browser_disconnect 工具实例。"""

    def sync_func(**kwargs) -> str:
        loop = mcp_manager._mcp_loop
        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                _browser_disconnect_impl(mcp_manager), loop
            )
            try:
                return future.result(timeout=30)
            except Exception as e:
                logger.error("browser_disconnect 执行失败: %s", e)
                return f"[错误] {e}"
        else:
            try:
                return asyncio.run(_browser_disconnect_impl(mcp_manager))
            except Exception as e:
                logger.error("browser_disconnect 执行失败: %s", e)
                return f"[错误] {e}"

    async def async_func(**kwargs) -> str:
        return await _browser_disconnect_impl(mcp_manager)

    return StructuredTool.from_function(
        func=sync_func,
        coroutine=async_func,
        name="browser_disconnect",
        description=(
            "切回 isolated 浏览器模式（独立浏览器，无登录态）。"
            "完成登录态页面访问后调用此工具。"
        ),
        args_schema=BrowserDisconnectArgs,
    )


def create_browser_status_tool(mcp_manager):
    """创建 browser_status 工具实例。"""

    def sync_func(**kwargs) -> str:
        return _browser_status_impl(mcp_manager)

    async def async_func(**kwargs) -> str:
        return _browser_status_impl(mcp_manager)

    return StructuredTool.from_function(
        func=sync_func,
        coroutine=async_func,
        name="browser_status",
        description="查看当前浏览器 MCP 模式、连接状态、Chrome 调试端口探活信息。",
        args_schema=BrowserStatusArgs,
    )


def create_all_browser_tools(mcp_manager):
    """创建所有内置浏览器工具。"""
    return [
        create_browser_connect_tool(mcp_manager),
        create_browser_disconnect_tool(mcp_manager),
        create_browser_status_tool(mcp_manager),
    ]