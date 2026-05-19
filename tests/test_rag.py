"""RAG 模块测试 — Phase 1-3：分块、关键词索引、混合检索。"""

import os
import json
import tempfile
import shutil

import pytest

# ── Phase 2: AST 分块器测试 ──────────────────────────────────


class TestPythonASTChunker:
    """Python AST 分块器测试。"""

    def test_simple_function(self):
        """模块级函数应被正确提取。"""
        from rag.chunkers.python_chunker import PythonASTChunker
        from rag.models import CodeChunk

        code = '''def hello(name: str) -> str:
    """Say hello."""
    return f"Hello, {name}!"
'''
        chunker = PythonASTChunker()
        chunks = chunker.chunk("test.py", code)

        func_chunks = [c for c in chunks if c.chunk_type == "function"]
        assert len(func_chunks) == 1
        assert func_chunks[0].name == "hello"
        assert func_chunks[0].signature == "def hello(name: str) -> str"
        assert func_chunks[0].docstring == "Say hello."
        assert func_chunks[0].start_line == 1
        assert func_chunks[0].end_line == 3

    def test_class_with_methods(self):
        """类和方法应分别提取。"""
        from rag.chunkers.python_chunker import PythonASTChunker

        code = '''class Calculator:
    """A simple calculator."""

    def __init__(self, value: int = 0):
        self.value = value

    def add(self, x: int) -> int:
        """Add x to value."""
        self.value += x
        return self.value

    def multiply(self, x: int) -> int:
        self.value *= x
        return self.value
'''
        chunker = PythonASTChunker()
        chunks = chunker.chunk("calc.py", code)

        class_chunks = [c for c in chunks if c.chunk_type == "class"]
        method_chunks = [c for c in chunks if c.chunk_type == "method"]

        assert len(class_chunks) == 1
        assert class_chunks[0].name == "Calculator"
        assert len(method_chunks) == 3
        assert method_chunks[0].name == "__init__"
        assert method_chunks[0].parent_name == "Calculator"
        assert method_chunks[1].name == "add"
        assert method_chunks[2].name == "multiply"

    def test_imports_extracted(self):
        """import 语句应被单独提取。"""
        from rag.chunkers.python_chunker import PythonASTChunker

        code = '''import os
import sys
from typing import List, Optional

def main():
    pass
'''
        chunker = PythonASTChunker()
        chunks = chunker.chunk("test.py", code)

        import_chunks = [c for c in chunks if c.chunk_type == "import"]
        assert len(import_chunks) == 1
        assert "import os" in import_chunks[0].content
        assert "from typing" in import_chunks[0].content

    def test_syntax_error_fallback(self):
        """AST 解析失败应回退到 FallbackChunker。"""
        from rag.chunkers.python_chunker import PythonASTChunker

        code = "def broken(:\n    pass"  # 语法错误
        chunker = PythonASTChunker()
        chunks = chunker.chunk("broken.py", code)

        # 回退到 file 级
        assert len(chunks) >= 1
        assert chunks[0].chunk_type == "file"

    def test_empty_file(self):
        """空文件应返回 file 级 chunk。"""
        from rag.chunkers.python_chunker import PythonASTChunker

        chunker = PythonASTChunker()
        chunks = chunker.chunk("empty.py", "")

        assert len(chunks) >= 1

    def test_async_function(self):
        """async 函数应被正确提取。"""
        from rag.chunkers.python_chunker import PythonASTChunker

        code = '''async def fetch_data(url: str) -> dict:
    """Fetch data from URL."""
    import aiohttp
    async with aiohttp.ClientSession() as session:
        resp = await session.get(url)
        return await resp.json()
'''
        chunker = PythonASTChunker()
        chunks = chunker.chunk("async_test.py", code)

        func_chunks = [c for c in chunks if c.chunk_type == "function"]
        assert len(func_chunks) == 1
        assert func_chunks[0].name == "fetch_data"
        assert "async" in func_chunks[0].signature


class TestFallbackChunker:
    """通用回退分块器测试。"""

    def test_small_file(self):
        """小文件应整体返回。"""
        from rag.chunkers import FallbackChunker

        code = "x = 1\ny = 2\n"
        chunker = FallbackChunker()
        chunks = chunker.chunk("small.py", code)

        assert len(chunks) == 1
        assert chunks[0].chunk_type == "file"

    def test_large_file(self):
        """大文件应按行数切分。"""
        from rag.chunkers import FallbackChunker

        code = "\n".join(f"line_{i} = {i}" for i in range(300))
        chunker = FallbackChunker(max_lines=200, chunk_lines=100)
        chunks = chunker.chunk("large.py", code)

        assert len(chunks) > 1
        assert all(c.chunk_type == "block" for c in chunks)


# ── Phase 1: CodeChunk 数据模型测试 ──────────────────────────


class TestCodeChunk:
    """CodeChunk 数据模型测试。"""

    def test_to_metadata(self):
        """metadata 应包含必要字段。"""
        from rag.models import CodeChunk

        chunk = CodeChunk(
            file_path="core/memory.py",
            chunk_type="method",
            name="search",
            content="def search(self, query): ...",
            start_line=130,
            end_line=158,
            parent_name="LongTermMemory",
            signature="def search(query: str, limit: int = 5)",
            language="python",
        )
        meta = chunk.to_metadata()
        assert meta["file_path"] == "core/memory.py"
        assert meta["chunk_type"] == "method"
        assert meta["name"] == "search"
        assert meta["start_line"] == 130

    def test_display_name_with_parent(self):
        """有 parent_name 时应显示 Parent.name。"""
        from rag.models import CodeChunk

        chunk = CodeChunk(
            file_path="a.py", chunk_type="method", name="run",
            content="", start_line=1, end_line=5,
            parent_name="AgentLoop",
        )
        assert chunk.display_name() == "AgentLoop.run"

    def test_display_name_without_parent(self):
        """无 parent_name 时应显示 file::name。"""
        from rag.models import CodeChunk

        chunk = CodeChunk(
            file_path="a.py", chunk_type="function", name="main",
            content="", start_line=1, end_line=5,
        )
        assert chunk.display_name() == "a.py::main"


# ── Phase 3: KeywordIndex 测试 ────────────────────────────────


class TestKeywordIndex:
    """SQLite FTS5 关键词索引测试。"""

    @pytest.fixture
    def tmp_index(self, tmp_path):
        """创建临时关键词索引。"""
        from rag.keyword_index import KeywordIndex
        db_path = str(tmp_path / "test_kw.db")
        idx = KeywordIndex(db_path=db_path)
        yield idx
        idx.close()

    def _make_chunk(self, name, content, chunk_type="function", **kwargs):
        from rag.models import CodeChunk
        return CodeChunk(
            file_path=kwargs.get("file_path", "test.py"),
            chunk_type=chunk_type,
            name=name,
            content=content,
            start_line=kwargs.get("start_line", 1),
            end_line=kwargs.get("end_line", 10),
            language="python",
            parent_name=kwargs.get("parent_name", None),
        )

    def test_add_and_search(self, tmp_index):
        """添加后应能检索到。"""
        chunks = [
            self._make_chunk("authenticate", "def authenticate(user, password):\n    return check_credentials(user, password)"),
            self._make_chunk("authorize", "def authorize(token):\n    return validate_token(token)"),
            self._make_chunk("calculate", "def calculate(x, y):\n    return x + y"),
        ]
        tmp_index.add_chunks(chunks)

        results = tmp_index.search("authenticate")
        assert len(results) > 0
        found_names = [c.name for c, _ in results]
        assert "authenticate" in found_names

    def test_delete_by_file(self, tmp_index):
        """按文件删除后应检索不到。"""
        chunks = [self._make_chunk("foo", "def foo(): pass", file_path="a.py")]
        tmp_index.add_chunks(chunks)
        assert tmp_index.count() > 0

        tmp_index.delete_by_file("a.py")
        # 删除后 count 应为 0
        assert tmp_index.count() == 0

    def test_count(self, tmp_index):
        """count 应返回正确的代码块数。"""
        chunks = [
            self._make_chunk("f1", "def f1(): pass"),
            self._make_chunk("f2", "def f2(): pass"),
        ]
        tmp_index.add_chunks(chunks)
        assert tmp_index.count() == 2

    def test_get_indexed_files(self, tmp_index):
        """应返回已索引的文件列表。"""
        chunks = [
            self._make_chunk("f1", "def f1(): pass", file_path="a.py"),
            self._make_chunk("f2", "def f2(): pass", file_path="b.py"),
        ]
        tmp_index.add_chunks(chunks)
        files = tmp_index.get_indexed_files()
        assert "a.py" in files
        assert "b.py" in files


# ── Phase 3: CodeRAGPipeline 集成测试 ────────────────────────


class TestCodeRAGPipeline:
    """CodeRAGPipeline 集成测试（使用临时目录和 mock embedding）。"""

    @pytest.fixture
    def tmp_project(self, tmp_path):
        """创建临时项目目录，包含几个 Python 文件。"""
        # memory.py
        (tmp_path / "memory.py").write_text('''"""Memory module."""

import hashlib
import json

class MemoryManager:
    """Memory manager facade."""

    def __init__(self, path: str):
        self.path = path
        self.store = {}

    def search(self, query: str, limit: int = 5) -> list:
        """Search for relevant memories."""
        results = []
        for key, val in self.store.items():
            if query.lower() in key.lower():
                results.append(val)
        return results[:limit]

    def store_fact(self, content: str) -> bool:
        """Store a fact to memory."""
        key = hashlib.md5(content.encode()).hexdigest()
        if key in self.store:
            return False
        self.store[key] = content
        return True
''', encoding="utf-8")

        # agent.py
        (tmp_path / "agent.py").write_text('''"""Agent loop module."""

from typing import Optional

class AgentLoop:
    """Main agent loop with LangGraph."""

    def __init__(self, tools, llm, memory=None):
        self.tools = tools
        self.llm = llm
        self.memory = memory

    def run(self, user_input: str) -> str:
        """Run the agent loop."""
        return f"Processing: {user_input}"

    def stream(self, user_input: str):
        """Stream the agent loop."""
        yield self.run(user_input)
''', encoding="utf-8")

        # config.yaml
        (tmp_path / "config.yaml").write_text("rag:\n  enabled: true\n", encoding="utf-8")

        return tmp_path

    def test_index_directory(self, tmp_project):
        """索引目录应生成代码块。"""
        from rag.chunkers.python_chunker import PythonASTChunker

        chunker = PythonASTChunker()

        # 索引 memory.py
        with open(tmp_project / "memory.py", "r") as f:
            content = f.read()
        chunks = chunker.chunk(str(tmp_project / "memory.py"), content)

        # 应提取出 MemoryManager 类 + 方法 + imports
        types = [c.chunk_type for c in chunks]
        assert "import" in types
        assert "class" in types
        assert "method" in types

        names = [c.name for c in chunks]
        assert "MemoryManager" in names
        assert "search" in names
        assert "store_fact" in names

    def test_search_code_tool_no_pipeline(self):
        """无 pipeline 时应返回错误信息。"""
        from tools.builtin.rag_tools import SearchCodeTool

        tool = SearchCodeTool(rag_pipeline=None)
        result = tool._run(query="test")
        assert "未初始化" in result or "错误" in result

    def test_index_workspace_tool_no_pipeline(self):
        """无 pipeline 时应返回错误信息。"""
        from tools.builtin.rag_tools import IndexWorkspaceTool

        tool = IndexWorkspaceTool(rag_pipeline=None)
        result = tool._run(directory=".")
        assert "未初始化" in result or "错误" in result


# ── 分块工具函数测试 ────────────────────────────────────────


class TestChunkerUtils:
    """分块工具函数测试。"""

    def test_should_index_file(self, tmp_path):
        """应正确判断文件是否需要索引。"""
        from rag.chunkers import should_index_file

        py_file = tmp_path / "test.py"
        py_file.write_text("x = 1")
        assert should_index_file(str(py_file)) is True

        pyc_file = tmp_path / "test.pyc"
        pyc_file.write_text("binary")
        assert should_index_file(str(pyc_file)) is False

        big_file = tmp_path / "big.py"
        big_file.write_text("x = 1" * 50000)
        assert should_index_file(str(big_file), max_file_size=100) is False

    def test_should_index_dir(self):
        """应跳过 .git/node_modules 等目录。"""
        from rag.chunkers import should_index_dir

        assert should_index_dir("src") is True
        assert should_index_dir(".git") is False
        assert should_index_dir("node_modules") is False
        assert should_index_dir("__pycache__") is False

    def test_get_chunker_for_file(self):
        """应按扩展名选择正确的分块器。"""
        from rag.chunkers import get_chunker_for_file
        from rag.chunkers.python_chunker import PythonASTChunker
        from rag.chunkers import FallbackChunker

        assert isinstance(get_chunker_for_file("test.py"), PythonASTChunker)
        assert isinstance(get_chunker_for_file("test.java"), FallbackChunker)
        assert isinstance(get_chunker_for_file("test.js"), FallbackChunker)


# ── RetrievalResult 格式化测试 ────────────────────────────────


class TestRetrievalResult:
    """检索结果格式化测试。"""

    def test_format_for_llm(self):
        """应格式化为 LLM 可读的文本。"""
        from rag.models import CodeChunk, RetrievalResult

        chunk = CodeChunk(
            file_path="core/memory.py",
            chunk_type="method",
            name="search",
            content="def search(self, query): ...",
            start_line=130,
            end_line=158,
            parent_name="LongTermMemory",
            language="python",
        )
        result = RetrievalResult(chunk=chunk, score=0.85, semantic_score=0.7, keyword_score=0.3)
        text = result.format_for_llm()

        assert "LongTermMemory.search" in text
        assert "0.85" in text
        assert "```python" in text
        assert "def search" in text


# ── Retrieval enhancement tests ─────────────────────────────────


class TestRetrievalEnhancements:
    """query rewrite、rerank、search cache 测试。"""

    def _result(self, name, content, score=0.2, parent_name=None):
        from rag.models import CodeChunk, RetrievalResult

        chunk = CodeChunk(
            file_path="core/memory.py",
            chunk_type="method",
            name=name,
            content=content,
            start_line=1,
            end_line=10,
            parent_name=parent_name,
            signature=f"def {name}(self):",
            language="python",
        )
        return RetrievalResult(chunk=chunk, score=score, semantic_score=score)

    def test_query_rewrite_expands_code_terms(self):
        from rag.retrieval_enhancements import QueryRewriter

        rewriter = QueryRewriter()
        variants = rewriter.rewrite("MemoryManager 缓存")

        assert variants[0] == "MemoryManager 缓存"
        assert any("Memory Manager" in q for q in variants)
        assert any("cache" in q for q in variants)

    def test_heuristic_rerank_promotes_matching_symbol(self):
        from rag.retrieval_enhancements import ResultReranker

        reranker = ResultReranker()
        reranker.enabled = True
        reranker.method = "heuristic"

        relevant = self._result(
            "search",
            "def search(self, query): return self.store.get(query)",
            parent_name="MemoryManager",
        )
        unrelated = self._result(
            "render",
            "def render(self): return '<html></html>'",
            parent_name="Renderer",
        )

        ranked = reranker.rerank(
            "MemoryManager search",
            [unrelated, relevant],
            top_k=2,
            query_variants=["MemoryManager search Memory Manager"],
        )

        assert ranked[0].chunk.name == "search"

    def test_search_cache_uses_fingerprint_and_returns_copy(self):
        from rag.retrieval_enhancements import SearchCache

        cache = SearchCache()
        cache.enabled = True
        cache.ttl_seconds = 30
        cache.max_entries = 2
        result = self._result("search", "def search(self): pass")

        cache.set("k", "fp1", [result])
        cached = cache.get("k", "fp1")
        assert cached is not None
        cached[0].score = 0.0

        cached_again = cache.get("k", "fp1")
        assert cached_again[0].score == result.score
        assert cache.get("k", "fp2") is None
