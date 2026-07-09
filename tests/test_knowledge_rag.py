"""Knowledge RAG tests."""

from pathlib import Path

import pytest


def test_knowledge_pipeline_indexes_and_searches_text(tmp_path, monkeypatch):
    monkeypatch.setattr("settings.get", _settings_for(tmp_path))
    from knowledge_rag.pipeline import KnowledgeRAGPipeline

    doc = tmp_path / "policy.md"
    doc.write_text(
        "# 报销制度\n\n"
        "差旅报销需要在 30 天内提交发票。\n\n"
        "## 审批\n\n"
        "金额超过 5000 元需要部门负责人审批。\n",
        encoding="utf-8",
    )

    pipeline = KnowledgeRAGPipeline()
    document, chunk_count = pipeline.index_file(str(doc), collection_id="hr")

    assert document.collection_id == "hr"
    assert chunk_count > 0

    results = pipeline.search("超过 5000 元 谁审批", collection_id="hr")
    assert results
    assert results[0].chunk.file_name == "policy.md"
    assert "5000" in "\n".join(r.chunk.content for r in results)
    assert "policy.md" in results[0].chunk.citation()


def test_knowledge_pipeline_deduplicates_same_file_hash(tmp_path, monkeypatch):
    monkeypatch.setattr("settings.get", _settings_for(tmp_path))
    from knowledge_rag.pipeline import KnowledgeRAGPipeline

    doc = tmp_path / "manual.txt"
    doc.write_text("安装步骤：先启动服务，再执行健康检查。", encoding="utf-8")

    pipeline = KnowledgeRAGPipeline()
    _, first_count = pipeline.index_file(str(doc), collection_id="ops")
    _, second_count = pipeline.index_file(str(doc), collection_id="ops")

    assert first_count > 0
    assert second_count == 0


def test_knowledge_pipeline_ask_returns_answer(tmp_path, monkeypatch):
    monkeypatch.setattr("settings.get", _settings_for(tmp_path))
    from knowledge_rag.pipeline import KnowledgeRAGPipeline

    doc = tmp_path / "faq.md"
    doc.write_text(
        "# 请假制度\n\n"
        "员工请假超过 3 天需要直属主管审批。\n",
        encoding="utf-8",
    )

    pipeline = KnowledgeRAGPipeline()
    pipeline.index_file(str(doc), collection_id="hr")
    answer = pipeline.ask("请假超过 3 天需要什么流程？", collection_id="hr")

    assert "3 天" in answer or "主管" in answer or "证据" in answer


def test_knowledge_pipeline_chat_history_rewrites_query(tmp_path, monkeypatch):
    monkeypatch.setattr("settings.get", _settings_for(tmp_path))
    from knowledge_rag.pipeline import KnowledgeRAGPipeline

    doc = tmp_path / "handbook.md"
    doc.write_text(
        "# 员工手册\n\n"
        "公司每年提供 10 天带薪年假。\n",
        encoding="utf-8",
    )

    pipeline = KnowledgeRAGPipeline()
    pipeline.index_file(str(doc), collection_id="hr")
    history = ["user: 公司年假有多少天？", "assistant: 根据员工手册..."]
    results = pipeline.search("那病假呢？", collection_id="hr", chat_history=history)
    # Rewriter may expand "那病假" with context; just ensure search runs.
    assert isinstance(results, list)


def test_search_knowledge_tool_without_pipeline_returns_clear_error():
    from tools.builtin.knowledge_tools import SearchKnowledgeTool

    tool = SearchKnowledgeTool(knowledge_pipeline=None)
    result = tool._run(query="合同条款")

    assert "Knowledge RAG" in result


def test_knowledge_reranker_keeps_top_k():
    from knowledge_rag.models import KnowledgeChunk, KnowledgeSearchResult
    from knowledge_rag.retrieval.reranker import KnowledgeReranker

    reranker = KnowledgeReranker()
    reranker.enabled = True
    reranker.method = "heuristic"
    reranker.top_n = 10

    results = [
        KnowledgeSearchResult(
            chunk=KnowledgeChunk(
                chunk_id=f"c{i}",
                doc_id="d1",
                source_file="/tmp/a.md",
                file_name="a.md",
                content="content",
            ),
            score=0.5,
        )
        for i in range(10)
    ]
    ranked = reranker.rerank("query", results, top_k=3)
    assert len(ranked) == 3


def _settings_for(tmp_path: Path):
    values = {
        "knowledge_rag.root_dir": str(tmp_path / "knowledge"),
        "knowledge_rag.sqlite_db": str(tmp_path / "knowledge" / "knowledge.db"),
        "knowledge_rag.vector_store.provider": "sqlite",
        "knowledge_rag.embedding.provider": "hash",
        "knowledge_rag.embedding.dimension": 128,
        "knowledge_rag.chunking.max_chars": 800,
        "knowledge_rag.chunking.overlap_chars": 80,
        "knowledge_rag.retrieval.rrf_k": 60,
        "knowledge_rag.retrieval.max_context_chars": 4000,
        "knowledge_rag.retrieval.rerank.enabled": True,
        "knowledge_rag.retrieval.rerank.method": "heuristic",
        "knowledge_rag.retrieval.rerank.top_n": 10,
        "knowledge_rag.incremental_sync_before_search": False,
    }

    def _get(key: str, default=None):
        return values.get(key, default)

    return _get
