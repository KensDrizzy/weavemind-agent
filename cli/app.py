"""WeaveMindCLI — 顶层编排器 + REPL。"""

import sys
import logging

from langchain_core.messages import AIMessage, HumanMessage
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.completion import WordCompleter, FuzzyCompleter
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.panel import Panel

from cli.commands import handle_command
from cli.direct_intent import DirectIntentHandler
from cli.hitl_handler import TerminalHitlHandler
from cli.renderer import (
    InteractionStreamRenderer,
    print_plan_created,
    print_plan_progress,
    print_plan_result,
)
from core.agent_loop import AgentLoop
from core.memory import MemoryManager
from core.plan_models import Plan
from hooks.manager import HookManager
from permissions.modes import PermissionMode
from permissions.policy import PermissionPolicy
from tools.hitl_registry import HitlToolRegistry
from core.session import SessionManager
from mcp_client.manager import MCPManager
from core.multimodal.image_loader import capture_clipboard_image, load_image_parts
from core.multimodal.image_reference import ImageRef, parse_image_refs, replace_image_refs, strip_image_refs
from core.multimodal.message_builder import build_multimodal_message
from core.multimodal.content_part import ImageBase64Part
import settings

console = Console()
logger = logging.getLogger(__name__)

MAX_CONVERSATION_MESSAGES = 40  # 20 轮 × 2 条/轮


class WeaveMindCLI:
    def __init__(self, hitl_enabled: bool = False):
        self.permission_policy = PermissionPolicy()
        self.hook_manager = HookManager()
        self.memory = MemoryManager()
        self.session_manager = SessionManager()
        self.mode = PermissionMode.DEFAULT
        self.plan_mode = False
        self.team_mode = False
        self.conversation: list = []  # 对话历史
        self.stream_details_expanded = False
        self.stream_renderer = InteractionStreamRenderer(
            rich_console=console,
            expanded=self.stream_details_expanded,
        )
        self._register_stream_hooks()

        # HITL 人工审批处理器（默认启用）
        self.hitl_handler = TerminalHitlHandler()
        self._has_shown_hitl_hint = False  # 是否已显示首次危险操作提示

        # 默认启用 HITL（可通过 /hitl off 关闭）
        self.hitl_handler.set_enabled(True)

        # 根据参数禁用 HITL
        if hitl_enabled is False:
            self.hitl_handler.set_enabled(False)

        # 根据配置文件禁用
        if settings.get("hitl.enabled", True) is False:
            self.hitl_handler.set_enabled(False)

        # 根据环境变量禁用
        import os
        if os.environ.get("HITL_ENABLED", "").lower() == "false":
            self.hitl_handler.set_enabled(False)

        # 初始化 RAG Pipeline（如果启用）
        self.rag_pipeline = None
        if settings.get("rag.enabled", False):
            try:
                from rag.pipeline import CodeRAGPipeline
                self.rag_pipeline = CodeRAGPipeline()
                logger.info("RAG Pipeline 已初始化")
            except Exception as e:
                logger.warning(f"RAG Pipeline 初始化失败: {e}")

        self.knowledge_pipeline = None
        if settings.get("knowledge_rag.enabled", False):
            try:
                from knowledge_rag.pipeline import KnowledgeRAGPipeline
                self.knowledge_pipeline = KnowledgeRAGPipeline()
                logger.info("Knowledge RAG Pipeline 已初始化")
            except Exception as e:
                logger.warning(f"Knowledge RAG Pipeline 初始化失败: {e}")

        # 初始化 MCP Manager（异步初始化在 run() 中执行）
        self.mcp_manager = MCPManager()
        self._mcp_initialized = False

        # 初始化 Skill 系统
        self._init_skills()

        self._create_agent_loop()
        self._direct_intent = DirectIntentHandler(self.tool_registry)

        self._ctrl_c_count = 0
        self._ctrl_c_window = 1.0  # 双击时间窗口（秒）
        self._has_shown_rag_hint = False  # 是否已显示 RAG 提示
        self._pending_clipboard_parts: list[ImageBase64Part] = []  # 热键预捕获的剪贴板图片
        self._has_shown_image_privacy_hint = False  # 是否已显示图片隐私提示

        # 创建命令补全器
        commands = [
            "/help", "/memory", "/save", "/sessions", "/mode", "/plan", "/team",
            "/hitl", "/mcp", "/browser", "/skill", "/index", "/search", "/kb", "/clear", "/exit", "/quit"
        ]
        completer = FuzzyCompleter(WordCompleter(commands, ignore_case=True))
        key_bindings = self._build_key_bindings()

        # prompt_toolkit 会话，支持方向键历史和命令补全
        self.prompt_session = PromptSession(
            history=FileHistory(".weavemind/cmd_history"),
            completer=completer,
            complete_while_typing=True,
            key_bindings=key_bindings,
        )

    def _register_stream_hooks(self):
        """注册流式渲染相关 Hook。"""
        self.hook_manager.register("LLMStart", self.stream_renderer.on_llm_start)
        self.hook_manager.register("LLMDelta", self.stream_renderer.on_llm_delta)
        self.hook_manager.register("LLMEnd", self.stream_renderer.on_llm_end)
        self.hook_manager.register("PreToolUse", self.stream_renderer.on_tool_start)
        self.hook_manager.register("PostToolUse", self.stream_renderer.on_tool_end)
        self.hook_manager.register("PlanStart", self.stream_renderer.on_plan_start)
        self.hook_manager.register("PlanCreated", self.stream_renderer.on_plan_created)
        self.hook_manager.register("PlanExecuteStart", self.stream_renderer.on_plan_execute_start)
        self.hook_manager.register("PlanExecuteEnd", self.stream_renderer.on_plan_execute_end)

    def _build_key_bindings(self):
        """定义交互快捷键。"""
        bindings = KeyBindings()

        @bindings.add("c-o")
        def _toggle_stream_details(_event):
            self.stream_details_expanded = not self.stream_details_expanded
            self.stream_renderer.set_expanded(self.stream_details_expanded)
            status = "展开" if self.stream_details_expanded else "收起"
            console.print(f"\n[dim]流式详情已{status}[/dim]")

        @bindings.add("f5")
        def _paste_clipboard_image(event):
            """F5 捕获剪贴板图片并插入 @clipboard 标记。"""
            if not self._has_shown_image_privacy_hint:
                console.print("\n[yellow]提示：图片将上传至当前 LLM provider。[/yellow]")
                self._has_shown_image_privacy_hint = True

            try:
                processed = capture_clipboard_image()
            except Exception as e:
                console.print(f"\n[red]剪贴板图片捕获失败: {e}[/red]")
                return

            part = ImageBase64Part(data=processed.data, mime_type=processed.mime_type)
            self._pending_clipboard_parts.append(part)

            buffer = event.app.current_buffer
            buffer.insert_text(" @clipboard ")
            size = len(processed.data) * 3 // 4
            console.print(
                f"\n[dim][已插入剪贴板图片: {processed.mime_type}, "
                f"{processed.width}x{processed.height}, base64≈{size} bytes][/dim]"
            )

        return bindings

    def _init_skills(self):
        """初始化 Skill 系统。"""
        from pathlib import Path
        from skills.registry import SkillRegistry
        from skills.state_store import SkillStateStore
        from skills.buffer import SkillContextBuffer

        builtin_dir = Path(__file__).parent.parent / "skills" / "builtin"
        user_dir = Path.home() / ".weavemind" / "skills"
        project_dir = Path(".weavemind") / "skills"
        state_file = Path.home() / ".weavemind" / "skills.json"

        self.skill_state_store = SkillStateStore(state_file)
        self.skill_registry = SkillRegistry(builtin_dir, user_dir, project_dir, self.skill_state_store)
        self.skill_registry.reload()
        self.skill_buffer = SkillContextBuffer()

    def _create_agent_loop(self, force_plan: bool = False):
        """重建 ToolRegistry 和 AgentLoop，同时重建意图直达处理器。"""
        self.tool_registry = HitlToolRegistry(
            hitl_handler=self.hitl_handler,
            memory_manager=self.memory,
            rag_pipeline=self.rag_pipeline,
            knowledge_pipeline=self.knowledge_pipeline,
            mcp_manager=self.mcp_manager if self._mcp_initialized else None,
        )
        # 注册 load_skill 工具
        if self.skill_registry:
            from tools.builtin.skill_tools import LoadSkillTool
            self.tool_registry.register(LoadSkillTool(
                skill_registry=self.skill_registry,
                skill_buffer=self.skill_buffer,
            ))
        # 让 MCPManager 持有 ToolRegistry 引用，以便模式切换时同步更新
        self.mcp_manager._tool_registry = self.tool_registry
        self._direct_intent = DirectIntentHandler(self.tool_registry)
        self.agent_loop = AgentLoop(
            tool_registry=self.tool_registry,
            permission_policy=self.permission_policy,
            hook_manager=self.hook_manager,
            memory=self.memory,
            force_plan_mode=force_plan,
            mcp_manager=self.mcp_manager if self._mcp_initialized else None,
            skill_registry=self.skill_registry,
            skill_buffer=self.skill_buffer,
        )

    def _init_mcp_sync(self):
        """同步方式初始化 MCP（在 REPL 启动前执行）。

        使用持久后台线程运行事件循环，避免 asyncio.run() 结束时
        关闭事件循环导致 MCP stdio 连接的异步生成器被强制关闭
        （anyio TaskGroup 的 cancel scope 跨 task 退出会报 RuntimeError）。
        """
        import asyncio
        try:
            # 创建持久事件循环（在后台线程中运行，不会提前关闭）
            self._mcp_loop = asyncio.new_event_loop()

            # 注入持久事件循环到 MCPManager，供 browser_connect 等工具使用
            self.mcp_manager.set_mcp_loop(self._mcp_loop)

            import threading
            self._mcp_thread = threading.Thread(
                target=self._mcp_loop.run_forever,
                name="mcp-event-loop",
                daemon=True,
            )
            self._mcp_thread.start()

            # 在持久事件循环中执行 MCP 初始化
            future = asyncio.run_coroutine_threadsafe(
                self._async_init_mcp(), self._mcp_loop
            )
            future.result(timeout=30)  # 等待初始化完成
        except Exception as e:
            logger.warning(f"MCP 初始化失败（不影响主流程）: {e}")

    async def _async_init_mcp(self):
        """异步初始化 MCP Manager 并重建 AgentLoop。"""
        try:
            success = await self.mcp_manager.initialize()
            if success:
                self._mcp_initialized = True
                # MCP 初始化后重建 AgentLoop（含 MCP 工具）
                self._create_agent_loop()

                # 注入 BrowserGuard 到 PermissionPolicy
                browser_guard = self.mcp_manager.get_browser_guard()
                if browser_guard:
                    self.permission_policy.set_browser_guard(browser_guard)

                # 显示 MCP 工具信息
                provider = settings.get("llm.provider", "mimo")
                model = settings.get("llm.model", "unknown")
                console.print(f"[dim]🧠 Model: {provider}/{model}[/dim]")
                mcp_info = self.mcp_manager.get_tools_info()
                if mcp_info:
                    console.print("\n[dim]📡 MCP Servers:[/dim]")
                    for server, tools in mcp_info.items():
                        tool_preview = ", ".join(tools[:3])
                        if len(tools) > 3:
                            tool_preview += f"... (+{len(tools)-3})"
                        console.print(f"[dim]   • {server}: {tool_preview}[/dim]")
                    console.print()

                # 显示 Skill 加载信息
                self._print_skills_info()
        except Exception as e:
            logger.warning(f"MCP 异步初始化失败: {e}")

    def _cleanup_mcp(self):
        """退出时清理 MCP 资源。"""
        import asyncio
        try:
            if self._mcp_initialized and hasattr(self, '_mcp_loop') and self._mcp_loop:
                # 在持久事件循环中执行 shutdown
                future = asyncio.run_coroutine_threadsafe(
                    self.mcp_manager.shutdown(), self._mcp_loop
                )
                future.result(timeout=10)

                # 停止事件循环
                self._mcp_loop.call_soon_threadsafe(self._mcp_loop.stop)
        except Exception as e:
            logger.debug(f"MCP 清理时出错: {e}")

    def _should_auto_team(self, user_input: str) -> bool:
        """判断是否应自动使用 Team 模式。"""
        import settings
        if not settings.get("team.auto_detect", True):
            return False
        # 去掉图片引用，避免分类模型被 @image 标记干扰
        clean_input = strip_image_refs(user_input)
        complexity = self.agent_loop.classify_complexity(clean_input)
        logger.info(f"任务复杂度: {complexity}")
        return complexity == "complex"

    def _print_skills_info(self):
        """打印 Skill 加载汇总。"""
        if not self.skill_registry:
            return
        enabled = self.skill_registry.enabled_skills()
        if not enabled:
            return
        console.print(f"[dim]📚 Skills 加载（{len(enabled)} 个）...[/dim]")
        for skill in enabled:
            desc = skill.description.replace("\n", " ").strip()
            # 按显示宽度截断（CJK 字符占 2 列）
            max_width = 80
            width = 0
            cut = len(desc)
            for i, ch in enumerate(desc):
                width += 2 if ord(ch) > 0x7F else 1
                if width > max_width:
                    cut = i
                    break
            if cut < len(desc):
                desc = desc[:cut] + "..."
            console.print(f"[dim]   ✓ {skill.name:<16} {skill.display_source():<8} {desc}[/dim]")

    def _print_banner(self):
        """打印启动欢迎界面。"""
        banner = r"""
╭───────────────────────────────────────────────────────────────────────────────╮
│                                                                               │
│  [bold cyan]██     ██ ███████  █████  ██    ██ ███████ ███    ███ ██ ███    ██ ██████ [/bold cyan]   │
│  [cyan]██     ██ ██      ██   ██ ██    ██ ██      ████  ████ ██ ████   ██ ██   ██[/cyan]   │
│  [cyan]██  █  ██ █████   ███████ ██    ██ █████   ██ ████ ██ ██ ██ ██  ██ ██   ██[/cyan]   │
│  [cyan]██ ███ ██ ██      ██   ██  ██  ██  ██      ██  ██  ██ ██ ██  ██ ██ ██   ██[/cyan]   │
│  [cyan] ███ ███  ███████ ██   ██   ████   ███████ ██      ██ ██ ██   ████ ██████ [/cyan]   │
│                                                                               │
│                                [dim]AGENT CLI[/dim]                                      │
│                                                                               │
╰───────────────────────────────────────────────────────────────────────────────╯
"""
        console.print(banner)

    def run(self):
        """启动 REPL。"""
        # 打印立体文 WeaveMind 标题
        self._print_banner()
        
        # 异步初始化 MCP
        self._init_mcp_sync()

        # 显示 Skill 加载信息（独立于 MCP）
        if not self._mcp_initialized:
            self._print_skills_info()
        
        console.print(Panel(
            "[bold]WeaveMind Agent[/bold]\n"
            "输入问题开始对话 | /help 查看命令 ",
            border_style="blue",
            padding=(1, 2),
        ))

        while True:
            try:
                # 模式指示器
                mode_hint = ""
                if self.plan_mode:
                    mode_hint = " PLAN"
                if self.team_mode:
                    mode_hint = " TEAM"
                if self.mode != PermissionMode.DEFAULT:
                    mode_hint += f" [{self.mode.value}]"
                if self.stream_details_expanded:
                    mode_hint += " [details]"

                # 使用 prompt_toolkit 获取输入（支持方向键历史）
                user_input = self.prompt_session.prompt(
                    f"\nYou{mode_hint}> ",
                ).strip()

                if not user_input:
                    continue

                # 正常输入后重置 Ctrl+C 计数
                self._ctrl_c_count = 0

                # 斜杠命令
                if user_input.startswith("/"):
                    result = handle_command(
                        user_input,
                        self.agent_loop,
                        self.session_manager,
                        rag_pipeline=self.rag_pipeline,
                        knowledge_pipeline=self.knowledge_pipeline,
                        mcp_manager=self.mcp_manager,
                    )

                    if result == "plan_mode":
                        self.plan_mode = not self.plan_mode
                        self._create_agent_loop(force_plan=self.plan_mode)
                        status = "开启" if self.plan_mode else "关闭"
                        console.print(f"\n[cyan]📋 Plan-Execute 模式已{status}[/cyan]\n")
                        continue

                    if result == "team_mode":
                        self.team_mode = not self.team_mode
                        status = "开启" if self.team_mode else "关闭"
                        console.print(f"\n[cyan]👥 Multi-Agent 模式已{status}[/cyan]\n")
                        continue

                    if result == "clear":
                        self.conversation.clear()
                        console.print("[dim]对话历史已清空[/dim]")
                        continue

                    if isinstance(result, str) and result in [
                        PermissionMode.DEFAULT,
                        PermissionMode.ACCEPT_EDITS,
                        PermissionMode.BYPASS,
                    ]:
                        self.mode = result
                        # 同步设置 agent_loop 的模式
                        if hasattr(self, 'agent_loop'):
                            self.agent_loop.mode = result
                        console.print(f"\n[cyan]权限模式切换为: {result}[/cyan]\n")
                        continue

                    continue

                # 意图直达：高频本地操作绕过 LLM 直接执行
                direct_result = self._direct_intent.handle(user_input)
                if direct_result is not None:
                    console.print(f"\n🤖 {direct_result}")
                    self.conversation.append(HumanMessage(content=user_input))
                    self.conversation.append(AIMessage(content=direct_result))
                    continue

                self._run_agent(user_input)

            except KeyboardInterrupt:
                import time
                now = time.monotonic()
                if self._ctrl_c_count > 0 and (now - self._ctrl_c_last) < self._ctrl_c_window:
                    self._cleanup_mcp()
                    console.print("\n[dim]再见！[/dim]")
                    sys.exit(0)
                self._ctrl_c_count += 1
                self._ctrl_c_last = now
                console.print("\n[yellow]再按一次 Ctrl+C 退出，或输入 /exit 退出[/yellow]")
            except EOFError:
                self._cleanup_mcp()
                console.print("\n[dim]再见！[/dim]")
                sys.exit(0)
            except SystemExit:
                self._cleanup_mcp()
                console.print("\n[dim]再见！[/dim]")
                sys.exit(0)
            except Exception as e:
                logger.error(f"REPL 错误: {e}", exc_info=True)
                console.print(f"\n[red]❌ 错误: {e}[/red]\n")

    def _build_user_message(self, user_input: str):
        """把用户输入解析为可能包含图片的 HumanMessage。"""
        refs = parse_image_refs(user_input)
        if not refs:
            return HumanMessage(content=user_input)

        image_parts: list[ImageBase64Part] = []
        clipboard_refs = [r for r in refs if r.is_clipboard]
        path_refs = [r for r in refs if not r.is_clipboard]

        # 优先使用热键预捕获的剪贴板图片，避免提交时剪贴板内容已变
        if clipboard_refs:
            pending = self._pending_clipboard_parts
            if pending:
                for _ in clipboard_refs:
                    if pending:
                        image_parts.append(pending.pop(0))
                if len(clipboard_refs) > len(image_parts):
                    console.print(
                        f"[yellow]警告：@{clipboard_refs[0].source} 引用数量超过预捕获图片数，"
                        f"超出部分将实时重新捕获。[/yellow]"
                    )
            # 剩余未匹配的剪贴板引用仍实时捕获
            remaining = len(clipboard_refs) - len(image_parts)
            if remaining > 0:
                image_parts.extend(load_image_parts(clipboard_refs[len(image_parts):]))

        if path_refs:
            image_parts.extend(load_image_parts(path_refs))

        plain_text = replace_image_refs(user_input)
        for part in image_parts:
            mime = part.mime_type
            size = len(part.data) * 3 // 4  # 粗略字节数
            console.print(f"[dim][已附加图片: {mime}, base64≈{size} bytes][/dim]")
        return build_multimodal_message(plain_text, image_parts)

    def _run_agent(self, user_input: str):
        """执行 Agent 循环并渲染输出。"""
        # 手动 /team 优先
        if self.team_mode:
            self._run_multi_agent(user_input)
            return

        # 自动判断复杂度，复杂任务走 Team 模式。
        # 手动 /plan 是显式执行意图，优先级高于自动 Team 检测。
        if not self.plan_mode and self._should_auto_team(user_input):
            console.print("[dim]检测到复杂任务，自动切换到 Multi-Agent 模式[/dim]")
            self._run_multi_agent(user_input)
            return

        # 首次危险操作提示
        if not self.hitl_handler.is_enabled() and not self._has_shown_hitl_hint:
            from core.hitl_policy import requires_approval
            # 检查输入是否可能触发危险工具（启发式匹配）
            dangerous_keywords = ["删除", "写入", "执行", "运行", "创建", "修改",
                                  "delete", "write", "execute", "run", "create", "modify",
                                  "rm ", "sh ", "bash"]
            if any(kw in user_input.lower() for kw in dangerous_keywords):
                console.print()
                console.print("💡 [yellow]提示：检测到可能涉及危险操作[/yellow]")
                console.print("[dim]输入 [bold]/hitl on[/bold] 启用人工审批，在执行危险操作前请求确认[/dim]")
                console.print()
                self._has_shown_hitl_hint = True

        # 首次代码问题提示
        if not self._has_shown_rag_hint and self._is_code_related_question(user_input):
            if not self.rag_pipeline:
                console.print()
                console.print("💡 [yellow]提示：检测到代码库问题，但 RAG 未启用[/yellow]")
                console.print("[dim]请在 config.yaml 中设置 rag.enabled: true 并使用 /index 索引代码库[/dim]")
                console.print()
            elif not self._is_rag_indexed():
                console.print()
                console.print("💡 [yellow]提示：检测到代码库问题，但代码库未索引[/yellow]")
                console.print("[dim]使用 /index 命令索引代码库，Agent 可以自动检索相关代码[/dim]")
                console.print()
            self._has_shown_rag_hint = True  # 只提示一次

        # 将用户输入加入对话历史（支持图片）
        self.conversation.append(self._build_user_message(user_input))

        self._compact_conversation_history()

        self.stream_renderer.reset(expanded=self.stream_details_expanded)
        self.stream_renderer.start()

        try:
            # 使用对话历史调用 Agent
            final_ai_message = None
            for event in self.agent_loop.stream_with_history(self.conversation):
                for node_name, state in event.items():
                    if state is None or not isinstance(state, dict):
                        continue

                    if node_name == "plan" and self.stream_details_expanded:
                        plan_dict = state.get("plan")
                        if plan_dict:
                            plan = Plan.model_validate(plan_dict)
                            print_plan_created(plan)

                    elif node_name == "execute_plan" and self.stream_details_expanded:
                        plan_dict = state.get("plan")
                        if plan_dict:
                            plan = Plan.model_validate(plan_dict)
                            print_plan_progress(plan)
                            print_plan_result(plan)

                    messages = state.get("messages", [])
                    if messages:
                        for msg in messages:
                            if isinstance(msg, AIMessage) and not msg.tool_calls:
                                final_ai_message = msg

            if final_ai_message:
                # 检查是否为空响应
                content = getattr(final_ai_message, "content", "")
                has_reasoning = bool(
                    getattr(final_ai_message, "additional_kwargs", {}).get("reasoning_content")
                )
                if (content and content.strip()) or has_reasoning:
                    if content and content.strip() and not self.stream_renderer.has_streamed_answer:
                        console.print(f"\n🤖 {content}")
                    self.conversation.append(final_ai_message)
                else:
                    # 空响应：不加入对话历史，避免污染上下文
                    logger.warning("检测到空响应，跳过加入对话历史")
                    console.print("\n[dim]⚠ 模型返回了空响应（可能是 thinking-only），请重试或换个方式提问[/dim]")

        except Exception as e:
            logger.error(f"Agent 执行错误: {e}", exc_info=True)
            console.print(f"\n[red]❌ 执行错误: {e}[/red]\n")
        finally:
            self.stream_renderer.finish()
            # 保存会话状态
            try:
                self.session_manager.save(
                    self.session_manager.create(),
                    {"message_count": len(self.conversation)},
                )
            except Exception:
                pass  # 会话保存失败不影响主流程

    def _compact_conversation_history(self):
        """对话历史过长时先压缩，再用滑动窗口兜底裁剪。

        固定消息数上限只是兜底保护。优先通过 ContextCompactor 把早期历史
        摘要成一条 SystemMessage，并在压缩过程中沉淀关键事实，避免旧消息在
        token 未超阈值时被直接丢弃。
        """
        if len(self.conversation) <= MAX_CONVERSATION_MESSAGES:
            return

        original_count = len(self.conversation)
        compactor = getattr(getattr(self, "agent_loop", None), "compactor", None)
        if compactor:
            try:
                compacted = compactor.compact(self.conversation)
                if compacted and len(compacted) < original_count:
                    self.conversation = compacted
                    logger.info(
                        "对话历史过长，已先压缩: %d 条 → %d 条",
                        original_count,
                        len(compacted),
                    )
                    console.print(
                        f"[dim]对话历史过长，已先压缩为 {len(compacted)} 条消息[/dim]"
                    )
            except Exception as e:
                logger.warning("对话历史压缩失败，回退到滑动窗口裁剪: %s", e)

        if len(self.conversation) > MAX_CONVERSATION_MESSAGES:
            kept = self.conversation[-MAX_CONVERSATION_MESSAGES:]
            dropped = len(self.conversation) - len(kept)
            self.conversation = kept
            logger.info("对话历史过长，压缩后仍截断 %d 条旧消息", dropped)
            console.print(f"[dim]对话历史过长，已裁剪 {dropped} 条旧消息[/dim]")

    def _run_multi_agent(self, user_input: str):
        """以 Multi-Agent 模式执行任务，流式输出每个 Agent 的执行进度。"""
        console.print("\n[cyan]👥 Multi-Agent 协作启动...[/cyan]\n")
        self.stream_renderer.reset(expanded=self.stream_details_expanded)
        self.stream_renderer.start()

        try:
            from agents.orchestrator import MultiAgentOrchestrator

            orchestrator = MultiAgentOrchestrator(
                llm=self.agent_loop.llm,
                tool_registry=self.agent_loop.tool_registry,
                permission_policy=self.agent_loop.permission_policy,
                hook_manager=self.agent_loop.hook_manager,
                memory=self.agent_loop.memory,
            )

            latest_step_results = {}
            latest_messages = []

            for event in orchestrator.stream(user_input):
                for node_name, state in event.items():
                    if state is None or not isinstance(state, dict):
                        continue
                    if state.get("step_results"):
                        latest_step_results = state["step_results"]
                    if state.get("messages"):
                        latest_messages = state["messages"]

                    # 输出每个 Agent 的执行进度
                    if node_name == "supervisor":
                        next_agent = state.get("next", "")
                        if next_agent and next_agent != "__end__":
                            console.print(f"[dim]→ 路由到 {next_agent}[/dim]")
                    elif node_name in ("planner", "worker-1", "worker-2"):
                        step_results = state.get("step_results", {})
                        if node_name == "planner" and state.get("current_task"):
                            console.print(f"\n[cyan]planner[/cyan]:\n{state['current_task']}")
                        if node_name in step_results:
                            preview = step_results[node_name][:200] + "..." if len(step_results[node_name]) > 200 else step_results[node_name]
                            console.print(f"\n[cyan]{node_name}[/cyan]: {preview}")
                    elif node_name == "reviewer":
                        review_status = state.get("review_status")
                        if review_status:
                            if review_status == "approved":
                                console.print("[green]审查通过[/green]")
                            elif review_status == "rejected":
                                console.print("[yellow]审查未通过，重新执行[/yellow]")
                            elif review_status == "max_retries_exceeded":
                                console.print("[yellow]超过最大重试次数，保留当前结果[/yellow]")

            step_results = latest_step_results
            messages = latest_messages

            if step_results:
                console.print("\n[bold]📋 执行结果汇总：[/bold]")
                for agent_name, agent_result in step_results.items():
                    console.print(f"\n[cyan]{agent_name}[/cyan]:\n{agent_result}")

            if messages:
                last_msg = messages[-1]
                if hasattr(last_msg, 'content') and last_msg.content:
                    console.print(f"\n🤖 {last_msg.content}")

            console.print("\n[green]✅ Multi-Agent 协作完成[/green]\n")

        except Exception as e:
            logger.error(f"Multi-Agent 执行错误: {e}", exc_info=True)
            console.print(f"\n[red]❌ Multi-Agent 执行错误: {e}[/red]\n")
        finally:
            self.stream_renderer.finish()

    def _is_code_related_question(self, user_input: str) -> bool:
        """判断是否为代码库相关问题"""
        code_keywords = [
            "这个类", "这个函数", "这个方法", "哪里", "怎么", "实现", "代码",
            "类", "函数", "方法", "文件", "模块", "组件", "服务",
            "什么", "干什么", "作用", "功能", "逻辑", "原理",
            "查找", "搜索", "检索", "找到", "定位", "查看",
            "class", "function", "method", "file", "module", "component",
            "where", "how", "implement", "code", "find", "search", "locate"
        ]
        input_lower = user_input.lower()
        return any(keyword in input_lower for keyword in code_keywords)

    def _is_rag_indexed(self) -> bool:
        """检查 RAG 是否已索引"""
        import os
        return os.path.exists(".weavemind/chroma") and os.path.exists(".weavemind/rag")
