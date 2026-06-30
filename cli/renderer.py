from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree
from langchain_core.messages import AIMessage, ToolMessage
import json
import re
import threading
import time

console = Console()


def print_tool_use(tool_name: str, args: dict):
    """Display tool usage with visual feedback."""
    tool_panel = Panel(
        f"[bold cyan]{tool_name}[/bold cyan]\n[dim]{json.dumps(args, ensure_ascii=False, indent=2)}[/dim]",
        title="🔧 Tool Call",
        border_style="cyan",
        padding=(1, 2)
    )
    console.print(tool_panel)


def print_tool_result(tool_name: str, result: str):
    """Display tool result."""
    # 只显示前 200 字符的摘要
    preview = str(result)[:200]
    if len(str(result)) > 200:
        preview += "..."
    
    result_panel = Panel(
        f"[dim]{preview}[/dim]",
        title=f"✓ {tool_name} Result",
        border_style="green",
        padding=(1, 2)
    )
    console.print(result_panel)


def print_permission_request(tool_name: str) -> bool:
    """Request user permission for tool usage."""
    response = console.input(f"\n[bold yellow]🔐 Allow '{tool_name}' to execute?[/bold yellow] (y/n) ").strip().lower()
    return response == "y"


def _extract_text_content(content):
    """Extract text from content that may be a string or list (MiMo format)."""
    if isinstance(content, str):
        return content
    elif isinstance(content, list):
        # MiMo returns list of dicts with 'text' and 'type' fields
        texts = []
        for item in content:
            if isinstance(item, dict):
                if item.get('type') == 'text' and 'text' in item:
                    texts.append(item['text'])
            elif isinstance(item, str):
                texts.append(item)
        return "\n".join(texts) if texts else str(content)
    else:
        return str(content)


def _split_summary_and_result(text: str) -> tuple[str, str]:
    """Heuristically split final response into summary and result sections."""
    if not text:
        return "", ""

    # 优先按“结果”关键词切分（支持前缀 emoji、项目符号）
    marker_patterns = [
        r"\n\s*(?:[\-\*\d\.]+\s*)?(?:[\U0001F300-\U0001FAFF]\s*)?运行结果\s*[:：]",
        r"\n\s*(?:[\-\*\d\.]+\s*)?(?:[\U0001F300-\U0001FAFF]\s*)?结果\s*[:：]",
        r"\n\s*(?:[\-\*\d\.]+\s*)?(?:[\U0001F300-\U0001FAFF]\s*)?输出\s*[:：]",
    ]
    for pattern in marker_patterns:
        m = re.search(pattern, text)
        if m:
            idx = m.start()
            summary = text[:idx].strip()
            result = text[idx:].strip()
            return summary, result

    # 其次按首个代码块切分
    code_block = re.search(r"```[\s\S]*?```", text)
    if code_block:
        start = code_block.start()
        summary = text[:start].strip()
        result = code_block.group(0).strip()
        return summary, result

    return text.strip(), ""


def _clean_result_text(result: str) -> str:
    """Normalize result text for cleaner terminal rendering."""
    if not result:
        return ""

    cleaned = re.sub(
        r"^\s*(?:[\-\*\d\.]+\s*)?(?:[\U0001F300-\U0001FAFF]\s*)?(?:运行结果|结果|输出)\s*[:：]\s*",
        "",
        result,
        flags=re.IGNORECASE,
    ).strip()

    # 如果是代码块，仅展示代码块内部内容
    block = re.match(r"^```(?:\w+)?\n([\s\S]*?)\n```$", cleaned)
    if block:
        return block.group(1).strip()

    return cleaned


def stream_response(event_iter):
    """Render response in workflow style: thinking -> tools -> token usage -> summary -> result."""
    accumulated = ""
    tool_count = 0
    all_messages = []
    processed_msg_ids = set()
    latest_token_state = {}
    token_steps = []
    last_model_call_count = 0
    prev_msg_count = None
    tool_call_name_by_id = {}

    console.print("🤔 思考中...")

    for state in event_iter:
        msgs = state.get("messages", [])
        if not msgs:
            continue

        # 首帧通常是历史上下文，不属于本轮新增消息
        if prev_msg_count is None:
            prev_msg_count = len(msgs)
            latest_token_state = state
            continue

        latest_token_state = state
        current_model_call_count = state.get("model_call_count", 0)
        if current_model_call_count > last_model_call_count:
            last_model_call_count = current_model_call_count
            token_steps.append(
                (
                    current_model_call_count,
                    state.get("last_input_tokens", 0),
                    state.get("last_output_tokens", 0),
                    state.get("last_total_tokens", 0),
                )
            )
        new_msgs = msgs[prev_msg_count:]
        prev_msg_count = len(msgs)
        if not new_msgs:
            continue

        all_messages.extend(new_msgs)

        for message in new_msgs:
            msg_id = id(message)
            if msg_id in processed_msg_ids:
                continue
            processed_msg_ids.add(msg_id)

            if isinstance(message, AIMessage) and message.tool_calls:
                for tool_call in message.tool_calls:
                    tool_name = tool_call.get("name", "Unknown")
                    tool_args = tool_call.get("args", {})
                    tool_call_id = tool_call.get("id")
                    if tool_call_id:
                        tool_call_name_by_id[tool_call_id] = tool_name
                    args_str = json.dumps(tool_args, ensure_ascii=False)
                    console.print(f"🔧 执行工具: {tool_name}")
                    console.print(f"参数: {args_str}")

            if isinstance(message, ToolMessage):
                tool_count += 1
                tool_call_info = tool_call_name_by_id.get(message.tool_call_id, "Unknown")

                result_preview = str(message.content)[:160]
                if len(str(message.content)) > 160:
                    result_preview += "..."
                console.print(f"结果: [{tool_call_info}] {result_preview}")

            if isinstance(message, AIMessage) and not message.tool_calls:
                accumulated = _extract_text_content(message.content)

    input_tokens = latest_token_state.get("input_tokens", 0)
    output_tokens = latest_token_state.get("output_tokens", 0)
    total_tokens = latest_token_state.get("total_tokens", input_tokens + output_tokens)
    if token_steps:
        step_lines = []
        for step_no, step_in, step_out, step_total in token_steps:
            step_lines.append(
                f"第{step_no}次模型调用: 输入={step_in}, 输出={step_out}, 总计={step_total}"
            )
        console.print("📊 Token使用(按模型调用):")
        for line in step_lines:
            console.print(f"  - {line}")
    console.print(f"📊 Token使用(本任务累计): 输入={input_tokens}, 输出={output_tokens}, 总计={total_tokens}")

    summary, result = _split_summary_and_result(accumulated)
    clean_result = _clean_result_text(result)

    if not summary and clean_result:
        summary = "任务已完成，关键结果如下。"

    if not summary and not clean_result:
        # 检查是否有工具执行记录
        if tool_count > 0:
            summary = f"任务已执行 {tool_count} 个工具，但模型未生成最终回复。这可能是模型的 thinking-only 响应。"
        else:
            summary = "任务已结束，但未产出最终文本总结（可能达到迭代上限或中途停止）。"

    if summary:
        console.print("🤖 Agent 总结:")
        console.print(summary)
    if clean_result:
        console.print("📌 结果:")
        console.print(Panel(clean_result, border_style="cyan", padding=(0, 1)))

    return accumulated


# ── Plan-and-Execute 渲染 ──────────────────────────────────────────

def print_plan_created(plan):
    """渲染计划生成结果，显示任务列表和依赖关系。"""
    console.print()
    console.print(Panel(
        f"[bold]{plan.goal}[/bold]",
        title=f"📋 计划生成: {plan.id}",
        border_style="blue",
        padding=(1, 2),
    ))

    # 任务表格
    table = Table(title="任务列表", show_header=True, header_style="bold cyan")
    table.add_column("ID", style="cyan", width=10)
    table.add_column("描述", style="white")
    table.add_column("工具", style="yellow", width=12)
    table.add_column("依赖", style="dim", width=20)

    for task in plan.tasks:
        deps = ", ".join(task.dependencies) if task.dependencies else "-"
        tool = task.tool_name or "-"
        table.add_row(task.id, task.description, tool, deps)

    console.print(table)


def print_plan_progress(plan):
    """渲染计划执行进度。"""
    # DAG 树状图
    tree = Tree("🌳 执行进度", style="bold cyan")

    # 按依赖关系构建树
    roots = [t for t in plan.tasks if not t.dependencies]
    task_map = {t.id: t for t in plan.tasks}

    def add_task_node(parent, task):
        status_icons = {
            "pending": "⏳",
            "running": "🔄",
            "completed": "✅",
            "failed": "❌",
            "skipped": "⏭️",
        }
        icon = status_icons.get(task.status.value, "❓")

        # 截断描述
        desc = task.description[:60] + ("..." if len(task.description) > 60 else "")
        label = f"{icon} [{task.id}] {desc}"

        if task.status.value == "failed" and task.error:
            label += f" [red]({task.error[:40]})[/red]"
        elif task.status.value == "completed" and task.result:
            preview = task.result[:80] + ("..." if len(task.result) > 80 else "")
            label += f" [dim]→ {preview}[/dim]"

        node = parent.add(label)
        # 找到依赖此任务的子任务
        children = [t for t in plan.tasks if task.id in t.dependencies]
        for child in children:
            add_task_node(node, child)

    for root in roots:
        add_task_node(tree, root)

    console.print(tree)


def print_plan_result(plan):
    """渲染计划最终结果。"""
    status_colors = {
        "completed": "green",
        "failed": "red",
        "cancelled": "yellow",
    }
    color = status_colors.get(plan.status.value, "white")

    # 统计
    total = len(plan.tasks)
    completed = sum(1 for t in plan.tasks if t.status.value == "completed")
    failed = sum(1 for t in plan.tasks if t.status.value == "failed")
    skipped = sum(1 for t in plan.tasks if t.status.value == "skipped")

    summary = (
        f"[bold {color}]计划 {plan.status.value}[/bold {color}]\n"
        f"总计 {total} 个任务: "
        f"✅ {completed} 完成 | ❌ {failed} 失败 | ⏭️ {skipped} 跳过"
    )

    console.print()
    console.print(Panel(summary, title="📊 执行结果", border_style=color, padding=(1, 2)))

    # 失败任务详情
    failed_tasks = [t for t in plan.tasks if t.status.value == "failed"]
    if failed_tasks:
        console.print("[bold red]失败任务:[/bold red]")
        for task in failed_tasks:
            console.print(f"  ❌ [{task.id}] {task.description}")
            console.print(f"     错误: {task.error}")


def print_thinking_indicator():
    """显示思考中的指示器。"""
    console.print("\n[dim]🤔 思考中...[/dim]")


def print_stream_start():
    """标记流式开始。"""
    console.print("\n[cyan]▌[/cyan]", end="", flush=True)


def print_stream_token(token: str = ""):
    """打印流式令牌。"""
    if token:
        console.print(token, end="", flush=True)


def print_stream_end():
    """标记流式结束。"""
    console.print()


class InteractionStreamRenderer:
    """流式交互渲染器：展示思考状态、工具进度和 token 统计。

    策略：
    - 所有轮次文本先灰色流式输出（推理过程可见）
    - on_llm_end 时判断：如果本轮没有 tool_calls 且不是第1轮，说明是最终回答
    - 最终回答在 on_llm_end 时用白色重新输出（覆盖灰色）
    - 第1轮（无前序工具调用）的文本直接白色输出
    """

    def __init__(self, rich_console: Console = None, expanded: bool = False):
        self.console = rich_console or console
        self._lock = threading.Lock()
        self._expanded = expanded
        self.reset(expanded=expanded)

    @property
    def has_streamed_answer(self) -> bool:
        return self._has_streamed_final_answer

    def reset(self, expanded: bool = None):
        if expanded is not None:
            self._expanded = expanded
        self._has_streamed_final_answer = False
        self._started_at = time.perf_counter()
        self._input_tokens = 0
        self._output_tokens = 0
        self._total_tokens = 0
        self._call_index = 0
        self._has_content_in_current_call = False
        self._thinking_in_current_call = False
        # 累积当前轮次的文本，用于最终回答时白色重输出
        self._current_call_text = ""
        # 上一轮是否有 tool_calls
        self._prev_call_had_tool_calls = False

    def set_expanded(self, expanded: bool):
        self._expanded = expanded

    def start(self):
        with self._lock:
            self.console.print(
                "\n• [bold]思考中...[/bold] "
                f"[dim](Ctrl+O {'展开' if not self._expanded else '收起'}详情)[/dim]"
            )

    def on_llm_start(self, data: dict):
        call_index = data.get("call_index", 0)
        self._call_index = call_index
        self._has_content_in_current_call = False
        self._thinking_in_current_call = False
        self._current_call_text = ""
        if call_index >= 1:
            with self._lock:
                self.console.print(f"\n• 第 {call_index} 轮推理...", end="")

    def on_llm_delta(self, data: dict):
        delta = data.get("delta", "")
        if not delta:
            return
        self._has_content_in_current_call = True
        self._current_call_text += delta

        with self._lock:
            if not self._thinking_in_current_call:
                self._thinking_in_current_call = True
                self.console.print()  # 换行，和"第 N 轮推理..."分开

            # 第1轮（无前序工具调用）：直接白色流式输出
            if self._call_index == 1 and not self._prev_call_had_tool_calls:
                if not self._has_streamed_final_answer:
                    self._has_streamed_final_answer = True
                    self.console.print("🤖 ", end="", soft_wrap=True)
                self.console.print(delta, end="", soft_wrap=True)
            else:
                # 中间轮次：灰色流式输出
                self.console.print(f"[dim]{delta}[/dim]", end="", soft_wrap=True)

    def on_llm_end(self, data: dict):
        self._input_tokens += int(data.get("input_tokens", 0))
        self._output_tokens += int(data.get("output_tokens", 0))
        self._total_tokens += int(data.get("total_tokens", 0))

        has_tool_calls = data.get("has_tool_calls", False)

        with self._lock:
            # 推理文本结束时换行
            if self._thinking_in_current_call:
                self.console.print()

            # 本轮没有 tool_calls 且之前有工具调用 → 这是最终回答
            # 用白色重新输出（覆盖之前的灰色）
            if (not has_tool_calls
                    and self._prev_call_had_tool_calls
                    and self._current_call_text.strip()):
                self._has_streamed_final_answer = True
                self.console.print(f"\n🤖 {self._current_call_text}")

            # 如果本轮没有内容输出
            if not self._has_content_in_current_call and self._call_index >= 1:
                if self._expanded:
                    self.console.print(" [dim](空响应)[/dim]")

        self._prev_call_had_tool_calls = has_tool_calls

    def on_tool_start(self, data: dict):
        tool = data.get("tool", "Unknown")
        args = data.get("args") or {}
        agent = data.get("agent")
        title, detail = self._tool_text(tool, args)
        with self._lock:
            # 默认只显示工具名称简略行；展开时显示参数
            if self._expanded:
                prefix = f"[{agent}] " if agent else ""
                self.console.print(f"\n• {prefix}{title}")
                if detail:
                    self.console.print(f"  └ {detail}", style="dim")
            else:
                # 简略模式：只显示工具简称（不换行）
                emoji = self._tool_emoji(tool)
                prefix = f"{agent}:" if agent else ""
                self.console.print(f"  {prefix}{emoji} {tool}", end=" ")

    def on_tool_end(self, data: dict):
        if not self._expanded:
            # 简略模式：显示完成/失败标记
            status = "✓" if not data.get("error") else "✗"
            self.console.print(f"[{status}]", end=" ")
            return
        
        with self._lock:
            result = str(data.get("result", "")).replace("\n", " ").strip()
            if len(result) > 140:
                result = f"{result[:140]}..."
            status = "失败" if data.get("error") else "完成"
            self.console.print(f"  └ {status}: {result or '-'}", style="dim")

    def on_plan_start(self, data: dict):
        if not self._expanded:
            return
        with self._lock:
            self.console.print("\n• 正在生成执行计划...")

    def on_plan_created(self, data: dict):
        if not self._expanded:
            return
        task_count = data.get("task_count", 0)
        with self._lock:
            self.console.print(f"• 计划已生成，共 {task_count} 个任务")

    def on_plan_execute_start(self, data: dict):
        if not self._expanded:
            return
        with self._lock:
            self.console.print("• 开始执行计划任务...")

    def on_plan_execute_end(self, data: dict):
        if not self._expanded:
            return
        status = data.get("status", "unknown")
        with self._lock:
            self.console.print(f"• 计划执行状态: {status}")

    def finish(self):
        with self._lock:
            if self._has_streamed_final_answer:
                self.console.print()
            elapsed = time.perf_counter() - self._started_at
            if self._total_tokens > 0:
                self.console.print(
                    f"[dim]({elapsed:.1f}s · ↑ {self._output_tokens} tokens · Σ {self._total_tokens} tokens)[/dim]"
                )
            else:
                self.console.print(f"[dim]({elapsed:.1f}s)[/dim]")
            # 如果简略模式且显示了工具执行，补一个换行
            if not self._expanded and self._has_streamed_final_answer:
                self.console.print()  # 让工具行后面的输出更清晰

    def reset_between_iterations(self):
        """重置渲染缓冲区，用于工具执行前 flush 输出，避免审批面板与流式输出混淆。"""
        with self._lock:
            self.console.print()

    def _tool_text(self, tool_name: str, args: dict) -> tuple[str, str]:
        if tool_name == "Read":
            path = args.get("path") or args.get("file_path") or "-"
            return "正在读取文件...", str(path)
        if tool_name == "Write":
            path = args.get("path") or "-"
            return "正在写入文件...", str(path)
        if tool_name == "Edit":
            path = args.get("path") or "-"
            return "正在编辑文件...", str(path)
        if tool_name == "Glob":
            pattern = args.get("pattern") or "*"
            return "正在搜索文件...", f"pattern={pattern}"
        if tool_name == "Grep":
            pattern = args.get("pattern") or ""
            return "正在检索内容...", f"pattern={pattern}"
        if tool_name == "Bash":
            command = args.get("command") or ""
            return "正在执行命令...", command
        return f"正在执行工具 {tool_name}...", json.dumps(args, ensure_ascii=False)

    @staticmethod
    def _tool_emoji(tool_name: str) -> str:
        """获取工具对应的 emoji。"""
        emojis = {
            "Read": "📖",
            "Write": "✍️",
            "Edit": "✏️",
            "Bash": "🖥️",
            "Glob": "📁",
            "Grep": "🔍",
            "WebFetch": "🌐",
            "WebSearch": "🔎",
            "AskUser": "❓",
        }
        return emojis.get(tool_name, "🔧")
