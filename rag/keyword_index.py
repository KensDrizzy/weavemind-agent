"""关键词索引 — 基于 SQLite FTS5 的全文检索。

提供 BM25 风格的关键词检索，与向量检索互补：
- 向量检索擅长语义匹配（"用户认证" → 找到 login 方法）
- 关键词检索擅长精确匹配（"MemoryManager" → 精确找到类名）

FTS5 是 SQLite 内置的全文搜索引擎，无需额外服务。
"""

import json
import logging
import os
import sqlite3
from typing import List, Optional, Tuple

from rag.models import CodeChunk

logger = logging.getLogger(__name__)


class KeywordIndex:
    """SQLite FTS5 关键词索引 — 支持代码标识符精确匹配。

    特性：
    - FTS5 全文检索，支持 BM25 排序
    - tokenize="unicode61" 支持中文分词
    - 使用 contentless 模式（不存储原文，只建索引）
    - 额外建 metadata 表存储代码块详情
    - 增量更新：按 file_path 删除旧 chunk 再插入新 chunk
    """

    def __init__(self, db_path: str = ".weavemind/rag/keyword_index.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_table()

    def _create_table(self):
        """创建 FTS5 虚拟表和 metadata 表。"""
        # FTS5 普通（非 contentless）模式：存原文，支持删除和更新
        self._conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS code_chunks_fts USING fts5(
                file_path,
                chunk_type,
                name,
                parent_name,
                signature,
                content,
                tokenize='unicode61'
            )
        """)
        # metadata 表：存储代码块完整信息
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS chunk_metadata(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT,
                chunk_type TEXT,
                name TEXT,
                parent_name TEXT,
                signature TEXT,
                content TEXT,
                start_line INTEGER,
                end_line INTEGER,
                language TEXT,
                metadata TEXT
            )
        """)
        # 索引加速按文件删除
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunk_file
            ON chunk_metadata(file_path)
        """)
        self._conn.commit()

    def add_chunks(self, chunks: List[CodeChunk]):
        """批量添加代码块到索引。"""
        if not chunks:
            return

        # 先删除同文件的旧 chunk
        files = set(c.file_path for c in chunks)
        for fp in files:
            self.delete_by_file(fp)

        for chunk in chunks:
            metadata = json.dumps(chunk.to_metadata(), ensure_ascii=False)

            # 写入 metadata 表
            cursor = self._conn.execute(
                """INSERT INTO chunk_metadata
                   (file_path, chunk_type, name, parent_name, signature,
                    content, start_line, end_line, language, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    chunk.file_path,
                    chunk.chunk_type,
                    chunk.name,
                    chunk.parent_name or "",
                    chunk.signature or "",
                    chunk.content,
                    chunk.start_line,
                    chunk.end_line,
                    chunk.language,
                    metadata,
                ),
            )

            # 写入 FTS 索引
            self._conn.execute(
                """INSERT INTO code_chunks_fts
                   (file_path, chunk_type, name, parent_name, signature, content)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    chunk.file_path,
                    chunk.chunk_type,
                    chunk.name,
                    chunk.parent_name or "",
                    chunk.signature or "",
                    chunk.content,
                ),
            )

        self._conn.commit()
        logger.info(f"关键词索引新增 {len(chunks)} 个代码块")

    def delete_by_file(self, file_path: str):
        """删除指定文件的所有代码块。"""
        # 从 FTS 索引中删除（按 file_path 字段匹配）
        self._conn.execute(
            "DELETE FROM code_chunks_fts WHERE file_path = ?",
            (file_path,),
        )

        # 从 metadata 表中删除
        self._conn.execute(
            "DELETE FROM chunk_metadata WHERE file_path = ?",
            (file_path,),
        )
        self._conn.commit()

    def search(
        self, query: str, top_k: int = 10,
        file_filter: Optional[str] = None,
        source_filter: Optional[str] = None,
    ) -> List[Tuple[CodeChunk, float]]:
        """关键词检索，返回 (代码块, BM25分数) 列表。

        Args:
            query: 检索关键词
            top_k: 返回结果数量
            file_filter: 文件路径过滤（SQL LIKE 模式，如 '%.py'）
            source_filter: 索引源过滤（如 'weavemind'）

        Returns:
            List of (CodeChunk, bm25_score) 按分数降序
        """
        if not query.strip():
            return []

        # FTS5 查询：对每个词项做前缀匹配
        tokens = query.strip().split()
        fts_query = " OR ".join(f'"{t}"*' for t in tokens if len(t) > 1)
        if not fts_query:
            return []

        try:
            sql = """
                SELECT m.file_path, m.chunk_type, m.name, m.parent_name,
                       m.signature, m.content, m.start_line, m.end_line,
                       m.language, m.metadata,
                       bm25(code_chunks_fts) as score
                FROM code_chunks_fts
                JOIN chunk_metadata m ON m.id = code_chunks_fts.rowid
                WHERE code_chunks_fts MATCH ?
            """
            params: list = [fts_query]

            if file_filter:
                sql += " AND m.file_path LIKE ?"
                params.append(file_filter)
            if source_filter:
                sql += " AND m.metadata LIKE ?"
                params.append(f'%"source": "{source_filter}"%')

            sql += " ORDER BY score LIMIT ?"
            params.append(top_k)

            cursor = self._conn.execute(sql, params)
        except sqlite3.OperationalError as e:
            logger.warning(f"FTS5 查询失败: {e}, query={fts_query}")
            return []

        results = []
        for row in cursor:
            (fp, ctype, name, parent, sig, content,
             start, end, lang, metadata_str, raw_score) = row

            # bm25 返回负数，取绝对值并归一化
            score = min(abs(raw_score) / 10.0, 1.0)

            # 从 metadata JSON 中提取 source
            source = None
            if metadata_str:
                try:
                    meta_dict = json.loads(metadata_str)
                    source = meta_dict.get("source")
                except (json.JSONDecodeError, TypeError):
                    pass

            chunk = CodeChunk(
                file_path=fp,
                chunk_type=ctype,
                name=name,
                content=content,
                start_line=start,
                end_line=end,
                parent_name=parent or None,
                signature=sig or None,
                language=lang,
                source=source,
            )
            results.append((chunk, score))

        return results

    def count(self) -> int:
        """返回索引中的代码块总数。"""
        cursor = self._conn.execute("SELECT COUNT(*) FROM chunk_metadata")
        return cursor.fetchone()[0]

    def get_indexed_files(self) -> List[str]:
        """返回已索引的文件路径列表。"""
        cursor = self._conn.execute(
            "SELECT DISTINCT file_path FROM chunk_metadata"
        )
        return [row[0] for row in cursor]

    def close(self):
        """关闭数据库连接。"""
        self._conn.close()
