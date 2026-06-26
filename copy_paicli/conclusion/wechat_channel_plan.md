# WeaveMindAgent 微信接入技术路线与实施计划

> 分析来源：`copy_paicli/wechat-skill.pdf`
>
> 目标：让用户通过微信与本机运行的 WeaveMindAgent 交互，同时保留代码检索、文件操作、Plan-Execute、Multi-Agent、MCP 等能力，并确保远程调用不会绕过本机安全边界。

> 实施状态（2026-06-18）：第一轮 MVP 已完成。已实现独立 `setup/start/status/logout` 入口、iLink 文本协议、扫码登录、凭证持久化、长轮询、消息去重、单用户绑定、FIFO 调度、typing、上下文命令、协作式取消、输出适配和 `remote_safe` 工作区只读策略。daemon、远程审批、媒体和群聊仍属于后续阶段。

---

## 一、结论先行

PaiCLI 的实现本质上不是“微信调用一个云端 Agent API”，也不是“把微信消息写进 CLI 的 stdin”，而是在本机增加一个长期运行的微信 Channel：

```text
微信用户
  ↓
iLink Bot API
  ↓ getUpdates 长轮询
本机微信消息引擎
  ↓ 队列调度
Agent 会话
  ↓
WeaveMindAgent / LLM / Tools / MCP
  ↓
微信输出适配器
  ↓ sendMessage
iLink Bot API
  ↓
微信用户
```

WeaveMindAgent 已经具备 AgentLoop、工具注册、权限策略、HITL、流式 Hook、上下文压缩和 Multi-Agent，缺少的是：

1. 与终端 UI 解耦的可复用 `AgentSession`。
2. iLink 登录、长轮询、发送消息和 typing 状态的客户端。
3. 微信消息队列、去重、身份绑定和会话路由。
4. 适合远程入口的非交互式安全策略。
5. 后台任务、取消、守护进程和微信输出适配。

推荐先做“单微信用户、单工作区、私聊文本、串行执行”的 MVP，再增加媒体、远程审批和并发会话。

---

## 二、PaiCLI 的实现拆解

### 2.1 连接层：iLink Bot API

PDF 中描述的关键流程如下：

- 扫码登录后获得 `bot_token`。
- 本地保存 token、机器人账号 ID、绑定用户 ID、工作区路径等信息。
- 使用 `getUpdates` 长轮询收消息，并保存服务端返回的 `syncBuf` 游标。
- 使用 `context_token` 将回复发回正确的聊天上下文。
- 使用 `sendMessage` 回复。
- 使用 `getConfig` 获取临时 `typing_ticket`，再通过 `sendTyping` 展示“正在输入”。
- 默认轮询约 35 秒；Agent 工作期间缩短轮询周期，并每约 5 秒刷新 typing。
- iLink 返回 session expired（PDF 中为错误码 `-14`）时重新扫码绑定。

这些协议字段来自文章说明。正式开发前应以实际可用的 iLink 接口、SDK或抓包结果完成一次协议验证，不应在尚未验证时把 URL、错误结构和字段类型写死在业务层。

### 2.2 消息引擎

PaiCLI 使用一个持续运行的事件循环，每轮处理：

1. 检查正在执行的 Agent 任务是否完成。
2. 处理 `/stop`、`/status` 等旁路命令。
3. 从 FIFO 队列取普通消息。
4. 发起下一轮 `getUpdates` 长轮询。
5. Agent 运行期间刷新 typing 状态。

入站消息先经过：

- 消息 ID 去重。
- 绑定用户校验。
- 消息类型解析。
- 斜杠命令解析。
- 普通消息入队。

PDF 展示的命令包括：

- `/help`
- `/status`
- `/clear`
- `/compact`
- `/pause`
- `/resume`
- `/stop`

### 2.3 Agent 执行

PaiCLI 为微信 Channel 创建独立 Agent 会话，并使用单线程异步执行器运行 Agent。这样长轮询、typing 和旁路命令不会被同步 Agent 调用阻塞。

文章中特别强调：

- `/stop` 必须连接到实际运行线程、`Future` 和取消上下文，不能只清空队列。
- 微信命令需要独立解析器，不能复用普通 CLI 命令的字符串前缀逻辑。
- 微信通道不能依赖终端 HITL 输入。
- v1 先做私聊，不做群聊。

### 2.4 输出适配

PaiCLI 的微信渲染器负责：

- 不发送思考过程、完整工具参数和 diff。
- 清理 ANSI 控制码。
- 简化 Markdown。
- 将长回复按约 3800 字符分段。
- 最终回复通过 `sendMessage` 发回微信。
- 后续版本按字符数或时间窗口刷新，营造流式输入体验。

---

## 三、WeaveMindAgent 现状与差距

### 3.1 可以直接复用的能力

| 现有模块 | 可复用能力 |
| --- | --- |
| `core/agent_loop.py` | ReAct、Plan-Execute、LLM 流式调用、工具调用 |
| `agents/orchestrator.py` | Multi-Agent 编排 |
| `tools/registry.py` | 内置工具、RAG、MCP 工具聚合 |
| `permissions/` | 工具权限和风险分类 |
| `core/hitl_policy.py` | 判断工具是否需要审批 |
| `hooks/manager.py` | LLM、工具和计划执行事件 |
| `core/compaction.py` | 对话压缩 |
| `core/memory.py` | 项目记忆和长期记忆 |
| `skills/` | Skill 索引和按需加载 |

### 3.2 当前阻碍

#### 1. Agent 生命周期与终端耦合

`WeaveMindCLI` 同时负责初始化、对话历史、命令、终端渲染、HITL 和 Agent 调用。微信 Channel 如果直接实例化 `WeaveMindCLI`，会继承 `prompt_toolkit`、Rich Console 和阻塞式 `console.input()`，难以后台运行。

#### 2. HITL 是同步终端输入

`TerminalHitlHandler.request_approval()` 会等待终端输入。微信任务触发 Write、Edit、Bash 或高风险 MCP 工具时，后台线程会永久等待。

#### 3. SessionManager 没有保存真实会话

当前 `core/session.py` 只保存 `message_count`，不能恢复 LangChain 消息、运行模式、审批状态和微信会话映射。

#### 4. 没有真正的任务取消

`AgentLoop.stream_with_history()` 是同步生成器，当前没有 `CancellationToken`。`/stop` 最多只能停止后续调度，不能可靠中断正在进行的 LLM、Shell 或 MCP 调用。

#### 5. LLMDelta 不能直接推送微信

现有 `LLMDelta` 既可能是最终回复，也可能是调用工具前的中间文本。直接转发会泄露中间推理、临时结论或工具过程。MVP 应只发最终答案。

---

## 四、目标架构

```text
┌──────────────────────────────────────────────────────────────┐
│                        WeaveMind Core                        │
│                                                              │
│  AgentRuntimeFactory ──► AgentSession ──► AgentLoop          │
│       │                    │               │                  │
│       │                    ├─ conversation │                  │
│       │                    ├─ modes        ├─ tools / RAG     │
│       │                    ├─ cancellation ├─ MCP / Skills    │
│       │                    └─ event sink   └─ memory          │
└───────────────────────┬──────────────────────────────────────┘
                        │
             ┌──────────┴──────────┐
             │                     │
┌────────────▼──────────┐  ┌───────▼──────────────────────────┐
│ Terminal Channel      │  │ WeChat Channel                  │
│ prompt_toolkit + Rich │  │ iLinkClient                     │
│ TerminalHitlHandler   │  │ MessageEngine                   │
└───────────────────────┘  │ WeChatCommandParser             │
                           │ WeChatSafetyPolicy              │
                           │ WeChatRenderer                  │
                           │ AccountStore / SessionStore     │
                           └──────────────────────────────────┘
```

核心原则是：终端和微信只是两个 Channel，共用 Agent 内核，但各自拥有独立的输入、输出、命令和审批策略。

---

## 五、模块设计

### 5.1 抽取 AgentSession

新增 `core/agent_session.py`，把 `cli/app.py` 中可复用的会话逻辑移入该类：

```python
class AgentSession:
    def __init__(
        self,
        agent_loop,
        session_id: str,
        workspace: Path,
        event_sink=None,
        cancellation_token=None,
    ):
        self.conversation = []
        self.plan_mode = False
        self.team_mode = False

    def run(self, user_input: str) -> AgentRunResult:
        ...

    def clear(self) -> None:
        ...

    def compact(self) -> None:
        ...

    def cancel(self) -> bool:
        ...
```

`AgentRunResult` 至少包含：

- 最终文本。
- 是否成功。
- 是否取消。
- 错误信息。
- token 统计。
- 工具调用摘要。
- 本轮新增的对话消息。

终端 CLI 改为调用 `AgentSession`，而不是自己维护一套 Agent 执行流程。微信接入后也调用同一接口，避免两套逻辑逐渐分叉。

### 5.2 AgentRuntimeFactory

新增 `core/runtime_factory.py`，统一创建：

- MemoryManager
- RAG Pipeline
- MCPManager
- SkillRegistry
- ToolRegistry
- PermissionPolicy
- AgentLoop
- AgentSession

允许不同 Channel 注入不同的：

- HITL Handler。
- PermissionPolicy。
- Hook/Event Sink。
- 工作区。
- 运行模式。

### 5.3 iLinkClient

新增 `channels/wechat/ilink_client.py`，只负责协议通信，不包含业务逻辑：

```python
class ILinkClient:
    def request_qr_code(self): ...
    def poll_login(self, login_session): ...
    def get_updates(self, sync_buf, timeout_seconds): ...
    def send_message(self, context_token, text): ...
    def get_typing_ticket(self): ...
    def send_typing(self, context_token, ticket): ...
```

要求：

- 基于项目已有的 `httpx`。
- 明确连接、读取和总超时。
- 重试只覆盖网络错误、超时和服务端可重试错误。
- 认证失效单独抛出 `SessionExpiredError`。
- 日志中禁止输出完整 token、ticket 和消息正文。
- API 地址、版本和请求头全部从配置读取。

### 5.4 AccountStore

新增 `channels/wechat/account_store.py`，建议保存到：

```text
~/.weavemind/wechat/account.json
```

数据包括：

```json
{
  "schema_version": 1,
  "bot_id": "...",
  "bot_token": "...",
  "bound_user_id": "...",
  "workspace": "/absolute/path",
  "sync_buf": "...",
  "created_at": "...",
  "updated_at": "..."
}
```

要求：

- 文件权限设为 `0600`。
- 原子写入：临时文件写完后 replace。
- 日志和 `/status` 只显示脱敏后的账号信息。
- 工作区必须是绝对路径，启动时重新校验存在性和可写边界。
- `sync_buf` 定期持久化，避免重启后重复消费大量消息。

### 5.5 MessageEngine

新增 `channels/wechat/engine.py`，职责：

- 驱动长轮询。
- 消息去重。
- 绑定用户校验。
- 解析消息。
- 命令旁路。
- 普通消息排队。
- Agent 任务调度。
- typing 刷新。
- 结果发送。
- 优雅停止和异常恢复。

MVP 调度模型：

- 只允许一个绑定用户。
- 只支持私聊文本和语音转文字。
- 全局一个 Agent Worker，普通消息 FIFO。
- `/status`、`/pause`、`/resume`、`/stop` 不进入普通队列。
- 队列设置上限，例如 20 条，超出时明确回复“队列已满”。

会话键建议使用：

```text
bot_id + sender_id + context_token + workspace
```

不能只按 sender ID 建会话，因为 `context_token` 决定消息回复到哪个聊天上下文。

### 5.6 消息解析和命令解析

新增：

- `channels/wechat/models.py`
- `channels/wechat/message_parser.py`
- `channels/wechat/commands.py`

统一把 iLink 原始消息转为：

```python
@dataclass
class InboundMessage:
    message_id: str
    sender_id: str
    context_token: str
    text: str
    attachments: list
    received_at: datetime
```

微信命令必须精确匹配命令名：

- `/help`
- `/status`
- `/clear`
- `/compact`
- `/pause`
- `/resume`
- `/stop`

不要用 `startswith("/stop")` 之类的宽松判断，避免普通文本被误判。

### 5.7 微信安全策略

MVP 不应把终端 HITL 简单关闭后继续使用默认权限。正确做法是新增 `WechatSafetyPolicy` 或微信专用权限配置，执行时失败关闭。

建议默认策略：

| 能力 | MVP 策略 |
| --- | --- |
| Read / Glob / Grep / SearchCode | 允许，路径限制在绑定工作区 |
| WebSearch / WebFetch | 允许，沿用 SSRF 防护 |
| Write / Edit | 默认拒绝；可配置为仅工作区内允许 |
| Bash | 默认拒绝；后续只开放严格 argv 白名单 |
| Browser 只读工具 | 可配置允许 |
| Browser 点击、填写、脚本 | 默认拒绝 |
| 外部 MCP 写操作 | 默认拒绝 |
| Memory 写入 | 仅允许写本项目的 WeaveMind 存储 |
| Multi-Agent Worker | 使用同一微信安全策略，不能获得更高权限 |

必须补充：

- 所有文件路径在执行前 `resolve()`，确认仍位于绑定工作区。
- 拒绝符号链接逃逸。
- Bash 白名单不能只匹配字符串前缀，应解析 argv，并拒绝管道、重定向、命令替换和 shell 控制符。
- 微信入口禁止 `bypassPermissions`。
- 所有拒绝结果作为 ToolMessage 返回给 Agent，由 Agent 给用户解释。

远程审批可作为第二阶段实现：

```text
Agent 产生危险工具调用
  ↓
生成 approval_id，暂停会话
  ↓
微信发送审批卡片/文本
  ↓
/approve <id> 或 /reject <id> [原因]
  ↓
恢复 Agent
```

该方案需要异步 `ApprovalBroker` 或 LangGraph checkpoint/interrupt。不能继续调用当前会阻塞终端输入的 `TerminalHitlHandler`。

### 5.8 取消机制

新增 `core/cancellation.py`：

```python
class CancellationToken:
    def cancel(self): ...
    def is_cancelled(self) -> bool: ...
    def raise_if_cancelled(self): ...
```

在以下边界检查：

- 每个 LangGraph 节点开始前。
- 每次 LLM 调用前后。
- 每个工具调用前。
- PlanExecutor 每个 task 前。
- Multi-Agent 每次路由前。

微信 MessageEngine 保存当前 `Future` 和 token。`/stop` 执行：

1. 设置 cancellation token。
2. 取消尚未开始的 Future。
3. 清除 typing。
4. 回复取消状态。

需要明确：Python 线程不能安全强杀。MVP 的取消是协作式取消，无法立即终止已经阻塞在第三方 SDK、Shell 子进程或 MCP 请求中的调用。Bash 工具后续应使用可终止的子进程组，HTTP/LLM 调用应设置超时。

### 5.9 WeChatRenderer

新增 `channels/wechat/renderer.py`：

- 只发送最终用户可见文本。
- 清理 ANSI 和控制字符。
- 删除不适合微信的 Markdown 装饰，但保留代码内容和列表结构。
- 按 3500～3800 字符安全切分。
- 优先按段落、换行、句号切分，避免截断代码块和 Unicode 字符。
- 发送失败时按片段重试，记录已成功片段，避免整段重复发送。

MVP 不直接转发现有 `LLMDelta`。第二阶段如要流式发送，应新增语义明确的 `FinalAnswerDelta` 事件，确保：

- 中间工具调用前的文本不发送。
- reasoning_content 不发送。
- 完整工具参数、命令输出和 diff 不发送。
- 本地终端仍可显示详细执行过程。

### 5.10 CLI 和 daemon

提供两类入口：

```bash
weavemind wechat setup
weavemind wechat start
weavemind wechat start --daemon
weavemind wechat status
weavemind wechat stop
weavemind wechat logs
```

以及 REPL 内的快捷命令：

```text
/wechat
```

`setup/status/stop/logs` 应在 LLM、RAG 和 MCP 初始化前完成参数分流，避免只查看状态也加载完整 Agent。

daemon 需要：

- PID 文件。
- 日志文件和轮转。
- 重复启动检测。
- SIGTERM/SIGINT 优雅退出。
- 异常退出后清理 PID。
- 启动健康检查。

首版可先做前台 `start`，验证稳定后再做 daemon。

---

## 六、建议文件结构

```text
channels/
├── __init__.py
├── base.py
└── wechat/
    ├── __init__.py
    ├── account_store.py
    ├── cli.py
    ├── commands.py
    ├── daemon.py
    ├── engine.py
    ├── ilink_client.py
    ├── message_parser.py
    ├── models.py
    ├── renderer.py
    ├── safety.py
    └── session_router.py

core/
├── agent_session.py
├── cancellation.py
└── runtime_factory.py

tests/
├── test_agent_session.py
├── test_wechat_account_store.py
├── test_wechat_commands.py
├── test_wechat_engine.py
├── test_wechat_ilink_client.py
├── test_wechat_renderer.py
└── test_wechat_safety.py
```

需要修改的现有文件：

| 文件 | 改动 |
| --- | --- |
| `main.py` | 增加 `wechat` 子命令的早期分流 |
| `cli/app.py` | 改为使用 `AgentSession`；增加 `/wechat` |
| `cli/commands.py` | 注册 `/wechat` |
| `core/agent_loop.py` | 注入取消检查；移除对终端输入的隐式依赖 |
| `core/plan_executor.py` | task 边界增加取消检查 |
| `agents/orchestrator.py` | 路由边界增加取消检查 |
| `tools/hitl_registry.py` | HitlHandler 类型抽象化，不再绑定 TerminalHitlHandler |
| `core/session.py` | 保存真实对话状态和微信会话映射 |
| `config.yaml.example` | 增加微信 Channel 配置 |
| `requirements.txt` | 如协议需要，再增加二维码展示等最小依赖 |

---

## 七、配置建议

```yaml
wechat:
  enabled: false
  data_dir: ~/.weavemind/wechat
  workspace: /absolute/path/to/project
  api_base_url: ""
  poll_timeout_seconds: 35
  busy_poll_timeout_seconds: 3
  typing_refresh_seconds: 5
  queue_max_size: 20
  max_reply_chars: 3800
  private_chat_only: true

  security:
    profile: remote_safe
    allow_workspace_write: false
    allow_bash: false
    allow_browser_modify: false
    allow_external_mcp_write: false

  daemon:
    pid_file: ~/.weavemind/wechat/wechat.pid
    log_file: ~/.weavemind/wechat/wechat.log
```

敏感 token 不放入 `config.yaml`，只放在权限为 `0600` 的账号存储中。

---

## 八、分阶段实施

### Phase 0：协议验证

目标：验证 iLink 接口确实可用，避免先写完整业务再发现认证或协议不成立。

任务：

1. 验证二维码申请和扫码确认。
2. 验证 token、bot ID、绑定用户 ID 的返回格式。
3. 验证 `getUpdates`、`syncBuf`、`context_token`。
4. 验证 `sendMessage`。
5. 验证 session expired、限流和超时错误。
6. 验证 typing ticket 的生命周期。
7. 记录真实 JSON 样例并脱敏保存为测试 fixture。

完成标准：使用独立脚本完成“扫码 -> 收到一条微信文本 -> 原样回显”。

### Phase 1：解耦 AgentSession

目标：让终端和微信都能调用同一个无 UI Agent 会话。

任务：

1. 新增 `AgentSession` 和 `AgentRunResult`。
2. 把 `cli/app.py` 中的对话、压缩和模式逻辑迁入 Session。
3. 抽象 HitlHandler 接口。
4. 增加 CancellationToken 的基础结构。
5. 保证现有 CLI 行为和测试不回归。

完成标准：测试中无需启动 prompt_toolkit，即可创建 Session、连续对话并取得最终文本。

### Phase 2：微信文本通道 MVP

目标：跑通单用户、单工作区、私聊文本闭环。

任务：

1. 实现 AccountStore、ILinkClient。
2. 实现扫码 setup。
3. 实现长轮询、去重、身份绑定。
4. 实现 FIFO 队列和单线程 Agent Worker。
5. 实现 `/help`、`/status`、`/clear`、`/pause`、`/resume`。
6. 实现最终文本清理、分段和发送。
7. 应用 `remote_safe` 策略。

完成标准：

- 未绑定用户消息不会进入 Agent。
- 连续发 3 条消息按顺序处理。
- 重复消息 ID 只执行一次。
- Agent 输出不包含 reasoning、ANSI、工具参数和 diff。
- 高风险工具默认被拒绝且不会阻塞线程。

### Phase 3：任务取消、typing 和会话持久化

目标：达到可日常使用的交互体验。

任务：

1. `/stop` 连接 Future 和 CancellationToken。
2. Agent 工作时定时刷新 typing。
3. 保存完整对话历史和会话映射。
4. 重启后恢复 syncBuf 和最近会话。
5. 处理 token 失效并引导重新绑定。
6. 增加网络重试、指数退避和熔断。

完成标准：

- Agent 运行时消息引擎仍能响应 `/status` 和 `/stop`。
- 服务重启后不会重复执行已处理消息。
- 网络短暂中断后能自动恢复轮询。

### Phase 4：daemon 和可观测性

目标：支持长期后台运行。

任务：

1. `start --daemon`、`status`、`stop`、`logs`。
2. PID、日志轮转、信号处理。
3. 结构化日志：poll、queue、agent、send、security。
4. 指标：消息延迟、队列长度、Agent 时长、失败率、重连次数。

完成标准：后台运行 24 小时，无重复消费、僵尸进程和凭证泄漏。

### Phase 5：增强能力

按优先级选择：

1. 远程审批 `/approve`、`/reject`。
2. 真正的最终答案流式事件。
3. 图片和文件下载、大小限制、AES/CDN 处理。
4. 多 context_token 独立会话。
5. 多用户白名单。
6. 群聊 @ 触发。

媒体和群聊不建议进入 MVP，它们会显著增加鉴权、存储、提示注入和隐私风险。

---

## 九、测试计划

### 9.1 单元测试

- AccountStore 原子写、权限和脱敏。
- 消息 ID 去重和 TTL 淘汰。
- 用户绑定校验。
- context_token 路由。
- 命令精确匹配。
- Markdown/ANSI 清理和长文本切分。
- 工作区路径逃逸、符号链接逃逸。
- Bash 控制符和命令注入拒绝。
- session expired 转换。

### 9.2 集成测试

使用 `httpx.MockTransport` 或本地 Fake iLink Server：

- 长轮询超时后继续下一轮。
- syncBuf 更新。
- 网络错误退避。
- 同一消息重复返回。
- Agent 忙时消息继续入队。
- typing 定时刷新和任务结束后停止。
- sendMessage 部分失败重试。

### 9.3 Agent 回归测试

- 普通 ReAct。
- Plan-Execute。
- Multi-Agent。
- RAG。
- MCP。
- `/clear` 和 `/compact`。
- 取消发生在 LLM 前、工具前和工具后。

### 9.4 真机验收

1. 扫码绑定。
2. 发送“解释当前项目架构”。
3. 连发多条消息验证队列。
4. 发送会触发 Write/Bash 的请求，确认远程安全策略生效。
5. Agent 运行期间执行 `/status` 和 `/stop`。
6. 重启进程，验证不重复消费旧消息。
7. token 失效后验证重新绑定提示。

---

## 十、主要风险与处理

| 风险 | 处理方式 |
| --- | --- |
| iLink 协议、开放范围或使用条款不明确 | Phase 0 先验证；协议隔离在 ILinkClient |
| 微信成为本机远程命令入口 | remote_safe、单用户绑定、工作区沙箱、默认拒绝高风险工具 |
| 终端 HITL 导致后台线程阻塞 | 微信不用 TerminalHitlHandler；MVP 失败关闭 |
| LLMDelta 泄露思考和中间文本 | MVP 仅发送最终答案 |
| `/stop` 无法中断阻塞调用 | 协作式取消 + 超时；Bash 后续使用可杀进程组 |
| 重启后重复消费 | 持久化 syncBuf 和去重窗口 |
| 回复发错聊天窗口 | 发送时必须携带原消息 context_token |
| 多消息并发污染上下文 | MVP 单 Worker；后续按会话键隔离 Session |
| token 泄漏 | 0600 存储、日志脱敏、不进入 Git |
| 图片/文件提示注入或超大文件 | MVP 不支持；后续做类型、大小、病毒和内容策略 |

---

## 十一、推荐的第一轮开发范围

第一轮只做以下内容：

1. Phase 0 的 iLink 文本回显验证。
2. 抽取 `AgentSession`。
3. 单用户、单工作区、私聊文本。
4. `/help`、`/status`、`/clear`、`/pause`、`/resume`。
5. 最终答案发送，不做增量流式。
6. `remote_safe`：只读代码和网络检索，默认禁止 Bash、任意写文件和外部写操作。
7. 前台运行，不做 daemon。

这条路线改动面最小，也最容易验证架构是否正确。文本闭环稳定后，再加入 `/stop`、typing、持久化、daemon 和远程审批。
