# CDP 双模式修复设计 — 单 MCP Server + browser_connect 工具

日期: 2026-05-14

## 问题

当前 WeaveMind 的 CDP 功能无法正常工作。用户期望的流程：
1. Agent 用 isolated 模式访问 URL
2. 发现需要登录
3. 切换到 shared 模式（连接用户已登录的 Chrome）
4. 获取内容

但当前实现走不通，原因：
- MCP Server 启动参数错误（`--browserUrl` 需要 Chrome 已在 9222 端口监听）
- ChromeLauncher 启动逻辑有缺陷（独立 user-data-dir 与已有 Chrome 冲突）
- 模式切换依赖 DevToolsActivePort 文件（只在 `--remote-debugging-port` 启动时存在）
- BrowserGuard 缺少执行后状态更新
- 自动切换逻辑重叠且事件循环处理有 bug
- HITL 缺少敏感页面逐次审批

## 设计方案

采用 PaiCLI 方式：单 MCP Server + 重启切换 + 内置 browser_connect 工具。

### 核心流程

```
用户: "帮我看一下语雀链接"
  ↓
AgentLoop → LLM 选择 navigate(url)  [MCP Server: --isolated 模式]
  ↓
MCP Server (isolated) → 匿名 Chrome 打开 → 返回登录页
  ↓
AgentLoop → LLM 看到需要登录 → 选择 browser_connect
  ↓
browser_connect 执行:
  1. 杀掉当前 MCP Server (isolated)
  2. 以 --autoConnect 重新启动 MCP Server
  3. 重新注册工具（工具名不变）
  4. 清除 HITL 审批缓存
  ↓
AgentLoop → LLM 再次选择 navigate(url)  [MCP Server: --autoConnect 模式]
  ↓
MCP Server (shared) → 用户已登录 Chrome 打开 → 返回内容
  ↓
AgentLoop → LLM 总结内容返回
```

### 模块改动

#### 1. mcp_client/manager.py — 支持重启切换

- 新增 `switch_mode(new_mode: str)` 方法
  - 保存当前 args 作为回滚参数
  - 断开当前 MCP 连接
  - 以新参数（`--isolated` 或 `--autoConnect`）重启 MCP Server
  - 重新注册工具到 MCPManager._tools 和 ToolRegistry._tools
  - 失败时回滚到旧参数
- 新增 `current_mode` 属性（`isolated` | `shared`）
- 默认以 `--isolated` 启动

#### 2. mcp_client/client.py — 支持不同启动参数

- `MCPConnection` 构造函数不变，mode 由 args 参数决定
- 重启时创建新的 `MCPConnection` 实例

#### 3. mcp_client/tools.py — 注册 browser_connect 内置工具

新增三个内置工具（参考 PaiCLI 的 ToolRegistry.registerBrowserTools()）：

- **browser_connect**: "当浏览器页面返回登录页、权限不足或明确需要登录态时，连接已允许远程调试的本机 Chrome 并复用其登录态。公开页面不要提前调用。"
- **browser_disconnect**: "完成登录态页面访问后，切回 isolated 浏览器模式。"
- **browser_status**: "查看当前浏览器 MCP 模式、连接状态。"

这些工具注册到 ToolRegistry，不是 MCP Server 提供的。

#### 4. mcp_client/browser_guard.py — 补全执行后逻辑

新增 `apply_after_execution(tool_name, args, result)` 方法：
- 记录导航 URL 到 `last_navigated_url`（navigate_page / new_page 工具）
- 记录新标签页 ID 到 `agent_opened_tabs`（new_page 工具）
- 从结果中提取 pageId（正则匹配 `page[-_][A-Za-z0-9_-]+`）

新增 `detect_login_page(result_str, url)` 方法：
- URL 包含 login/signin/auth/登录/登陆 关键词
- 页面内容包含密码输入框 + 用户名/邮箱输入框
- 页面内容包含"登录"/"sign in"/"log in"文本

#### 5. core/agent_loop.py — 简化模式切换

- 移除 `_try_auto_switch_on_login` 复杂逻辑
- 移除 `_auto_switch_to_shared` 和 `_refresh_tools_after_switch`
- 模式切换完全由 LLM 通过 `browser_connect` 工具驱动
- `browser_connect` 执行后，MCPManager 重启 MCP Server 并更新工具列表
- AgentLoop 下一轮 think 自动使用新工具（无需手动刷新）

#### 6. mcp_client/chrome_launcher.py — 简化

- 保留 `is_running()` 和 `_check_port()` 方法（用于 browser_status 显示）
- 移除 `start()` 和 `stop()` 方法（不再需要手动启动 Chrome）
- `--isolated` 模式由 MCP Server 自己管理 Chrome 实例
- `--autoConnect` 模式连接用户已打开的 Chrome

#### 7. mcp_client/auto_connect.py — 清理

- 移除 `AutoConnectDiscovery` 类（`--autoConnect` 原生支持，不需要读取 DevToolsActivePort）
- 保留文件但标记为已废弃，后续可完全删除

#### 8. mcp_client/session_manager.py — 清理

- 移除 `ChromeSessionManager` 类（会话状态合并到 BrowserGuard）
- 保留文件但标记为已废弃，后续可完全删除

#### 9. config.yaml — 更新默认配置

```yaml
mcp:
  servers:
    chrome-devtools:
      command: npx
      args: ["-y", "chrome-devtools-mcp@latest", "--isolated"]
      enabled: true
      env:
        PATH: "/opt/homebrew/bin:${PATH}"
```

移除 `chrome` 子配置（auto_start/headless/port/executable 不再需要）。

#### 10. permissions/policy.py — HITL 敏感页面处理

- `browser_connect` 标记为需要确认（切换到用户已登录的 Chrome 是敏感操作）
- 敏感页面的写操作强制逐次审批（不可复用"全部放行"）
- 新增 `needs_per_call_approval(tool_name, url)` 方法

#### 11. cli/commands.py — 更新 /browser 命令

- `/browser status` — 显示当前模式、MCP Server 状态、Chrome 调试端口探活
- `/browser connect` — 手动切换到 shared 模式（使用 --autoConnect）
- `/browser connect <port>` — 旧式端口连接（使用 --browserUrl）
- `/browser disconnect` — 切换回 isolated 模式

### 不改动的部分

- `mcp_client/chrome_formatter.py` — 不变，格式化逻辑正确
- `mcp_client/client.py` 的 `MCPConnection` — 不变，只改 manager 的重启逻辑
- `tools/registry.py` — 不变，browser_connect 等工具通过 MCPManager 注册
- 测试文件 — 更新以匹配新逻辑

### 关键设计决策

1. **LLM 自主决策 vs 代码自动切换**: 选择 LLM 自主决策（通过 browser_connect 工具），与 PaiCLI 一致。原因：代码自动切换需要复杂的异步事件循环处理，容易出 bug；LLM 可以根据上下文判断是否真的需要切换。

2. **重启 MCP Server vs 双实例**: 选择重启。原因：与 PaiCLI 一致，资源占用少，工具名不变，LLM 不需要学习新的工具名。

3. **--autoConnect vs --browserUrl**: 优先使用 --autoConnect（Chrome 144+ 原生支持），回退到 --browserUrl（旧式端口连接）。

4. **BrowserGuard 执行后更新**: 必须补全，否则 last_navigated_url 和 agent_opened_tabs 无法维护，导致敏感页面检测和标签页保护失效。