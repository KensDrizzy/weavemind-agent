"""意图直达处理器 — 对高频本地操作绕过 LLM 直接执行，提升响应速度和稳定性。

设计原则：
  1. 只处理确定性操作（列目录、读文件、搜索文件/内容、查看文件信息）
  2. 不替代 LLM 的推理能力，只做"快速通道"
  3. 命中后写入对话历史，保持上下文连续性
  4. 未命中时透明回退到 LLM
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional

from langchain_core.messages import AIMessage, HumanMessage

logger = logging.getLogger(__name__)

# ── 意图分类 ──────────────────────────────────────────────

# 操作动词
_LIST_VERBS = r"(列出|查看|显示|有哪些|都有什么|看看|看下|展示|打印)"
_READ_VERBS = r"(读取|查看|显示|看看|看下|打开|读|展示|打印)"
_FIND_VERBS = r"(查找|搜索|找|检索|搜)"
_INFO_VERBS = r"(多大|多少行|多少文件|多大文件|文件大小|行数|统计)"
_REASONING_TERMS = r"(分析|解释|总结|说明|梳理|评估|比较|设计|原理|为什么|怎么|如何|实现|架构|流程|机制|原因|建议)"
_MULTI_STEP_TERMS = r"(然后|并且|再|同时|顺便|接着|之后|并|以及)"

# 目标描述
_DIR_TARGETS = r"(目录|文件夹|路径)"
_FILE_TARGETS = r"(文件|文档|代码)"
_CONTENT_TARGETS = r"(内容|代码|文本|字符串|关键字)"

# 路径模式
_PATH_PATTERN = r"(?:['\"]?)((?:[./~]|[\w\-]+/)[\w\-./]*)(?:['\"]?)"
_ROOT_PATTERNS = r"(根目录|当前目录|项目目录|weavemind|这个项目|此项目|这里|当前文件夹)"


class DirectIntentHandler:
    """意图直达处理器。

    分类：
      - list_dir:    列出目录内容
      - read_file:   读取文件内容
      - glob_files:  按模式搜索文件
      - grep_content:搜索文件内容
      - file_info:   查看文件信息（大小、行数）
    """

    def __init__(self, tool_registry):
        self._tool_registry = tool_registry

    def handle(self, user_input: str) -> Optional[str]:
        """尝试处理用户输入，返回 AI 回复文本或 None（表示回退 LLM）。"""
        text = user_input.strip()

        # 包含 URL 的输入不走直达，交给 LLM 处理
        if re.search(r'https?://', text):
            return None

        # 复合理解任务交给 LLM：直达只适合短小、确定性的本地操作。
        if self._requires_reasoning(text):
            return None

        # 按优先级尝试各意图
        for handler in [
            self._try_list_dir,
            self._try_read_file,
            self._try_glob_files,
            self._try_grep_content,
            self._try_file_info,
        ]:
            result = handler(text)
            if result is not None:
                return result

        return None

    def _requires_reasoning(self, text: str) -> bool:
        """判断是否应回退给 LLM，而不是用快速工具结果抢答。"""
        has_local_action = re.search(
            rf"({_LIST_VERBS}|{_READ_VERBS}|{_FIND_VERBS}|{_INFO_VERBS})",
            text,
            re.IGNORECASE,
        )
        if not has_local_action:
            return False

        has_reasoning = re.search(_REASONING_TERMS, text, re.IGNORECASE)
        has_multi_step = re.search(_MULTI_STEP_TERMS, text, re.IGNORECASE)

        return bool(has_reasoning and (has_multi_step or len(text) > 18))

    # ── 1. 列出目录 ──────────────────────────────────────

    def _try_list_dir(self, text: str) -> Optional[str]:
        """匹配：列出/查看 + 目录 + 路径"""
        # 必须有列表意图
        if not re.search(_LIST_VERBS, text, re.IGNORECASE):
            return None

        # 必须有目录或文件目标
        has_target = re.search(
            rf"({_DIR_TARGETS}|{_FILE_TARGETS})", text, re.IGNORECASE
        )
        if not has_target:
            return None

        # 提取路径
        target_path = self._extract_path(text)
        if target_path is None:
            return None

        read_tool = self._tool_registry.get("Read")
        if not read_tool:
            return None

        try:
            result = read_tool.invoke({"path": target_path})
            lines = [ln for ln in str(result).splitlines() if ln.strip()]

            # 区分文件和目录
            if os.path.isfile(target_path):
                # 这是文件内容，不是列表
                return None

            lines.sort(key=str.lower)
            count = len(lines)
            rendered = "\n".join(f"  {ln}" for ln in lines)

            dir_label = target_path if target_path != os.getcwd() else "项目根目录"
            return f"{dir_label} 下共有 {count} 个条目：\n{rendered}"
        except Exception as e:
            logger.debug(f"list_dir 直达失败: {e}")
            return None

    # ── 2. 读取文件 ──────────────────────────────────────

    def _try_read_file(self, text: str) -> Optional[str]:
        """匹配：读取/查看 + 文件 + 具体文件路径"""
        # 必须有读取意图
        if not re.search(_READ_VERBS, text, re.IGNORECASE):
            return None

        # 必须有文件目标
        if not re.search(_FILE_TARGETS, text, re.IGNORECASE):
            return None

        # 提取文件路径（需要更精确的匹配）
        path = self._extract_file_path(text)
        if not path or not os.path.isfile(path):
            return None

        # 排除"列出目录"的歧义
        if re.search(_LIST_VERBS, text, re.IGNORECASE) and os.path.isdir(path):
            return None

        read_tool = self._tool_registry.get("Read")
        if not read_tool:
            return None

        try:
            result = read_tool.invoke({"path": path, "limit": 2000})
            content = str(result)

            # 截断过长内容
            if len(content) > 3000:
                content = content[:3000] + "\n... (内容过长，已截断)"

            return f"文件 `{path}` 的内容：\n```\n{content}\n```"
        except Exception as e:
            logger.debug(f"read_file 直达失败: {e}")
            return None

    # ── 3. 搜索文件 ──────────────────────────────────────

    def _try_glob_files(self, text: str) -> Optional[str]:
        """匹配：查找/搜索 + 文件 + 模式/扩展名"""
        # 必须有查找意图
        if not re.search(_FIND_VERBS, text, re.IGNORECASE):
            return None

        # 必须有文件目标
        if not re.search(_FILE_TARGETS, text, re.IGNORECASE):
            return None

        # 提取搜索模式
        pattern = self._extract_glob_pattern(text)
        if not pattern:
            return None

        glob_tool = self._tool_registry.get("Glob")
        if not glob_tool:
            return None

        try:
            result = glob_tool.invoke({"pattern": pattern, "root": "."})
            lines = [ln for ln in str(result).splitlines() if ln.strip()]

            if not lines or lines == ["(no matches)"]:
                return f"未找到匹配 `{pattern}` 的文件。"

            lines.sort()
            count = len(lines)
            # 限制输出数量
            if count > 50:
                lines = lines[:50]
                rendered = "\n".join(f"  {ln}" for ln in lines)
                return f"找到 {count} 个匹配 `{pattern}` 的文件（显示前 50 个）：\n{rendered}"

            rendered = "\n".join(f"  {ln}" for ln in lines)
            return f"找到 {count} 个匹配 `{pattern}` 的文件：\n{rendered}"
        except Exception as e:
            logger.debug(f"glob_files 直达失败: {e}")
            return None

    # ── 4. 搜索内容 ──────────────────────────────────────

    def _try_grep_content(self, text: str) -> Optional[str]:
        """匹配：搜索/查找 + 内容/关键字 + 关键词"""
        # 必须有查找意图
        if not re.search(_FIND_VERBS, text, re.IGNORECASE):
            return None

        # 必须有内容目标
        if not re.search(_CONTENT_TARGETS, text, re.IGNORECASE):
            return None

        # 提取搜索关键词
        keyword = self._extract_grep_keyword(text)
        if not keyword:
            return None

        grep_tool = self._tool_registry.get("Grep")
        if not grep_tool:
            return None

        try:
            result = grep_tool.invoke({"pattern": keyword, "path": "."})
            lines = [ln for ln in str(result).splitlines() if ln.strip()]

            if not lines or lines == ["(no matches)"]:
                return f"未找到包含 `{keyword}` 的内容。"

            count = len(lines)
            if count > 30:
                lines = lines[:30]
                rendered = "\n".join(f"  {ln}" for ln in lines)
                return f"找到 {count} 处包含 `{keyword}` 的内容（显示前 30 条）：\n{rendered}"

            rendered = "\n".join(f"  {ln}" for ln in lines)
            return f"找到 {count} 处包含 `{keyword}` 的内容：\n{rendered}"
        except Exception as e:
            logger.debug(f"grep_content 直达失败: {e}")
            return None

    # ── 5. 文件信息 ──────────────────────────────────────

    def _try_file_info(self, text: str) -> Optional[str]:
        """匹配：多大/多少行/文件大小 + 文件路径"""
        if not re.search(_INFO_VERBS, text, re.IGNORECASE):
            return None

        path = self._extract_file_path(text)
        if not path or not os.path.isfile(path):
            return None

        try:
            stat = os.stat(path)
            size = stat.st_size
            size_str = self._format_size(size)

            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                line_count = sum(1 for _ in f)

            return (
                f"文件 `{path}` 信息：\n"
                f"  大小：{size_str} ({size:,} bytes)\n"
                f"  行数：{line_count:,}"
            )
        except Exception as e:
            logger.debug(f"file_info 直达失败: {e}")
            return None

    # ── 路径提取工具 ──────────────────────────────────────

    def _extract_path(self, text: str) -> Optional[str]:
        """从文本中提取路径。

        优先级：
          1. 显式路径（如 "src/"、"config.yaml"）
          2. 根目录关键词 → 当前工作目录
          3. 模糊匹配 → 当前工作目录
        """
        # 尝试匹配显式路径
        m = re.search(_PATH_PATTERN, text)
        if m:
            raw = m.group(1)
            expanded = os.path.expanduser(raw)
            if os.path.exists(expanded):
                return expanded
            # 尝试相对于当前目录
            abs_path = os.path.join(os.getcwd(), raw)
            if os.path.exists(abs_path):
                return abs_path

        # 检查根目录关键词
        if re.search(_ROOT_PATTERNS, text, re.IGNORECASE):
            return os.getcwd()

        return None

    def _extract_file_path(self, text: str) -> Optional[str]:
        """从文本中提取具体文件路径（更严格）。"""
        # 匹配带扩展名的路径或已知文件名
        m = re.search(
            r"(?:['\"]?)(([\w\-./]+\.\w{1,10})|([\w\-]+\.(?:py|md|yaml|yml|json|toml|cfg|ini|txt|js|ts|java|go|rs)))(?:['\"]?)",
            text,
            re.IGNORECASE,
        )
        if m:
            raw = m.group(1)
            expanded = os.path.expanduser(raw)
            if os.path.isfile(expanded):
                return expanded
            abs_path = os.path.join(os.getcwd(), raw)
            if os.path.isfile(abs_path):
                return abs_path
        return None

    def _extract_glob_pattern(self, text: str) -> Optional[str]:
        """从文本中提取文件搜索模式。"""
        # 匹配 "*.py"、"*.md"、"test_*.py" 等模式
        m = re.search(r"(\*\.\w+|\w+\*\w*\.\w+|[\w\-]+\.\w+)", text)
        if m:
            return m.group(1)

        # 匹配扩展名描述（如 "python文件"、"md文件"）
        ext_map = {
            "python": "*.py",
            "py": "*.py",
            "markdown": "*.md",
            "md": "*.md",
            "yaml": "*.yaml",
            "yml": "*.yml",
            "json": "*.json",
            "java": "*.java",
            "javascript": "*.js",
            "js": "*.js",
            "typescript": "*.ts",
            "ts": "*.ts",
            "go": "*.go",
            "rust": "*.rs",
            "toml": "*.toml",
            "ini": "*.ini",
            "cfg": "*.cfg",
            "txt": "*.txt",
        }
        for word, pattern in ext_map.items():
            if re.search(rf"\b{word}\b", text, re.IGNORECASE):
                return pattern

        return None

    def _extract_grep_keyword(self, text: str) -> Optional[str]:
        """从文本中提取搜索关键词。"""
        # 匹配引号中的内容
        m = re.search(r"['\"](.+?)['\"]", text)
        if m:
            return m.group(1)

        # 匹配 "搜索/查找 XXX" 模式
        m = re.search(
            rf"(?:{_FIND_VERBS})\s*(?:一下|下|的|包含)?\s*([A-Za-z_][\w.:-]*|[\u4e00-\u9fff]+)",
            text,
            re.IGNORECASE,
        )
        if m:
            return m.group(m.lastindex)

        return None

    # ── 工具函数 ──────────────────────────────────────────

    @staticmethod
    def _format_size(size: int) -> str:
        """格式化文件大小。"""
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"
