# WeaveMindAgent CDP 登录态保持功能实现计划

> 本文档分析 PaiCLI 的 CDP 登录态保持机制，并制定在 WeaveMindAgent 中的实现方案。
> 基于: `/Users/lqf/projects/agentcode/WeaveMindAgent/copy_paicli/cdp_keep_login.pdf`

---

## 一、PaiCLI CDP 登录态保持机制总结

### 1.1 核心问题

**chrome-devtools-mcp 默认使用 isolated 模式：**
- 每次创建临时 user-data-dir
- 浏览器关闭后临时目录被清理
- Agent 的浏览器与用户日常 Chrome 完全隔离
- 无法访问 GitHub、飞书、公司内网等已登录状态

### 1.2 解决方案：isolated/shared 双模式

| 模式 | 说明 | 使用场景 |
|------|------|----------|
| **isolated** | 临时 user-data-dir，无登录态 | 公开页面、安全隔离 |
| **shared** | 连接用户已有 Chrome，继承登录态 | 需要认证的页面 |

### 1.3 自动切换机制

```
用户请求访问页面
       │
       ▼
Agent 先用 isolated 模式尝试
       │
       ├── 页面正常返回 → 使用 isolated 结果
       │
       └── 检测到登录页/无权限 ──► 自动切换到 shared 模式
                                         │
                                         ▼
                              检测本机是否有开启远程调试的 Chrome
                                         │
                                         ├── 发现 → 用 --autoConnect 连接
                                         │
                                         └── 未发现 → 提示用户开启远程调试
```

### 1.4 关键实现点

**（1）Chrome 远程调试开启**
- 用户在 Chrome 地址栏输入 `chrome://inspect/#remote-debugging`
- 打开 "Allow remote debugging for this browser instance" 开关
- Chrome 144+ 版本支持（必须）

**（2）--autoConnect 发现机制**
- 不扫描固定端口 9222（避免冲突）
- 读取 `DevToolsActivePort` 文件获取随机端口和 WebSocket 路径
- macOS 路径: `~/Library/Application Support/Google/Chrome/DevToolsActivePort`

**（3）敏感页面保护机制**
- 内置 14 条默认规则覆盖银行、支付、云控制台
- 读型工具（截图、快照）不受影响
- 修改型工具（点击、填写、执行脚本）强制单步 HITL 审批

**（4）标签页防误关**
- 记录 Agent 自己创建的标签页 ID (`agentOpenedTabs`)
- shared 模式下禁止关闭非自创建的标签页

---

## 二、WeaveMindAgent 现有基础分析

### 2.1 已实现的相关功能

| 模块 | 功能 | 状态 |
|------|------|------|
| `mcp_client/chrome_launcher.py` | Chrome 自动启动、端口检测 | ✅ 已有 |
| `mcp_client/client.py` | MCP 长连接管理 | ✅ 已有 |
| `mcp_client/manager.py` | 多 Server 管理、工具聚合 | ✅ 已有 |
| `mcp_client/chrome_formatter.py` | Chrome 工具结果格式化 | ✅ 已有 |
| `mcp_client/tools.py` | 动态工具封装 | ✅ 已有 |
| `permissions/policy.py` | 基础权限策略 | ✅ 已有 |

### 2.2 现有 Chrome Launcher 能力

```python
# chrome_launcher.py 已有功能
- _find_chrome()           # 自动检测 Chrome 路径
- _check_port()            # 检测调试端口（支持 IPv4/IPv6）
- start()                  # 启动 Chrome with --remote-debugging-port
- stop()                   # 停止由启动器启动的 Chrome
- is_running()             # 检查 Chrome 状态
- launched_by_us           # 是否由本启动器启动
```

### 2.3 与目标差距

| PaiCLI 功能 | WeaveMindAgent 现状 | 差距 |
|-------------|---------------------|------|
| isolated/shared 双模式 | 仅支持 isolated（临时 profile） | 需新增模式切换 |
| --autoConnect 自动发现 | 使用固定端口 9222 | 需支持 DevToolsActivePort 文件读取 |
| 自动模式切换 | 无 | 需新增失败检测+自动重连机制 |
| 敏感页面保护 | 仅有基础权限策略 | 需新增 URL 规则匹配+HITL 升级 |
| 标签页所有权追踪 | 无 | 需新增 tab 记录+关闭保护 |

---

## 三、实现方案

### 3.1 架构设计

```
┌─────────────────────────────────────────────────────────────────┐
│                     WeaveMindCLI                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              ChromeSessionManager (新增)                 │   │
│  │  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐  │   │
│  │  │  ISOLATED   │◄──►│   SHARED    │◄──►│   AUTO      │  │   │
│  │  │   模式      │    │   模式      │    │  切换逻辑   │  │   │
│  │  └─────────────┘    └─────────────┘    └─────────────┘  │   │
│  └─────────────────────────────────────────────────────────┘   │
│                           │                                     │
│       ┌───────────────────┼───────────────────┐                 │
│       ▼                   ▼                   ▼                 │
│  ┌─────────┐        ┌─────────┐        ┌─────────────┐         │
│  │ ChromeLauncher    │ AutoConnect     │ BrowserGuard │         │
│  │ (已有)   │        │  (新增)  │        │   (新增)    │         │
│  │         │        │         │        │             │         │
│  │ 启动临时 │        │ 读取 DevTools│      │ 敏感页面检测 │         │
│  │ Chrome   │        │ ActivePort │      │ 标签页保护  │         │
│  └────┬────┘        └────┬────┘        └──────┬──────┘         │
│       │                  │                      │               │
│       └──────────────────┴──────────────────────┘               │
│                          │                                      │
│                          ▼                                      │
│              ┌─────────────────────┐                           │
│              │  MCPManager (已有)  │                           │
│              │  - MCPConnection    │                           │
│              │  - 工具动态封装      │                           │
│              └─────────────────────┘                           │
└─────────────────────────────────────────────────────────────────┘
```

---

### 3.2 核心模块设计

#### 3.2.1 ChromeSessionManager - 会话模式管理器

**文件**: `mcp_client/session_manager.py`

```python
from enum import Enum, auto
from typing import Optional, Set
from dataclasses import dataclass, field
import logging

logger = logging.getLogger(__name__)


class ChromeMode(Enum):
    """Chrome 运行模式"""
    ISOLATED = "isolated"   # 临时 user-data-dir，无登录态
    SHARED = "shared"       # 连接用户 Chrome，有登录态


@dataclass
class ChromeSession:
    """Chrome 会话状态"""
    mode: ChromeMode
    user_data_dir: Optional[str] = None   # isolated 模式使用
    browser_url: Optional[str] = None     # shared 模式使用
    page_ids: Set[str] = field(default_factory=set)  # Agent 创建的标签页


class ChromeSessionManager:
    """
    Chrome 会话管理器。
    
    职责：
    1. 管理 isolated/shared 双模式切换
    2. 维护当前会话状态
    3. 协调 ChromeLauncher 和 MCPManager
    4. 提供模式切换的原子操作
    
    使用示例：
        manager = ChromeSessionManager(mcp_manager)
        await manager.start_isolated()  # 默认启动 isolated
        
        # 当检测到需要登录时
        if manager.detect_need_login(result):
            await manager.switch_to_shared()  # 自动切换到 shared
    """
    
    def __init__(self, mcp_manager, chrome_launcher=None):
        self._mcp_manager = mcp_manager
        self._chrome_launcher = chrome_launcher
        self._session: Optional[ChromeSession] = None
        self._server_name: str = "chrome"
        
    @property
    def current_mode(self) -> Optional[ChromeMode]:
        """获取当前会话模式"""
        return self._session.mode if self._session else None
        
    @property
    def is_shared(self) -> bool:
        """是否处于 shared 模式"""
        return self._session and self._session.mode == ChromeMode.SHARED
    
    async def start_isolated(self) -> bool:
        """启动 isolated 模式会话（默认）"""
        pass
    
    async def switch_to_shared(self) -> bool:
        """切换到 shared 模式（自动发现用户 Chrome）"""
        pass
    
    def detect_need_login(self, page_content: str, url: str = "") -> bool:
        """根据页面内容判断是否需要登录"""
        pass
    
    def record_agent_page(self, page_id: str):
        """记录 Agent 创建的标签页"""
        if self._session:
            self._session.page_ids.add(page_id)
    
    def is_agent_page(self, page_id: str) -> bool:
        """检查标签页是否由 Agent 创建"""
        return self._session and page_id in self._session.page_ids

```

#### 3.2.2 AutoConnectDiscovery - 自动发现机制

**文件**: `mcp_client/auto_connect.py`

```python
"""
Chrome DevTools autoConnect 机制实现。

原理：
1. 用户开启 Chrome 远程调试后，Chrome 将端口号和 WebSocket 路径写入 DevToolsActivePort 文件
2. autoConnect 读取该文件获取连接信息
3. 不扫描固定端口，避免冲突和误连

文件位置：
- macOS: ~/Library/Application Support/Google/Chrome/DevToolsActivePort
- Linux: ~/.config/google-chrome/DevToolsActivePort
- Windows: %LOCALAPPDATA%/Google/Chrome/User Data/DevToolsActivePort

文件格式（两行）：
  \d+                    # 端口号（随机分配）
  /devtools/browser/...  # WebSocket 路径
"""

import platform
import re
from pathlib import Path
from typing import Optional, Tuple


class AutoConnectDiscovery:
    """Chrome DevTools 自动发现器"""
    
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
        """获取默认 Chrome 用户数据目录"""
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
            
        except (ValueError, IOError, OSError):
            return None
    
    def get_browser_url(self) -> Optional[str]:
        """
        获取用于 --browser-url 参数的完整 WebSocket URL。
        
        Returns:
            ws://127.0.0.1:{port}{ws_path} 或 None
        """
        result = self.discover()
        if not result:
            return None
        
        port, ws_path = result
        return f"ws://127.0.0.1:{port}{ws_path}"
    
    def is_remote_debugging_enabled(self) -> bool:
        """检查 Chrome 是否开启了远程调试"""
        return self.discover() is not None
```

#### 3.2.3 BrowserGuard - 浏览器行为保护器

**文件**: `mcp_client/browser_guard.py`

```python
"""
浏览器行为保护机制。

职责：
1. 敏感页面检测与保护
2. 标签页关闭权限控制
3. 工具调用前的安全检查

策略设计原则：
- 读型操作（快照、截图）风险低，允许
- 写型操作（点击、填写、执行脚本）在敏感页面需强制确认
- 不关闭非 Agent 创建的标签页
"""

import re
import fnmatch
from dataclasses import dataclass
from typing import List, Set, Optional
from enum import Enum


class PageRiskLevel(Enum):
    """页面风险等级"""
    SAFE = "safe"
    SENSITIVE = "sensitive"  # 敏感页面，写操作需确认
    CRITICAL = "critical"    # 关键页面（支付等），应禁止访问


@dataclass
class PageCheckResult:
    """页面检查结果"""
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
    WRITE_TOOLS = {
        "click", "drag", "fill", "fill_form",
        "handle_dialog", "hover", "press_key",
        "resize_page", "upload_file", "evaluate_script",
        "type_text",  # 旧版工具名
    }
    
    # 读型工具（不受敏感规则限制）
    READ_TOOLS = {
        "take_screenshot", "take_snapshot",
        "list_pages", "list_console_messages",
        "list_network_requests", "get_console_message",
        "get_network_request",
    }
    
    def __init__(self, custom_patterns: Optional[List[str]] = None):
        """
        Args:
            custom_patterns: 用户自定义敏感页面规则文件路径
        """
        self._patterns = self.DEFAULT_SENSITIVE_PATTERNS.copy()
        self._compiled = [self._glob_to_regex(p) for p in self._patterns]
        
        # 加载自定义规则
        if custom_patterns:
            self._load_custom_patterns(custom_patterns)
    
    def _glob_to_regex(self, pattern: str) -> re.Pattern:
        """将 glob 通配符转换为正则表达式"""
        import fnmatch
        regex = fnmatch.translate(pattern)
        return re.compile(regex, re.IGNORECASE)
    
    def _load_custom_patterns(self, filepath: str):
        """从文件加载自定义规则"""
        from pathlib import Path
        
        path = Path(filepath)
        if not path.exists():
            return
        
        try:
            with open(path, 'r') as f:
                for line in f:
                    line = line.strip()
                    # 跳过注释和空行
                    if not line or line.startswith('#'):
                        continue
                    self._patterns.append(line)
                    self._compiled.append(self._glob_to_regex(line))
        except IOError:
            pass
    
    def check_page(self, url: str) -> PageCheckResult:
        """
        检查 URL 的风险等级。
        
        Args:
            url: 页面 URL
            
        Returns:
            页面检查结果
        """
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
        url: str,
        is_agent_page: bool = False
    ) -> tuple[bool, Optional[str]]:
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
                return True, result.message  # 允许但需标记（由 HITL 处理）
        
        return True, None
    
    def needs_confirmation(self, tool_name: str, url: str) -> tuple[bool, Optional[str]]:
        """
        判断工具调用是否需要用户确认。
        
        Returns:
            (是否需要确认, 确认提示信息)
        """
        result = self.check_page(url)
        
        if result.risk_level == PageRiskLevel.SENSITIVE and tool_name in self.WRITE_TOOLS:
            return True, f"⚠️ 敏感页面检测到，{tool_name} 操作需要确认\n{result.message}"
        
        return False, None
```

### 3.3 配置方式

**config.yaml 扩展：**

```yaml
# 在原有 MCP 配置基础上扩展
mcp:
  enabled: true
  
  servers:
    chrome:
      enabled: true
      transport: stdio
      command: npx
      args:
        - "-y"
        - "@modelcontextprotocol/server-chrome"
      
      # 新增：Chrome 模式配置
      chrome:
        # isolated 模式配置（默认）
        isolated:
          enabled: true
          # 是否自动启动 Chrome
          auto_start: true
          # 调试端口（isolated 模式固定）
          port: 9222
          # 无头模式
          headless: false
          # Chrome 可执行文件路径（可选，自动检测）
          executable: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        
        # shared 模式配置（连接用户 Chrome）
        shared:
          enabled: true
          # 发现机制：auto（自动发现）/manual（手动指定）
          discovery: "auto"
          # 手动指定时的 browser-url（discovery=manual 时使用）
          # browser_url: "ws://127.0.0.1:9222/devtools/browser/xxx"
          # Chrome 通道（影响 auto 发现时的用户数据目录路径）
          channel: "stable"  # stable/beta/dev/canary
      
      # 环境变量
      env:
        PATH: "/opt/homebrew/bin:${PATH}"

  # 新增：浏览器行为保护配置
  browser_guard:
    enabled: true
    # 敏感页面规则文件路径（可选）
    custom_patterns_file: ".weavemind/sensitive_patterns.txt"
    # 是否在 shared 模式下启用标签页保护
    protect_user_tabs: true

# permissions 中新增 Chrome DevTools 工具权限
permissions:
  allowed_tools: []
  disallowed_tools: []
  
  # 新增：按工具名+URL 的细粒度权限
  chrome:
    # 默认模式：isolated / shared / auto
    default_mode: "auto"
    # 自动切换时的策略
    auto_switch:
      # 检测到登录页时是否自动尝试切换
      on_login_detected: true
      # 切换失败回退到 isolated
      fallback_on_failure: true
```

**敏感页面规则文件示例** (`.weavemind/sensitive_patterns.txt`):

```
# 用户自定义敏感页面规则
# 每行一个 glob 模式，# 开头是注释

# 公司内部系统
*://admin.mycompany.com/*
*://erp.mycompany.com/*
*://internal-tools.company.com/*

# 个人账户设置
*://account.*.com/settings/*
*://profile.*.com/*
```

---

### 3.4 与现有 Hooks 系统集成

利用现有的 `HookManager` (已废弃，改用 HITL) 在关键节点插入检查：

```python
# 在原有 PermissionPolicy 基础上扩展 ChromeGuardPolicy

from permissions.policy import PermissionPolicy
from permissions.modes import PermissionMode
from mcp_client.browser_guard import BrowserGuard


class ChromePermissionPolicy(PermissionPolicy):
    """
    扩展的权限策略，支持 Chrome DevTools 工具的细粒度控制。
    """
    
    def __init__(self, *args, browser_guard: BrowserGuard = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._browser_guard = browser_guard or BrowserGuard()
    
    def check_chrome_tool(
        self,
        tool_name: str,
        url: str,
        is_agent_page: bool,
        mode: str = PermissionMode.DEFAULT
    ) -> tuple[bool, Optional[str]]:
        """
        检查 Chrome 工具调用权限。
        
        Returns:
            (是否允许, 拒绝原因)
        """
        # 1. 基础权限检查
        if not self.is_allowed(tool_name, mode):
            return False, f"工具 {tool_name} 不在允许列表中"
        
        # 2. 浏览器专用检查
        allowed, reason = self._browser_guard.check_tool_use(
            tool_name, url, is_agent_page
        )
        if not allowed:
            return False, reason
        
        return True, None
    
    def needs_confirmation(
        self,
        tool_name: str,
        url: str = "",
        mode: str = PermissionMode.DEFAULT
    ) -> tuple[bool, Optional[str]]:
        """判断是否需要确认"""
        # 基础检查
        base_confirm = super().needs_confirmation(tool_name, mode)
        
        # Chrome 工具额外检查
        if tool_name.startswith("chrome_") or tool_name in BrowserGuard.WRITE_TOOLS:
            guard_confirm, message = self._browser_guard.needs_confirmation(
                tool_name, url
            )
            if guard_confirm:
                return True, message
        
        return base_confirm, None
```

---

### 3.5 提示词更新

在 `core/memory.py` 注入的 System Prompt 中增加浏览器模式说明：

```markdown
## 浏览器登录态 (Chrome DevTools MCP)

你拥有控制 Chrome 浏览器的能力。浏览器有两种运行模式：

**isolated 模式（默认）**：
- 使用独立的临时浏览器实例
- 无 Cookie、无登录态
- 适合访问公开页面

**shared 模式**：
- 连接用户已有的 Chrome 浏览器
- 继承用户的登录态（GitHub、飞书、公司内网等）
- 适合访问需要认证的页面
- 注意：你看到的页面是用户的真实账户视图

### 自动切换机制
当你尝试访问一个需要登录的页面时，系统会自动从 isolated 切换到 shared 模式。
你不需要手动干预，系统会处理这个切换。

### 安全边界 - shared 模式下
1. **不要主动点击可能导致账号变更的操作**：关注/取消关注、删除内容、退出登录等
2. **不要填写用户未提供的数据到表单**
3. **不要执行用户未要求的 JavaScript**
4. **close_page 只能关闭你自己通过 new_page 创建的标签页**
5. **敏感页面**（银行、支付、设置等）上的写入操作会被强制要求用户确认

如果不确定某个操作是否安全，先询问用户，不要擅自执行。
```

---

## 四、实现步骤

### Phase 1: 基础组件实现 (2-3 天)

| 序号 | 任务 | 文件 | 优先级 |
|------|------|------|--------|
| 1.1 | 实现 AutoConnectDiscovery 自动发现 | `mcp_client/auto_connect.py` | P0 |
| 1.2 | 实现 BrowserGuard 敏感页面检测 | `mcp_client/browser_guard.py` | P0 |
| 1.3 | 实现 ChromeSession 状态模型 | `mcp_client/session_manager.py` | P0 |
| 1.4 | 扩展 MCPConnection 支持 browser-url | `mcp_client/client.py` | P0 |

### Phase 2: 模式切换核心 (2 天)

| 序号 | 任务 | 文件 | 优先级 |
|------|------|------|--------|
| 2.1 | 实现 ChromeSessionManager 模式切换 | `mcp_client/session_manager.py` | P0 |
| 2.2 | 实现 MCP Server 重启机制 | `mcp_client/manager.py` | P0 |
| 2.3 | 添加登录页检测逻辑 | `mcp_client/session_manager.py` | P1 |
| 2.4 | 实现标签页所有权追踪 | `mcp_client/session_manager.py` | P1 |

### Phase 3: 权限与保护集成 (2 天)

| 序号 | 任务 | 文件 | 优先级 |
|------|------|------|--------|
| 3.1 | 扩展 PermissionPolicy 支持 ChromeGuard | `permissions/policy.py` | P0 |
| 3.2 | 集成 HITL 敏感页面确认 | `cli/hitl_handler.py` | P0 |
| 3.3 | 加载自定义敏感规则文件 | `mcp_client/browser_guard.py` | P1 |
| 3.4 | 更新 System Prompt 说明 | `core/memory.py` | P1 |

### Phase 4: CLI 命令与调试 (1-2 天)

| 序号 | 任务 | 文件 | 优先级 |
|------|------|------|--------|
| 4.1 | 添加 /browser 相关命令 | `cli/commands.py` | P1 |
| 4.2 | 实现 /browser status 查看模式 | `cli/commands.py` | P1 |
| 4.3 | 添加 /browser connect/disconnect 手动切换 | `cli/commands.py` | P2 |
| 4.4 | 集成日志与状态显示 | `cli/renderer.py` | P2 |

---

## 五、关键实现细节

### 5.1 MCP Server 重启机制

```python
# 参考 PaiCLI 的 restartWithArgs() 设计

async def restart_chrome_with_args(
    self,
    new_args: List[str]
) -> bool:
    """
    重启 Chrome MCP Server 使用新的参数。
    
    流程：
    1. 断开现有连接
    2. 更新配置参数
    3. 重新建立连接
    4. 验证连接成功
    
    注意：重启只影响内存中的配置，不写入 config.yaml
    """
    from mcp_client.client import MCPConnection
    
    # 获取现有配置
    conn = self._connections.get("chrome")
    if not conn:
        return False
    
    # 断开现有连接
    await conn.disconnect()
    
    # 更新参数
    config = conn.config.copy()
    config["args"] = new_args
    
    # 重新连接
    new_conn = MCPConnection(config)
    success = await new_conn.connect()
    
    if success:
        self._connections["chrome"] = new_conn
        # 重新注册工具
        await self._re_register_tools(new_conn)
        return True
    
    return False
```

### 5.2 从 isolated 切换到 shared 的参数变化

```python
# isolated 模式参数（默认）
isolated_args = [
    "-y",
    "@modelcontextprotocol/server-chrome",
    "--port", "9222",
    "--isolated", "true"
]

# shared 模式参数（autoConnect）
shared_args = [
    "-y",
    "@modelcontextprotocol/server-chrome",
    "--autoConnect", "true",
    "--channel", "stable"  # 或 beta/dev
]
```

### 5.3 登录页检测启发式规则

```python
def detect_need_login(self, page_content: str, url: str = "") -> bool:
    """
    根据页面内容判断是否为登录页。
    
    检测指标：
    1. URL 包含 login/signin/auth
    2. 页面标题包含"登录""Login""Sign in"
    3. 存在密码输入框但没有用户会话特征
    4. 返回 401/403 状态码（从网络日志）
    """
    login_indicators = [
        r'login', r'signin', r'sign-in', r'auth',
        r'登录', r'登陆', r'授权'
    ]
    
    # URL 检查
    url_lower = url.lower()
    if any(pattern in url_lower for pattern in login_indicators):
        return True
    
    # 内容检查（简化版，实际可用更复杂的规则）
    content_lower = page_content.lower()
    if '<input type="password"' in content_lower:
        # 有密码框，但无用户信息
        if 'username' in content_lower or 'email' in content_lower:
            return True
    
    return False
```

---

## 六、与现有代码的兼容性

### 6.1 向后兼容

- 默认继续使用 isolated 模式，不破坏现有行为
- shared 模式需要用户显式开启 Chrome 远程调试
- 所有新功能可通过配置开关禁用

### 6.2 已有组件复用

| 已有组件 | 复用方式 |
|----------|----------|
| `ChromeLauncher` | 作为 isolated 模式的启动器 |
| `MCPConnection` | 扩展支持 browser-url 参数 |
| `MCPManager` | 集成 session manager 控制 |
| `PermissionPolicy` | 继承扩展 Chrome 专用检查 |
| `chrome_formatter.py` | 无需修改 |

---

## 七、参考文档

1. **PaiCLI CDP 登录态保持** - `/Users/lqf/projects/agentcode/WeaveMindAgent/copy_paicli/cdp_keep_login.pdf`
2. **Chrome DevTools MCP Plan** - `/Users/lqf/projects/agentcode/WeaveMindAgent/copy_paicli/conclusion/chrome_devtools_mcp_plan.md`
3. **MCP Implementation Plan** - `/Users/lqf/projects/agentcode/WeaveMindAgent/copy_paicli/conclusion/mcp_implementation_plan.md`
4. **WeaveMindAgent 源码** - `/Users/lqf/projects/agentcode/WeaveMindAgent/`

---

## 八、简历写法参考

基于 PaiCLI 的简历描述，WeaveMindAgent 可以写：

```markdown
**Chrome DevTools MCP 登录态管理**
- 设计并实现 ChromeSessionManager，支持 isolated/shared 双模式运行时切换
- 实现 CDP autoConnect 自动发现机制，通过 DevToolsActivePort 文件自动定位用户 Chrome
- 设计 BrowserGuard 策略层，对 29 个 Chrome 工具实施分级安全检查，敏感页面强制单步 HITL
- 实现标签页所有权追踪，防止 shared 模式下误关闭用户标签页
- 优化提示词引导，使 Agent 自动感知登录状态并选择合适模式
```


