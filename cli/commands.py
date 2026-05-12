"""斜杠命令处理。"""

import logging
import os

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import settings

console = Console()
logger = logging.getLogger(__name__)


def handle_command(cmd: str, agent_loop, session_manager, rag_pipeline=None) -> bool:
    """Returns True if command was handled, str for mode change, 'plan_mode' for /plan toggle."""
    parts = cmd.strip().split()
    name = parts[0].lower()

    if name == "/help":
        help_table = Table(title="📖 可用命令", show_header=True, header_style="bold cyan")
        help_table.add_column("命令", style="cyan")
        help_table.add_column("说明", style="dim")
        help_table.add_row("/help", "显示此帮助信息")
        help_table.add_row("/memory", "查看记忆系统状态（核心记忆 + 长期记忆）")
        help_table.add_row("/save <事实>", "手动保存事实到长期记忆")
        help_table.add_row("/index [目录]", "索引代码库（建立 RAG 检索数据库）")
        help_table.add_row("/search <关键词>", "手动检索代码库中的相关代码")
        help_table.add_row("/sessions", "列出所有保存的会话")
        help_table.add_row("/mode [MODE]", "切换权限模式 (default | acceptEdits | bypassPermissions)")
        help_table.add_row("/hitl [on|off|status]", "人工审批模式（默认启用，/hitl off 关闭）")
        help_table.add_row("/plan", "切换 Plan-Execute 模式（复杂任务先规划后执行）")
        help_table.add_row("/team", "切换 Multi-Agent 模式（多角色分工+审查验收）")
        help_table.add_row("/clear", "清空屏幕")
        help_table.add_row("/exit 或 /quit", "退出 Agent")

        console.print()
        console.print(help_table)
        console.print()

    elif name == "/memory":
        _show_memory_status()

    elif name == "/save":
        _save_fact(parts)

    elif name == "/sessions":
        sessions = session_manager.list()
        if not sessions:
            console.print("\n[dim]无保存的会话[/dim]\n")
        else:
            session_table = Table(title="💾 保存的会话", show_header=False)
            for i, sid in enumerate(sessions, 1):
                session_table.add_row(f"{i}.", sid, style="dim")

            console.print()
            console.print(session_table)
            console.print()

    elif name == "/mode":
        if len(parts) > 1:
            new_mode = parts[1]
            valid_modes = ["default", "acceptEdits", "bypassPermissions"]
            if new_mode in valid_modes:
                return new_mode
            else:
                console.print(f"\n[red]❌ 无效模式: {new_mode}[/red]")
                console.print(f"[dim]可用模式: {', '.join(valid_modes)}[/dim]\n")
        else:
            console.print("\n[yellow]用法: /mode [mode][/yellow]")
            console.print("[dim]可用模式: default | acceptEdits | bypassPermissions[/dim]\n")
            console.print("[dim]default 模式：自动判断危险操作并询问用户确认[/dim]\n")

    elif name == "/plan":
        return "plan_mode"

    elif name == "/team":
        return "team_mode"

    elif name == "/index":
        _index_workspace(parts, rag_pipeline)

    elif name == "/search":
        _search_code(parts, rag_pipeline)

    elif name == "/hitl":
        _handle_hitl(parts, agent_loop)

    elif name in ("/exit", "/quit"):
        raise SystemExit(0)

    elif name == "/clear":
        # 清除屏幕，同时清除 HITL 全部放行记录
        if hasattr(agent_loop, 'tool_registry') and hasattr(agent_loop.tool_registry, 'hitl_handler'):
            agent_loop.tool_registry.hitl_handler.clear_approved_all()
        console.clear()
        return "clear"

    else:
        console.print(f"\n[red]❌ 未知命令: {name}[/red]")
        console.print("[dim]输入 /help 查看可用命令[/dim]\n")

    return True


def _show_memory_status():
    """显示记忆系统完整状态。"""
    from core.memory import MemoryManager

    mem = MemoryManager()

    # 核心记忆
    core_table = Table(title="🧠 核心记忆块", show_header=True, header_style="bold cyan")
    core_table.add_column("块", style="cyan", width=10)
    core_table.add_column("内容", style="dim")
    for block, content in mem.core.get_all().items():
        display = content[:100] + "..." if len(content) > 100 else content
        core_table.add_row(block, display or "(空)")

    # 长期记忆
    facts = mem.long_term.get_all()
    facts_table = Table(
        title=f"💾 长期记忆 ({len(facts)} 条)",
        show_header=True,
        header_style="bold cyan",
    )
    facts_table.add_column("#", style="dim", width=4)
    facts_table.add_column("内容", style="dim")
    for i, entry in enumerate(facts[:10], 1):
        facts_table.add_row(str(i), entry.content[:80])
    if len(facts) > 10:
        facts_table.add_row("...", f"还有 {len(facts) - 10} 条")

    console.print()
    console.print(core_table)
    if facts:
        console.print(facts_table)
    else:
        console.print("\n[dim]💾 长期记忆为空（使用 /save 或 MemoryAdd 工具保存事实）[/dim]")
    console.print("\n[dim]💡 核心记忆可通过 CoreMemoryEdit 工具编辑（Agent 自动调用），始终在系统提示中可见[/dim]")
    console.print()


def _save_fact(parts: list):
    """手动保存事实到长期记忆。"""
    if len(parts) < 2:
        console.print("\n[yellow]用法: /save <事实内容>[/yellow]")
        console.print("[dim]示例: /save 用户偏好使用 JDK 17[/dim]")
        console.print("[dim]示例: /save 项目使用 Maven 构建[/dim]\n")
        return

    content = " ".join(parts[1:])
    from core.memory import MemoryManager

    mem = MemoryManager()
    saved = mem.store_fact(content)
    if saved:
        console.print(f"\n[green]✅ 已保存到长期记忆: {content}[/green]\n")
    else:
        console.print(f"\n[dim]该事实已存在，跳过重复保存[/dim]\n")


def _index_workspace(parts: list, rag_pipeline=None):
    """索引工作区代码文件。

    用法:
        /index                          索引当前目录（source 自动取目录名）
        /index /path/to/project         索引指定目录
        /index /path/to/project myname  索引指定目录并命名为 myname
    """
    if not settings.get("rag.enabled", False) or not rag_pipeline:
        console.print("\n[red]❌ RAG 未启用。请在 config.yaml 中设置 rag.enabled: true[/red]\n")
        return

    directory = parts[1] if len(parts) > 1 else "."
    source = parts[2] if len(parts) > 2 else None
    console.print(f"\n[cyan]🔍 正在索引 {directory} ...[/cyan]")

    try:
        stats = rag_pipeline.index_directory(directory, source=source)
        source_label = source or os.path.basename(os.path.abspath(directory))
        console.print(f"\n[green]✅ 索引完成！[/green] (source: {source_label})")
        console.print(f"  文件数: {stats.total_files}")
        console.print(f"  代码块: {stats.total_chunks}")
        console.print(f"  耗时: {stats.index_time:.1f}s")
        if stats.chunks_by_language:
            lang_str = ", ".join(f"{k}={v}" for k, v in stats.chunks_by_language.items())
            console.print(f"  语言分布: {lang_str}")
        console.print()
    except Exception as e:
        console.print(f"\n[red]❌ 索引失败: {e}[/red]\n")


def _search_code(parts: list, rag_pipeline=None):
    """手动检索代码库。

    用法:
        /search 关键词                   全局搜索
        /search 关键词 --source weavemind  只搜指定源
    """
    if not settings.get("rag.enabled", False) or not rag_pipeline:
        console.print("\n[red]❌ RAG 未启用。请在 config.yaml 中设置 rag.enabled: true[/red]\n")
        return

    if len(parts) < 2:
        console.print("\n[yellow]用法: /search <关键词或自然语言描述> [--source 源名][/yellow]")
        console.print("[dim]示例: /search 用户认证逻辑[/dim]")
        console.print("[dim]示例: /search MemoryManager --source weavemind[/dim]\n")
        return

    # 解析参数：提取 --source
    source_filter = None
    search_parts = []
    i = 1
    while i < len(parts):
        if parts[i] == "--source" and i + 1 < len(parts):
            source_filter = parts[i + 1]
            i += 2
        else:
            search_parts.append(parts[i])
            i += 1

    query = " ".join(search_parts)
    if not query:
        console.print("\n[yellow]请输入搜索关键词[/yellow]\n")
        return

    source_hint = f" (source: {source_filter})" if source_filter else ""
    console.print(f"\n[cyan]🔍 检索: {query}{source_hint} ...[/cyan]")

    try:
        results = rag_pipeline.search(
            query, top_k=5, strategy="hybrid", source_filter=source_filter
        )
        if not results:
            console.print(f"\n[dim]未找到与 '{query}' 相关的代码[/dim]\n")
            return

        console.print(f"\n[green]找到 {len(results)} 个相关代码片段：[/green]\n")
        for i, r in enumerate(results, 1):
            console.print(
                f"  [{i}] {r.chunk.display_name()} "
                f"(score={r.score:.2f}, {r.chunk.chunk_type}, "
                f"L{r.chunk.start_line}-{r.chunk.end_line})"
            )
            # 显示代码片段（截断）
            content = r.chunk.content
            if len(content) > 300:
                content = content[:300] + "..."
            console.print(f"  [dim]{content[:200]}[/dim]")
            console.print()
    except Exception as e:
        console.print(f"\n[red]❌ 检索失败: {e}[/red]\n")


def _handle_hitl(parts: list, agent_loop):
    """处理 /hitl 命令。"""
    # 获取 HITL 处理器
    hitl_handler = None
    if hasattr(agent_loop, 'tool_registry') and hasattr(agent_loop.tool_registry, 'hitl_handler'):
        hitl_handler = agent_loop.tool_registry.hitl_handler

    if not hitl_handler:
        console.print("\n[red]❌ HITL 处理器未初始化[/red]\n")
        return

    subcmd = parts[1] if len(parts) > 1 else ""

    if subcmd == "on":
        hitl_handler.set_enabled(True)
        console.print("\n[green]✅ HITL 已启用，危险操作将在执行前请求确认[/green]\n")
    elif subcmd == "off":
        hitl_handler.set_enabled(False)
        console.print("\n[red]❌ HITL 已禁用[/red]\n")
    elif subcmd == "status":
        status = "启用" if hitl_handler.is_enabled() else "禁用"
        approved_count = hitl_handler.approved_all_count()
        console.print(f"\n[cyan]HITL 当前状态：{status}[/cyan]")
        console.print(f"[dim]已全部放行的工具：{approved_count} 个[/dim]\n")
    else:
        console.print("\n[yellow]用法: /hitl [on|off|status][/yellow]")
        console.print("[dim]  on     启用人工审批[/dim]")
        console.print("[dim]  off    禁用人工审批[/dim]")
        console.print("[dim]  status 查看当前状态[/dim]\n")
