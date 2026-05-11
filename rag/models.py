"""RAG 数据模型 — 代码块、检索结果等结构化定义。"""

from typing import List, Optional, Literal
from pydantic import BaseModel, Field


class CodeChunk(BaseModel):
    """代码块 — RAG 索引和检索的基本单元。

    每个代码块对应一个语义完整的代码片段：
    - Python: 模块级函数、类定义、类方法
    - 其他语言: 按行数切分的代码段
    """

    file_path: str = Field(description="源文件路径（相对索引源目录）")
    chunk_type: Literal["file", "class", "method", "function", "import", "block"] = Field(
        description="块类型：file=文件级, class=类定义, method=类方法, function=模块函数, import=导入区, block=通用块"
    )
    name: str = Field(description="块名称：类名/方法名/函数名，block 类型为文件名")
    content: str = Field(description="块的完整代码内容")
    start_line: int = Field(description="起始行号（1-indexed）")
    end_line: int = Field(description="结束行号（1-indexed）")
    parent_name: Optional[str] = Field(default=None, description="所属父级名称（如方法所属类名）")
    signature: Optional[str] = Field(default=None, description="方法/函数签名")
    docstring: Optional[str] = Field(default=None, description="文档字符串")
    language: str = Field(default="python", description="编程语言")
    source: Optional[str] = Field(default=None, description="索引源目录标签，用于区分不同项目")

    def to_metadata(self) -> dict:
        """转换为向量存储的 metadata 字典。"""
        meta = {
            "file_path": self.file_path,
            "chunk_type": self.chunk_type,
            "name": self.name,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "language": self.language,
        }
        if self.source:
            meta["source"] = self.source
        if self.parent_name:
            meta["parent_name"] = self.parent_name
        if self.signature:
            meta["signature"] = self.signature
        return meta

    def display_name(self) -> str:
        """人类可读的显示名称。"""
        prefix = f"[{self.source}] " if self.source else ""
        if self.parent_name:
            return f"{prefix}{self.parent_name}.{self.name}"
        return f"{prefix}{self.file_path}::{self.name}"


class RetrievalResult(BaseModel):
    """检索结果 — 带分数的代码块。"""

    chunk: CodeChunk = Field(description="匹配的代码块")
    score: float = Field(description="综合检索分数（0-1）")
    semantic_score: float = Field(default=0.0, description="语义相似度分数")
    keyword_score: float = Field(default=0.0, description="关键词匹配分数")
    source: str = Field(default="hybrid", description="来源：semantic/keyword/hybrid")

    def format_for_llm(self) -> str:
        """格式化为 LLM 可读的文本。"""
        lines = [
            f"📄 {self.chunk.display_name()} "
            f"(score={self.score:.2f}, {self.chunk.chunk_type}, "
            f"L{self.chunk.start_line}-{self.chunk.end_line})",
            f"```{self.chunk.language}",
            self.chunk.content,
            "```",
        ]
        return "\n".join(lines)


class IndexStats(BaseModel):
    """索引统计信息。"""

    total_files: int = 0
    total_chunks: int = 0
    chunks_by_type: dict = Field(default_factory=dict)
    chunks_by_language: dict = Field(default_factory=dict)
    index_time: float = 0.0
    last_updated: Optional[str] = None
