"""代码分块器基类 — 定义分块接口和通用回退策略。"""

import logging
import os
from abc import ABC, abstractmethod
from typing import List, Optional

from langchain.text_splitter import RecursiveCharacterTextSplitter

from rag.models import CodeChunk

logger = logging.getLogger(__name__)

# 按语言判断是否支持 AST 解析
AST_SUPPORTED_EXTENSIONS = {
    ".py": "python",
}

# 代码文件扩展名到语言映射
CODE_EXTENSIONS = {
    ".py": "python",
    ".java": "java",
    ".js": "javascript",
    ".ts": "typescript",
    ".jsx": "javascript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".cpp": "cpp",
    ".c": "c",
    ".h": "c",
    ".hpp": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".sh": "shell",
    ".bash": "shell",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".xml": "xml",
    ".md": "markdown",
    ".rst": "rst",
    ".toml": "toml",
    ".cfg": "ini",
    ".ini": "ini",
}

# 跳过索引的目录
SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__",
    ".venv", "venv", ".env", ".tox", ".mypy_cache",
    ".pytest_cache", "dist", "build", "egg-info",
    ".idea", ".vscode", ".weavemind",
}

# 跳过索引的文件模式
SKIP_FILE_PATTERNS = {
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe",
    ".class", ".jar", ".war", ".zip", ".tar", ".gz",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico",
    ".mp3", ".mp4", ".wav", ".avi", ".mov",
    ".db", ".sqlite", ".sqlite3",
    ".pkl", ".pickle", ".npy", ".npz",
}


class BaseChunker(ABC):
    """代码分块器基类 — 子类实现特定语言的 AST 解析。"""

    @abstractmethod
    def chunk(self, file_path: str, content: str) -> List[CodeChunk]:
        """将文件内容拆分为代码块列表。"""
        ...


class FallbackChunker(BaseChunker):
    """通用回退分块器 — 按字符数切分，适用于所有文件类型。

    策略：
    - 文件 ≤ 200 行：整体作为一个 chunk（file 级）
    - 文件 > 200 行：按 chunk_size 行数切分，每段带行号
    """

    def __init__(self, max_lines: int = 200, chunk_lines: int = 80):
        self.max_lines = max_lines
        self.chunk_lines = chunk_lines

    def chunk(self, file_path: str, content: str) -> List[CodeChunk]:
        """按行数切分大文件，小文件整体返回。"""
        lines = content.split("\n")
        file_name = os.path.basename(file_path)
        language = CODE_EXTENSIONS.get(
            os.path.splitext(file_path)[1].lower(), "text"
        )

        # 小文件整体返回
        if len(lines) <= self.max_lines:
            return [
                CodeChunk(
                    file_path=file_path,
                    chunk_type="file",
                    name=file_name,
                    content=content,
                    start_line=1,
                    end_line=len(lines),
                    language=language,
                )
            ]

        # 大文件按行数切分
        chunks = []
        for i in range(0, len(lines), self.chunk_lines):
            segment = lines[i : i + self.chunk_lines]
            start = i + 1  # 1-indexed
            end = min(i + self.chunk_lines, len(lines))
            chunks.append(
                CodeChunk(
                    file_path=file_path,
                    chunk_type="block",
                    name=f"{file_name}:L{start}-{end}",
                    content="\n".join(segment),
                    start_line=start,
                    end_line=end,
                    language=language,
                )
            )
        return chunks


def should_index_file(file_path: str, max_file_size: int = 100_000) -> bool:
    """判断文件是否应该被索引。

    Args:
        file_path: 文件路径
        max_file_size: 最大文件大小（字节），默认 100KB

    Returns:
        True 表示应该索引
    """
    # 检查扩展名
    _, ext = os.path.splitext(file_path)
    if ext.lower() in SKIP_FILE_PATTERNS:
        return False

    # 检查文件大小
    try:
        if os.path.getsize(file_path) > max_file_size:
            return False
    except OSError:
        return False

    return True


def should_index_dir(dir_path: str) -> bool:
    """判断目录是否应该被索引（跳过 .git/node_modules 等）。"""
    dir_name = os.path.basename(dir_path)
    return dir_name not in SKIP_DIRS


def get_chunker_for_file(file_path: str) -> BaseChunker:
    """根据文件扩展名选择合适的分块器。

    支持 AST 的语言使用专用分块器，其余使用 FallbackChunker。
    """
    from rag.chunkers.python_chunker import PythonASTChunker

    ext = os.path.splitext(file_path)[1].lower()
    language = AST_SUPPORTED_EXTENSIONS.get(ext)

    if language == "python":
        return PythonASTChunker()

    return FallbackChunker()
