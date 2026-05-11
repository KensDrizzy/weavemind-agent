"""Python AST 代码分块器 — 按类/方法/函数级拆分 Python 文件。

分块策略（参考 PaiCLI 的 JavaParser 方案）：
1. 模块级 import 区：单独一个 chunk
2. 模块级函数：每个函数一个 chunk
3. 类定义：类声明+前5行字段为一个 chunk（类级概览）
4. 类方法：每个方法一个 chunk（完整方法体）
5. 模块级代码：其他顶层代码合并为一个 chunk

容错：AST 解析失败时自动回退到 FallbackChunker。
"""

import ast
import logging
import os
from typing import List, Optional

from rag.chunkers import BaseChunker, FallbackChunker
from rag.models import CodeChunk

logger = logging.getLogger(__name__)


class PythonASTChunker(BaseChunker):
    """基于 Python ast 标准库的代码分块器。"""

    # 类级概览保留的字段行数上限
    CLASS_OVERVIEW_LINES = 5

    def chunk(self, file_path: str, content: str) -> List[CodeChunk]:
        """解析 Python 文件，返回结构化代码块列表。

        解析失败时自动回退到 FallbackChunker。
        """
        try:
            tree = ast.parse(content)
        except SyntaxError as e:
            logger.debug(f"Python AST 解析失败 {file_path}: {e}，回退到行级分块")
            return FallbackChunker().chunk(file_path, content)

        lines = content.split("\n")
        file_name = os.path.basename(file_path)
        chunks: List[CodeChunk] = []

        # 1. 提取 import 区
        import_chunk = self._extract_imports(tree, lines, file_path, file_name)
        if import_chunk:
            chunks.append(import_chunk)

        # 2. 遍历顶层节点
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                # 类级概览 chunk
                class_chunk = self._create_class_overview(node, lines, file_path)
                chunks.append(class_chunk)

                # 类方法 chunk
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method_chunk = self._create_method_chunk(
                            item, lines, file_path, parent_name=node.name
                        )
                        chunks.append(method_chunk)

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # 模块级函数
                func_chunk = self._create_function_chunk(node, lines, file_path)
                chunks.append(func_chunk)

            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.Expr)):
                # 模块级赋值/表达式 — 不单独成块，归入 file 级概览
                pass

        # 3. 如果没有提取到任何结构化 chunk，回退到 file 级
        if not chunks:
            chunks.append(
                CodeChunk(
                    file_path=file_path,
                    chunk_type="file",
                    name=file_name,
                    content=content,
                    start_line=1,
                    end_line=len(lines),
                    language="python",
                )
            )

        return chunks

    def _extract_imports(
        self, tree: ast.Module, lines: list, file_path: str, file_name: str
    ) -> Optional[CodeChunk]:
        """提取模块级 import 语句为一个 chunk。"""
        import_nodes = [
            n for n in tree.body
            if isinstance(n, (ast.Import, ast.ImportFrom))
        ]
        if not import_nodes:
            return None

        start_line = import_nodes[0].lineno
        end_line = max(n.end_lineno or n.lineno for n in import_nodes)
        import_content = "\n".join(lines[start_line - 1 : end_line])

        return CodeChunk(
            file_path=file_path,
            chunk_type="import",
            name=f"{file_name}:imports",
            content=import_content,
            start_line=start_line,
            end_line=end_line,
            language="python",
        )

    def _create_class_overview(
        self, node: ast.ClassDef, lines: list, file_path: str
    ) -> CodeChunk:
        """创建类级概览 chunk：类声明 + 前几行字段/文档字符串。

        不包含完整类体（方法单独成块），只保留类签名和字段概览。
        """
        start = node.lineno
        # 概览行数：类声明 + 前 N 行
        overview_end = min(
            start + self.CLASS_OVERVIEW_LINES,
            node.end_lineno or start + 10,
        )

        # 如果类体很短（≤10行），直接包含完整类
        class_total_lines = (node.end_lineno or start + 10) - start + 1
        if class_total_lines <= 10:
            overview_end = node.end_lineno or start + 10

        content = "\n".join(lines[start - 1 : overview_end])
        docstring = ast.get_docstring(node)

        # 构建签名：class ClassName(Base1, Base2)
        bases = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                bases.append(ast.dump(base))
        signature = f"class {node.name}"
        if bases:
            signature += f"({', '.join(bases)})"

        return CodeChunk(
            file_path=file_path,
            chunk_type="class",
            name=node.name,
            content=content,
            start_line=start,
            end_line=overview_end,
            signature=signature,
            docstring=docstring,
            language="python",
        )

    def _create_method_chunk(
        self,
        node: ast.FunctionDef,
        lines: list,
        file_path: str,
        parent_name: Optional[str] = None,
    ) -> CodeChunk:
        """创建方法级 chunk：完整方法体。"""
        start = node.lineno
        end = node.end_lineno or start + 1
        content = "\n".join(lines[start - 1 : end])
        docstring = ast.get_docstring(node)
        signature = self._build_signature(node)

        return CodeChunk(
            file_path=file_path,
            chunk_type="method",
            name=node.name,
            content=content,
            start_line=start,
            end_line=end,
            parent_name=parent_name,
            signature=signature,
            docstring=docstring,
            language="python",
        )

    def _create_function_chunk(
        self, node: ast.FunctionDef, lines: list, file_path: str
    ) -> CodeChunk:
        """创建模块级函数 chunk：完整函数体。"""
        start = node.lineno
        end = node.end_lineno or start + 1
        content = "\n".join(lines[start - 1 : end])
        docstring = ast.get_docstring(node)
        signature = self._build_signature(node)

        return CodeChunk(
            file_path=file_path,
            chunk_type="function",
            name=node.name,
            content=content,
            start_line=start,
            end_line=end,
            signature=signature,
            docstring=docstring,
            language="python",
        )

    @staticmethod
    def _build_signature(node: ast.FunctionDef) -> str:
        """构建函数/方法签名字符串。"""
        args = []
        for arg in node.args.args:
            arg_str = arg.arg
            if arg.annotation:
                if isinstance(arg.annotation, ast.Name):
                    arg_str += f": {arg.annotation.id}"
                elif isinstance(arg.annotation, ast.Constant):
                    arg_str += f": {arg.annotation.value}"
            args.append(arg_str)

        prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
        sig = f"{prefix}def {node.name}({', '.join(args)})"

        # 返回值注解
        if node.returns:
            if isinstance(node.returns, ast.Name):
                sig += f" -> {node.returns.id}"
            elif isinstance(node.returns, ast.Constant):
                sig += f" -> {node.returns.value}"

        return sig
