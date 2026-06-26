# 面试复盘：追问应对与 WeaveMind 改进方案

> 来源：2026/05/25 面试中面试官的追问记录。
> 每题三段：**考点分析**（面试官在测什么）→ **参考回答**（怎么答）→ **WeaveMind 改进落地**（怎么改项目，下次面试变成主动输出的弹药）。
> 文末有改进优先级路线图。

面试官问题清单（原文提炼）：

1. Playwright 和 CDP 的取舍
2. 用户级 Skill 包含运行脚本，涉及安全问题 → 运行沙箱、安全沙箱
3. 下载 Skill 怎么实现
4. 增量同步：能不能用 git 日志来校验文件修改
5. AST 新老比较；AST 能不能看到函数依赖关系
6. Tool call 输入不存在的文件（要读文件，产生幻觉）→ 怎么加入确定性
7. 多 Agent：子 Agent 上下文独立性
8. 工作流人工确认、IO 中断：中断前把状态保存，因为恢复请求可能路由到不同的服务器
9. 文件云存储方案
10. 让普通用户通过自然语言也能调用 Agent 能力

总体观察：**1-6 题考你对自己项目每个技术点的边界认知**（"你做的方案我换个方案行不行/你的方案缺什么"），**7-10 题考从单机 CLI 到多用户服务的工程化思维**——后者正好是你 Java 后端（FlowMind/RagNexus）经验的主场，要主动往那边接。

---

## Q1 Playwright 和 CDP 的取舍

### 考点分析

技术选型判断力。面试官想确认你知道两者**不在同一层**：CDP 是 Chrome 的底层调试协议，Playwright 是构建在驱动协议之上的自动化框架（自动等待、selector 引擎、跨浏览器、上下文隔离、trace 录制）。如果答成"差不多，随便选的"就露怯。

### 参考回答

> 这两个不在一个抽象层：CDP 是协议，Playwright 是框架（Chromium 下它底层也走 CDP，但加了驱动层和大量工程化能力）。我选 CDP（chrome-devtools-mcp）有三个理由：
>
> ① **核心需求是 attach 到用户正在使用的 Chrome、复用登录态**——我的 shared 模式是读 `DevToolsActivePort` 文件拿 wsEndpoint 直连用户浏览器。Playwright 的主路径是 launch 自己管理的浏览器实例，虽然有 `connect_over_cdp` 但属于非主流用法，部分 API 在外接实例上受限。
>
> ② **MCP 生态**：chrome-devtools-mcp 是官方维护的 MCP Server，工具 Schema 现成，直接进我的统一注册管线；用 Playwright 我得自己把几十个 API 包装成 LLM 工具并维护 Schema。
>
> ③ **依赖重量**：Playwright 要下载自己的浏览器二进制（几百 MB），对一个本地 CLI 工具偏重。
>
> 反过来，Playwright 的优势我也清楚：auto-wait 大幅减少时序型 flaky、selector 引擎比手写 JS 提取健壮、跨浏览器、trace 可回放调试。**结论是按场景分**：确定性脚本自动化和 E2E 测试选 Playwright；"Agent 操作用户真实浏览器"选 CDP attach。我的浏览器循环检测（重复截图/无导航 evaluate_script）某种程度上就是在补 CDP 缺少 auto-wait 带来的不稳定。

加分句：两者可以混合——`playwright.connect_over_cdp()` 可以在已连接的 CDP 实例上获得 Playwright 高层 API，isolated 模式完全可以换 Playwright 后端。

### WeaveMind 改进落地

- 在 `mcp_client/` 上抽一个 `BrowserBackend` 接口：isolated 模式提供 Playwright 实现（拿 auto-wait 和 selector 的稳定性），shared 模式保留 CDP attach。
- 把"截图→快照→evaluate_script"的提取流程封装成更高层的 `extract_page_content` 工具，减少 LLM 自由发挥导致的循环（当前靠 `_detect_browser_loop` 事后兜底，不如事前收敛动作空间）。

---

## Q2 用户级 Skill 含运行脚本的安全问题（沙箱）

### 考点分析

安全意识 + 分层防御思维。这是 Claude Code Skills 真实面对的问题（skill 可以带可执行脚本）。先讲清楚自己项目的现状边界，再展开"如果支持脚本怎么设计"，体现的是诚实 + 设计能力。

### 参考回答

> 先说现状：WeaveMind 的 Skill 目前是**纯 markdown 指引**，body 注入上下文影响 LLM 决策，本身不直接执行——但这不等于没有安全面：skill 内容会被 LLM 当指令执行，恶意 skill 可以诱导 Agent 调 Bash 删文件，这本质是 **prompt injection 走工具调用变成 RCE**。如果进一步支持 skill 自带脚本，要做四层防御：
>
> **① 信任分级**：builtin（随发行版，可信）/ project（仓库内，半可信——进仓库要过 code review）/ user 和下载的（不可信）。不可信来源默认 disabled，启用前要求用户 review。
>
> **② 安装期静态防线**：内容扫描（危险命令模式、典型注入话术）；记录内容 hash，TOFU 模式——首次信任后内容变更即告警重新确认（防"先发布良性版本再恶意更新"的供应链手法）。
>
> **③ 权限声明 + 运行时强制**：SKILL.md frontmatter 声明 `allowed_tools`、`network: true/false`、`fs_scope`（可访问目录），安装时展示给用户授权；运行时在**代码层**强制（skill 加载期间收紧 PermissionPolicy 白名单），超出声明直接拒绝。关键原则：**约束放在代码层而不是 prompt 层**——prompt 里写"不要执行危险命令"是防不住注入的，skill 文本无法解除 HITL 审批，因为审批逻辑在 `_act` 的代码里。
>
> **④ 执行沙箱**（如果跑脚本）：按隔离强度递进——进程级（独立工作目录 + rlimit 资源限制 + 超时 + 网络禁用）→ OS 级（macOS sandbox-exec / Linux bubblewrap+seccomp，只读文件系统+目录白名单）→ 容器级（`docker run --network=none --read-only --memory/--cpus` 限额）→ 微 VM（gVisor/Firecracker，多租户服务端才需要）。本地 CLI 我会选 OS 级沙箱：够强且无 Docker 依赖。

### WeaveMind 改进落地

- `skills/models.py` 的 `Skill` 加 `trust: T