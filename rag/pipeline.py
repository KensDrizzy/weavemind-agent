"""CodeRAGPipeline — 代码库 RAG 核心管线。

整合三个子系统：
1. AST 分块器 — 按代码结构拆分文件
2. Chroma 向量存储 — 语义检索
3. SQLite FTS5 关键词索引 — 精确匹配

对外提供统一接口：index_files() / search() / get_stats()
"""

import hashlib
import json
import logging
import os
import time
from typing import List, Optional

import settings
from langchain_core.documents import Document
from langchain_chroma import Chroma

from rag.models import CodeChunk, RetrievalResult, IndexStats
from rag.chunkers import (
    get_chunker_for_file,
    should_index_file,
    should_index_dir,
    CODE_EXTENSIONS,
)
from rag.keyword_index import KeywordIndex

logger = logging.getLogger(__name__)


def _create_embeddings():
    """根据配置创建 Embedding 实例。

    支持 openai / ollama 两种 provider。
    """
    provider = settings.get("rag.embedding.provider", "openai")
    model = settings.get("rag.embedding.model", "text-embedding-3-small")

    if provider == "ollama":
        from langchain_ollama import OllamaEmbeddings
        base_url = settings.get("rag.embedding.base_url", "http://localhost:11434")
        return OllamaEmbeddings(model=model, base_url=base_url)

    # 默认 OpenAI 兼容端点
    from langchain_openai import OpenAIEmbeddings
    base_url = settings.get("rag.embedding.base_url", None)
    api_key = settings.get("rag.embedding.api_key", None)
    kwargs = {"model": model}
    if base_url:
        kwargs["base_url"] = base_url
    if api_key:
        kwargs["api_key"] = api_key
    # 如果没有显式配置 api_key，尝试从环境变量读取
    if "api_key" not in kwargs:
        import os
        env_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")
        if env_key:
            kwargs["api_key"] = env_key
    # 关键：非 OpenAI 官方 API（如 Qwen/DashScope）不支持 tiktoken 分词后传 token IDs，
    # 必须关闭 check_embedding_ctx_length，直接传原始文本给 API
    kwargs["check_embedding_ctx_length"] = False
    return OpenAIEmbeddings(**kwargs)


class CodeRAGPipeline:
    """代码库 RAG 管线 — 索引、检索、统计。

    使用方式：
        pipeline = CodeRAGPipeline()
        pipeline.index_directory("./src")
        results = pipeline.search("用户认证逻辑")
    """

    def __init__(self):
        chroma_dir = settings.get("rag.chroma_dir", ".weavemind/chroma")
        os.makedirs(chroma_dir, exist_ok=True)

        self.embeddings = _create_embeddings()
        self.vector_store = Chroma(
            persist_directory=chroma_dir,
            embedding_function=self.embeddings,
        )
        self.keyword_index = KeywordIndex(
            db_path=settings.get("rag.keyword_db", ".weavemind/rag/keyword_index.db")
        )
        self._metadata_path = os.path.join(
            os.path.dirname(chroma_dir), "rag", "index_metadata.json"
        )
        self._indexed_files: dict = {}  # file_path -> {hash, timestamp}
        self._load_metadata()

    # ── 索引 ──────────────────────────────────────────────

    def index_file(
        self, file_path: str, source: Optional[str] = None
    ) -> int:
        """索引单个文件，返回生成的 chunk 数。

        Args:
            file_path: 文件绝对路径或相对路径
            source: 索引源标签，用于区分不同项目（如 "weavemind", "omniagent"）
        """
        if not should_index_file(file_path):
            return 0

        # 增量索引：文件未变更则跳过
        file_hash = self._file_hash(file_path)
        cache_key = f"{source}::{file_path}" if source else file_path
        if self._is_file_unchanged(cache_key, file_hash):
            logger.debug(f"文件未变更，跳过: {file_path}")
            return 0

        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as e:
            logger.warning(f"读取文件失败: {file_path}: {e}")
            return 0

        # 分块
        chunker = get_chunker_for_file(file_path)
        chunks = chunker.chunk(file_path, content)

        if not chunks:
            return 0

        # 给每个 chunk 打上 source 标签
        if source:
            for chunk in chunks:
                chunk.source = source

        # 写入向量存储
        self._add_to_vector_store(chunks)

        # 写入关键词索引
        self.keyword_index.add_chunks(chunks)

        # 更新元数据
        self._indexed_files[cache_key] = {
            "hash": file_hash,
            "timestamp": time.time(),
            "chunks": len(chunks),
            "source": source,
        }
        self._save_metadata()

        logger.info(f"索引完成: {file_path} → {len(chunks)} 个代码块 (source={source})")
        return len(chunks)

    def index_directory(
        self, dir_path: str, max_files: int = 500, source: Optional[str] = None
    ) -> IndexStats:
        """递归索引目录下的所有代码文件。

        Args:
            dir_path: 目录路径（绝对路径或相对路径）
            max_files: 最大索引文件数（防止意外索引超大目录）
            source: 索引源标签，如 "weavemind"、"omniagent"。
                    不指定时自动取目录名（如 /path/to/MyProject → "MyProject"）

        Returns:
            IndexStats 索引统计
        """
        # 自动推断 source 标签
        if not source:
            source = os.path.basename(os.path.abspath(dir_path))

        start_time = time.time()
        total_files = 0
        total_chunks = 0
        skipped_files = 0
        failed_files = 0
        chunks_by_type = {}
        chunks_by_lang = {}

        # 第一遍：收集所有待索引文件
        all_files = []
        for root, dirs, files in os.walk(dir_path):
            dirs[:] = [d for d in dirs if should_index_dir(os.path.join(root, d))]
            for fname in files:
                fpath = os.path.join(root, fname)
                if should_index_file(fpath):
                    all_files.append(fpath)

        total_candidates = len(all_files)
        print(f"  发现 {total_candidates} 个可索引文件")

        # 第二遍：逐文件索引 + 实时进度
        for idx, fpath in enumerate(all_files, 1):
            if total_files >= max_files:
                logger.warning(f"达到最大索引文件数 {max_files}，停止索引")
                break

            try:
                n = self.index_file(fpath, source=source)
                if n > 0:
                    total_files += 1
                    total_chunks += n
                    # 实时打印进度
                    rel_path = os.path.relpath(fpath, dir_path)
                    print(f"  [{idx}/{total_candidates}] ✓ {rel_path} → {n} 块")
                else:
                    skipped_files += 1
                    # 每 50 个跳过文件打印一次进度
                    if skipped_files % 50 == 0:
                        print(f"  [{idx}/{total_candidates}] 已跳过 {skipped_files} 个未变更文件...")
            except Exception as e:
                failed_files += 1
                rel_path = os.path.relpath(fpath, dir_path)
                print(f"  [{idx}/{total_candidates}] ✗ {rel_path}: {e}")

        # 统计
        for fp, meta in self._indexed_files.items():
            ext = os.path.splitext(fp)[1].lower()
            lang = CODE_EXTENSIONS.get(ext, "other")
            chunks_by_lang[lang] = chunks_by_lang.get(lang, 0) + meta.get("chunks", 0)

        elapsed = time.time() - start_time
        stats = IndexStats(
            total_files=total_files,
            total_chunks=total_chunks,
            chunks_by_type=chunks_by_type,
            chunks_by_language=chunks_by_lang,
            index_time=elapsed,
            last_updated=time.strftime("%Y-%m-%d %H:%M:%S"),
        )

        # 汇总报告
        print(f"\n  索引完成: {total_files} 文件, {total_chunks} 代码块, "
              f"跳过 {skipped_files}, 失败 {failed_files}, 耗时 {elapsed:.1f}s")

        logger.info(
            f"目录索引完成: {total_files} 文件, {total_chunks} 代码块, "
            f"耗时 {elapsed:.1f}s"
        )
        return stats

    # ── 检索 ──────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 5,
        file_filter: Optional[str] = None,
        source_filter: Optional[str] = None,
        strategy: str = "hybrid",
    ) -> List[RetrievalResult]:
        """检索代码库。

        Args:
            query: 自然语言查询
            top_k: 返回结果数量
            file_filter: 文件路径过滤（如 '*.py'）
            source_filter: 索引源过滤（如 'weavemind' 只搜该项目的代码）
            strategy: 检索策略 semantic/keyword/hybrid

        Returns:
            检索结果列表，按分数降序
        """
        if strategy == "semantic":
            return self._semantic_search(query, top_k, file_filter, source_filter)
        elif strategy == "keyword":
            return self._keyword_search(query, top_k, file_filter, source_filter)
        else:
            return self._hybrid_search(query, top_k, file_filter, source_filter)

    def _semantic_search(
        self, query: str, top_k: int,
        file_filter: Optional[str] = None,
        source_filter: Optional[str] = None,
    ) -> List[RetrievalResult]:
        """纯向量语义检索。"""
        k = top_k * 3  # 多召回，后面去重
        kwargs = {"k": k}
        # Chroma metadata filter 支持 $and 组合条件
        chroma_filter = {}
        if file_filter:
            chroma_filter["file_path"] = file_filter
        if source_filter:
            chroma_filter["source"] = source_filter
        if chroma_filter:
            kwargs["filter"] = chroma_filter

        try:
            docs = self.vector_store.similarity_search_with_relevance_scores(
                query, **kwargs
            )
        except Exception as e:
            logger.warning(f"向量检索失败: {e}")
            return []

        results = []
        for doc, score in docs:
            meta = doc.metadata
            chunk = CodeChunk(
                file_path=meta.get("file_path", ""),
                chunk_type=meta.get("chunk_type", "block"),
                name=meta.get("name", ""),
                content=doc.page_content,
                start_line=meta.get("start_line", 0),
                end_line=meta.get("end_line", 0),
                parent_name=meta.get("parent_name"),
                signature=meta.get("signature"),
                language=meta.get("language", "python"),
                source=meta.get("source"),
            )
            results.append(
                RetrievalResult(
                    chunk=chunk,
                    score=max(score, 0.0),
                    semantic_score=score,
                    source="semantic",
                )
            )

        return results[:top_k]

    def _keyword_search(
        self, query: str, top_k: int,
        file_filter: Optional[str] = None,
        source_filter: Optional[str] = None,
    ) -> List[RetrievalResult]:
        """纯关键词检索。"""
        kw_results = self.keyword_index.search(
            query, top_k=top_k, file_filter=file_filter, source_filter=source_filter
        )
        results = []
        for chunk, score in kw_results:
            results.append(
                RetrievalResult(
                    chunk=chunk,
                    score=score,
                    keyword_score=score,
                    source="keyword",
                )
            )
        return results

    def _hybrid_search(
        self, query: str, top_k: int,
        file_filter: Optional[str] = None,
        source_filter: Optional[str] = None,
    ) -> List[RetrievalResult]:
        """混合检索：语义 + 关键词 + 评分融合 + 同文件去重。

        评分公式：
        final_score = semantic_score * 0.5
                    + keyword_score * 0.3
                    + type_boost * 0.1
                    + dual_hit_bonus * 0.1
        """
        # 1. 分别召回候选集
        semantic_results = self._semantic_search(query, top_k=top_k * 2, file_filter=file_filter, source_filter=source_filter)
        keyword_results = self._keyword_search(query, top_k=top_k * 2, file_filter=file_filter, source_filter=source_filter)

        # 2. 合并去重（以 file_path+name 为唯一键）
        merged: dict = {}  # key -> RetrievalResult

        for r in semantic_results:
            key = f"{r.chunk.file_path}::{r.chunk.name}::{r.chunk.start_line}"
            if key in merged:
                merged[key].keyword_score = max(merged[key].keyword_score, r.keyword_score)
                merged[key].semantic_score = max(merged[key].semantic_score, r.semantic_score)
            else:
                merged[key] = r

        for r in keyword_results:
            key = f"{r.chunk.file_path}::{r.chunk.name}::{r.chunk.start_line}"
            if key in merged:
                # 双重命中：语义+关键词都命中
                merged[key].keyword_score = max(merged[key].keyword_score, r.keyword_score)
                merged[key].semantic_score = max(merged[key].semantic_score, r.semantic_score)
                merged[key].source = "hybrid"
            else:
                merged[key] = r

        # 3. 计算混合分数
        for r in merged.values():
            type_boost = {
                "method": 0.08,
                "function": 0.08,
                "class": 0.05,
                "import": 0.02,
                "file": 0.0,
                "block": 0.0,
            }.get(r.chunk.chunk_type, 0.0)

            dual_hit_bonus = 0.1 if (r.semantic_score > 0.3 and r.keyword_score > 0.1) else 0.0

            r.score = (
                r.semantic_score * 0.5
                + r.keyword_score * 0.3
                + type_boost
                + dual_hit_bonus
            )

        # 4. 排序
        sorted_results = sorted(merged.values(), key=lambda x: -x.score)

        # 5. 同文件去重（每个文件最多 2 条）
        return self._deduplicate_by_file(sorted_results, max_per_file=2)[:top_k]

    @staticmethod
    def _deduplicate_by_file(
        results: List[RetrievalResult], max_per_file: int = 2
    ) -> List[RetrievalResult]:
        """同文件最多保留 max_per_file 条结果。"""
        file_counts: dict = {}
        deduped = []
        for r in results:
            fp = r.chunk.file_path
            count = file_counts.get(fp, 0)
            if count < max_per_file:
                deduped.append(r)
                file_counts[fp] = count + 1
        return deduped

    # ── 统计与元数据 ──────────────────────────────────────

    def get_stats(self) -> IndexStats:
        """返回索引统计信息。"""
        total_chunks = self.keyword_index.count()
        indexed_files = self.keyword_index.get_indexed_files()
        return IndexStats(
            total_files=len(indexed_files),
            total_chunks=total_chunks,
            last_updated=time.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def _file_hash(self, file_path: str) -> str:
        """计算文件 MD5 哈希，用于增量索引。"""
        h = hashlib.md5()
        try:
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
        except OSError:
            return ""
        return h.hexdigest()

    def _is_file_unchanged(self, file_path: str, file_hash: str) -> bool:
        """检查文件是否未变更。"""
        meta = self._indexed_files.get(file_path)
        if not meta:
            return False
        return meta.get("hash") == file_hash

    def _add_to_vector_store(self, chunks: List[CodeChunk]):
        """将代码块添加到 Chroma 向量存储。"""
        docs = []
        ids = []
        for chunk in chunks:
            # 跳过空内容（Qwen embedding API 不接受空字符串）
            if not chunk.content or not chunk.content.strip():
                continue
            doc_id = hashlib.md5(
                f"{chunk.file_path}::{chunk.name}::{chunk.start_line}".encode()
            ).hexdigest()[:12]
            doc = Document(
                page_content=chunk.content,
                metadata=chunk.to_metadata(),
            )
            docs.append(doc)
            ids.append(doc_id)

        if not docs:
            return

        # 先删除同文件的旧文档
        file_path = chunks[0].file_path if chunks else ""
        try:
            existing = self.vector_store.get(where={"file_path": file_path})
            if existing and existing["ids"]:
                self.vector_store.delete(ids=existing["ids"])
        except Exception:
            pass

        # 分批写入，DashScope text-embedding-v4 限制每批最多 10 个文档
        batch_size = min(settings.get("rag.embedding.batch_size", 10), 10)
        total_batches = (len(docs) + batch_size - 1) // batch_size
        for i in range(0, len(docs), batch_size):
            batch_docs = docs[i : i + batch_size]
            batch_ids = ids[i : i + batch_size]
            batch_num = i // batch_size + 1
            try:
                self.vector_store.add_documents(batch_docs, ids=batch_ids)
                logger.debug(f"向量写入批次 {batch_num}/{total_batches} 成功（{len(batch_docs)} 文档）")
            except Exception as e:
                logger.warning(
                    f"向量写入批次 {batch_num}/{total_batches} 失败（{len(batch_docs)} 文档）: {e}"
                )
                # 逐条重试，跳过有问题的文档
                for doc, doc_id in zip(batch_docs, batch_ids):
                    try:
                        self.vector_store.add_documents([doc], ids=[doc_id])
                    except Exception as e2:
                        logger.warning(f"跳过文档 {doc_id}: {e2}")

    def _load_metadata(self):
        """从磁盘加载索引元数据。"""
        if not os.path.exists(self._metadata_path):
            return
        try:
            with open(self._metadata_path, "r", encoding="utf-8") as f:
                self._indexed_files = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"索引元数据加载失败: {e}")
            self._indexed_files = {}

    def _save_metadata(self):
        """持久化索引元数据到磁盘。"""
        os.makedirs(os.path.dirname(self._metadata_path), exist_ok=True)
        with open(self._metadata_path, "w", encoding="utf-8") as f:
            json.dump(self._indexed_files, f, ensure_ascii=False, indent=2)
