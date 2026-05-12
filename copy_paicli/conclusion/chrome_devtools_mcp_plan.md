# Chrome DevTools MCP 专项实现方案

> 本文档补充说明如何在 WeaveMindAgent 中接入 Chrome DevTools MCP Server，实现浏览器自动化控制能力。
> 作为 `mcp_implementation_plan.md` 的补充文档。

---

## 一、Chrome DevTools MCP 简介

### 1.1 什么是 Chrome DevTools MCP

**Chrome DevTools MCP** 是一个基于 **Chrome DevTools Protocol (CDP)** 的 MCP Server，它允许 LLM 通过标准化的 MCP 接口控制 Chrome/Chromium 浏览器。

**GitHub**: https://github.com/modelcontextprotocol/servers/tree/main/src/chrome-devtools

**核心能力：**
```
┌─────────────────────────────────────────────────────────────┐
│                 Chrome DevTools MCP Server                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │   Console    │  │   Network    │  │     DOM      │      │
│  │   Access     │  │   Monitor    │  │   Manipulate │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │  Page Nav    │  │  Screenshot  │  │  Execute JS  │      │
│  │  (goto/url)  │  │  (capture)   │  │  (evaluate)  │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
└─────────────────────────────────────────────────────────────┘
                              │
                              │ CDP Protocol
                              ▼
                    ┌─────────────────┐
                    │  Chrome Browser │
                    │  (port 9222)    │
                    └─────────────────┘
```

### 1.2 提供的核心工具

| 工具名 | 功能 | 典型使用场景 |
|--------|------|-------------|
| `chrome_navigate` | 导航到指定 URL | 打开网页、页面跳转 |
| `chrome_evaluate` | 执行 JavaScript | 数据提取、页面操作 |
| `chrome_click` | 点击页面元素 | 表单提交、按钮点击 |
| `chrome_type` | 输入文本 | 表单填写、搜索框输入 |
| `chrome_screenshot` | 页面截图 | 证据留存、可视化验证 |
| `chrome_get_dom` | 获取 DOM 结构 | 页面分析、元素定位 |
| `chrome_wait` | 等待条件 | 异步加载完成检测 |
| `chrome_console_logs` | 获取控制台日志 | 调试信息收集 |
| `chrome_network_logs` | 获取网络请求日志 | API 监控、性能分析 |

---

## 二、与传统 WebFetch/WebSearch 的对比

### 2.1 能力对比

| 能力 | WebFetch/WebSearch | Chrome DevTools MCP |
|------|-------------------|---------------------|
| 静态页面抓取 | ✅ 优秀 | ✅ 支持 |
| 动态内容渲染 | ❌ 不支持 | ✅ 完整支持 |
| JavaScript 执行 | ❌ 不支持 | ✅ 支持 |
| 表单交互 | ❌ 不支持 | ✅ 支持 |
| 截图留证 | ❌ 不支持 | ✅ 支持 |
| 用户行为模拟 | ❌ 不支持 | ✅ 支持 |
| 实现复杂度 | 低 | 中等（需启动 Chrome） |
| 资源开销 | 低 | 中等（Chrome 进程） |

### 2.2 何时使用 Chrome DevTools MCP

**适用场景：**
1. **SPA 单页应用抓取** - React/Vue 渲染的内容
2. **需要登录/交互** - 需要点击、填写表单后才能获取数据
3. **懒加载内容** - 需要滚动触发加载的页面
4. **验证和审计** - 需要截图证明操作结果
5. **复杂数据提取** - 需要通过 JS 执行才能获取的数据

**示例场景：**
```
用户: "帮我从淘宝搜索'iPhone 16'，按销量排序，提取前10个商品的价格和名称"

传统 WebFetch: ❌ 淘宝有反爬、JS 渲染、需要交互
Chrome DevTools MCP: ✅ 可以模拟真实用户操作
```

---

## 三、配置和安装

### 3.1 前置条件

**1. 安装 Chrome/Chromium**
```bash
# macOS
brew install --cask google-chrome

# 或者安装 Chromium
brew install chromium

# 验证安装
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --version
```

**2. 启用 Chrome Remote Debugging**

在 WeaveMindAgent 启动前，需要启动带有远程调试端口的 Chrome：

```bash
# 启动 Chrome with remote debugging
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --no-first-run \
  --no-default-browser-check \
  --headless=new  # 可选：无头模式
```

或者使用 Chrome DevTools MCP 的启动命令：
```bash
npx @modelcontextprotocol/server-chrome --port 9222
```

### 3.2 config.yaml 配置

```yaml
# config.yaml - Chrome DevTools MCP 配置

mcp:
  enabled: true
  
  servers:
    # Chrome DevTools MCP Server
    chrome:
      enabled: true
      transport: stdio
      command: npx
      args:
        - "-y"
        - "@modelcontextprotocol/server-chrome"
        - "--port"
        - "9222"
      env:
        # Chrome 可执行文件路径（可选，默认自动检测）
        CHROME_EXECUTABLE: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        # 调试端口
        CHROME_DEBUG_PORT: "9222"
        # 是否以无头模式启动
        CHROME_HEADLESS: "false"
        # 启动参数
        CHROME_ARGS: "--no-first-run --no-default-browser-check"
```

### 3.3 自动启动 Chrome 的方案

为了方便使用，可以在 WeaveMindAgent 启动时自动启动 Chrome：

```python
# mcp/chrome_launcher.py
import subprocess
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class ChromeLauncher:
    """自动管理 Chrome 进程生命周期。"""
    
    def __init__(
        self,
        port: int = 9222,
        headless: bool = False,
        executable: str = None
    ):
        self.port = port
        self.headless = headless
        self.executable = executable or self._find_chrome()
        self._process = None
    
    def _find_chrome(self) -> str:
        """自动查找 Chrome 路径。"""
        import platform
        
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
            ]
        elif system == "Windows":
            paths = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ]
        
        for path in paths:
            if Path(path).exists():
                return path
        
        raise RuntimeError("无法找到 Chrome/Chromium，请手动指定路径")
    
    def start(self) -> bool:
        """启动 Chrome with remote debugging。"""
        
        # 检查端口是否已被占用（Chrome 已运行）
        if self._check_port():
            logger.info(f"Chrome 已运行在端口 {self.port}")
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
        
        try:
            self._process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            
            # 等待 Chrome 启动
            for _ in range(10):
                time.sleep(0.5)
                if self._check_port():
                    logger.info(f"Chrome 已启动，调试端口: {self.port}")
                    return True
            
            logger.error("Chrome 启动超时")
            return False
            
        except Exception as e:
            logger.error(f"启动 Chrome 失败: {e}")
            return False
    
    def stop(self):
        """停止 Chrome（如果是本启动器启动的）。"""
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
            logger.info("Chrome 已停止")
    
    def _check_port(self) -> bool:
        """检查调试端口是否可用。"""
        import socket
        try:
            sock = socket.create_connection(("localhost", self.port), timeout=1)
            sock.close()
            return True
        except:
            return False
```

---

## 四、工具封装增强

### 4.1 Chrome DevTools 工具的特殊处理

Chrome DevTools MCP 返回的结果包含丰富的结构化数据，需要专门的格式化：

```python
# mcp/chrome_tools.py
"""Chrome DevTools MCP 工具的专用包装器和结果格式化。"""

import json
import base64
from typing import Any
from mcp.tools import create_mcp_tool_class


class ChromeToolResultFormatter:
    """Chrome DevTools 工具结果格式化器。"""
    
    @staticmethod
    def format_navigate(result) -> str:
        """格式化导航结果。"""
        # result 包含 frameId, loaderId 等信息
        return f"✅ 页面导航成功"
    
    @staticmethod
    def format_evaluate(result) -> str:
        """格式化 JS 执行结果。"""
        # CDP Runtime.evaluate 返回 {result: {type, value, description}}
        if hasattr(result, 'result'):
            eval_result = result.result
            if eval_result.get('type') == 'string':
                return f"📤 返回: {eval_result.get('value', '')}"
            elif eval_result.get('type') == 'object':
                value = eval_result.get('value', {})
                return f"📤 返回对象:\n```json\n{json.dumps(value, indent=2, ensure_ascii=False)}\n```"
            else:
                return f"📤 返回值: {eval_result.get('value', eval_result.get('description', 'N/A'))}"
        return str(result)
    
    @staticmethod
    def format_screenshot(result) -> str:
        """格式化截图结果。"""
        # CDP 返回 base64 编码的图片
        if hasattr(result, 'data'):
            # 保存截图到文件
            image_data = base64.b64decode(result.data)
            filename = f".weavemind/screenshots/{int(time.time())}.png"
            Path(filename).parent.mkdir(parents=True, exist_ok=True)
            with open(filename, "wb") as f:
                f.write(image_data)
            return f"📸 截图已保存: {filename}"
        return "截图成功"
    
    @staticmethod
    def format_dom(result) -> str:
        """格式化 DOM 结果。"""
        if hasattr(result, 'outerHTML'):
            html = result.outerHTML[:500]  # 截断避免过长
            return f"📄 DOM 结构 (前500字符):\n```html\n{html}...\n```"
        return str(result)
    
    @staticmethod
    def format_console_logs(result) -> str:
        """格式化控制台日志。"""
        if isinstance(result, list):
            lines = []
            for log in result[-20:]:  # 只显示最近20条
                level = log.get('level', 'info')
                text = log.get('text', '')
                lines.append(f"[{level.upper()}] {text}")
            return "🖥️ 控制台日志:\n" + "\n".join(lines)
        return str(result)


def create_chrome_tool_class(tool_info, connection):
    """创建 Chrome DevTools 专用工具类。"""
    
    base_class = create_mcp_tool_class(tool_info, connection)
    tool_name = tool_info.name
    
    class ChromeToolWrapper(base_class):
        """Chrome DevTools 工具包装器（增强版）。"""
        
        async def _arun(self, **kwargs) -> str:
            """执行并格式化结果。"""
            result = await self._connection.call_tool(self.name, kwargs)
            
            # 根据工具类型选择格式化器
            formatter = ChromeToolResultFormatter()
            
            if tool_name == "chrome_navigate":
                return formatter.format_navigate(result)
            elif tool_name == "chrome_evaluate":
                return formatter.format_evaluate(result)
            elif tool_name == "chrome_screenshot":
                return formatter.format_screenshot(result)
            elif tool_name == "chrome_get_dom":
                return formatter.format_dom(result)
            elif tool_name == "chrome_console_logs":
                return formatter.format_console_logs(result)
            else:
                # 使用默认格式化
                return base_class._format_result(result)
    
    ChromeToolWrapper.__name__ = f"Chrome_{tool_name}"
    return ChromeToolWrapper
```

### 4.2 MCPManager 的 Chrome 特殊处理

```python
# 在 MCPManager.initialize() 中添加 Chrome 自动启动逻辑

async def initialize(self) -> bool:
    # ... 原有代码
    
    # 检查是否有 Chrome DevTools MCP
    chrome_config = self._servers_config.get("chrome")
    if chrome_config and chrome_config.get("enabled", True):
        # 尝试自动启动 Chrome
        from mcp.chrome_launcher import ChromeLauncher
        
        port = chrome_config.get("env", {}).get(
            "CHROME_DEBUG_PORT", "9222"
        )
        headless = chrome_config.get("env", {}).get(
            "CHROME_HEADLESS", "false"
        ).lower() == "true"
        
        launcher = ChromeLauncher(
            port=int(port),
            headless=headless
        )
        
        if launcher.start():
            self._chrome_launcher = launcher
            # 等待 Chrome 完全启动
            await asyncio.sleep(2)
        else:
            logger.warning("Chrome 自动启动失败，将尝试连接已有实例")
    
    # ... 继续原有初始化逻辑
```

---

## 五、使用示例

### 5.1 网页抓取（动态内容）

```
User: "帮我抓取美团外卖上杭州西湖区热门商家的名称和评分"

Agent 使用 Chrome DevTools:
1. chrome_navigate -> "https://waimaie.meituan.com"
2. chrome_type -> 搜索框输入 "西湖区"
3. chrome_click -> 点击搜索按钮
4. chrome_wait -> 等待结果加载
5. chrome_evaluate -> 执行 JS 提取商家数据
6. chrome_screenshot -> 截图验证
```

### 5.2 表单自动化

```
User: "帮我在 GitHub 上创建一个新的 Issue，标题是 'Bug Report'"

Agent 使用 Chrome DevTools:
1. chrome_navigate -> GitHub issues 页面
2. chrome_click -> "New issue" 按钮
3. chrome_type -> 输入标题和描述
4. chrome_click -> "Submit" 按钮
5. chrome_screenshot -> 截图确认创建成功
```

### 5.3 数据提取与验证

```
User: "查看 https://example.com 的页面控制台日志，检查是否有错误"

Agent 使用 Chrome DevTools:
1. chrome_navigate -> 目标页面
2. chrome_console_logs -> 获取所有日志
3. chrome_evaluate -> 执行 window.errors 检查
4. 分析并报告错误情况
```

---

## 六、安全和限制

### 6.1 安全考虑

| 风险 | 缓解措施 |
|------|----------|
| 浏览器自动化被恶意利用 | 执行前向用户确认<br>限制可访问的域名白名单 |
| Cookie/Session 泄露 | Chrome 使用独立 Profile<br>敏感操作需二次认证 |
| 截图包含敏感信息 | 截图自动保存到本地<br>不自动上传 |
| 资源消耗 | 限制并发页面数<br>超时自动清理 |

### 6.2 建议的权限策略

```python
# permissions/chrome_policy.py

class ChromeDevToolsPolicy:
    """Chrome DevTools 工具权限策略。"""
    
    DISALLOWED_URLS = [
        "*bank*",
        "*payment*",
        "*login*",  # 或需要额外确认
    ]
    
    REQUIRE_CONFIRMATION = [
        "chrome_click",  # 点击可能触发提交
        "chrome_type",   # 输入敏感信息
        "chrome_navigate",  # 跳转到外部链接
    ]
    
    @classmethod
    def check_url(cls, url: str) -> bool:
        """检查 URL 是否允许访问。"""
        import fnmatch
        for pattern in cls.DISALLOWED_URLS:
            if fnmatch.fnmatch(url.lower(), pattern):
                return False
        return True
    
    @classmethod
    def require_confirmation(cls, tool_name: str) -> bool:
        """检查工具是否需要用户确认。"""
        return tool_name in cls.REQUIRE_CONFIRMATION
```

---

## 七、依赖和安装命令总结

```bash
# 1. 安装 Chrome DevTools MCP Server
npm install -g @modelcontextprotocol/server-chrome

# 或直接通过 npx 使用（推荐）
npx @modelcontextprotocol/server-chrome --port 9222

# 2. 确保 Chrome/Chromium 已安装
# macOS
brew install --cask google-chrome

# 3. 可选：Python 依赖
pip install websocket-client  # 如果使用原生 CDP 客户端

# 4. 验证 MCP Server 安装
npx @modelcontextprotocol/server-chrome --help
```

---

## 八、总结

Chrome DevTools MCP 为 WeaveMindAgent 带来了**真正的浏览器自动化能力**，可以处理传统 WebFetch 无法应对的动态页面和交互场景。

### 与通用 MCP 方案的差异：

| 方面 | 通用 MCP | Chrome DevTools MCP |
|------|----------|---------------------|
| 前置条件 | 无 | 需要 Chrome/Chromium |
| 资源管理 | 简单 | 需要进程管理（启动/停止） |
| 结果处理 | 简单格式化 | 复杂的结构化数据格式化 |
| 安全策略 | 标准 | 需要额外的 URL/操作白名单 |
| 错误恢复 | 简单重试 | 可能需要页面刷新重试 |

### 建议实施优先级：

1. **P0** - 基础 MCP 框架（已实现计划中）
2. **P1** - Chrome DevTools MCP 配置支持
3. **P2** - Chrome 自动启动和管理
4. **P3** - 专用结果格式化和权限策略

---

*本文档作为 `mcp_implementation_plan.md` 的补充，专注于 Chrome DevTools MCP 的专项实现。*
