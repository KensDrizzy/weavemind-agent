"""代码 RAG 的无效文件过滤、长度保护与失败恢复回归测试。"""

from unittest.mock import Mock

import pytest
from langchain_core.documents import Document

from rag.chunkers import should_index_dir, should_index_file, split_oversized_chunks
from rag.keyword_index import KeywordIndex
from rag.models import CodeChunk
from rag.pipeline import CodeRAGPipeline


def make_chunk(content, **updates):
    values = dict(
        file_path="test.py", chunk_type="method", name="run",
        content=content, start_line=10, end_line=10 + content.count("\n"),
        parent_name="Worker", signature="def run(self):", source="project",
    )
    values.update(updates)
    return CodeChunk(**values)


@pytest.mark.parametrize("name", [".DS_Store", "._main.py", "Thumbs.db", "desktop.ini"])
def test_skip_system_metadata(tmp_path, name):
    file = tmp_path / name
    file.write_text("即使可以解码也不应该索引", encoding="utf-8")
    assert not should_index_file(str(file))


@pytest.mark.parametrize("data", [b"hello\x00binary", b"\xffinvalid", b"text\x01binary", b"a" * 9000 + b"\x00"])
def test_skip_binary_without_known_extension(tmp_path, data):
    file = tmp_path / "unknown"
    file.write_bytes(data)
    assert not should_index_file(str(file))


def test_valid_utf8_and_extensionless_source(tmp_path):
    file = tmp_path / "Dockerfile"
    file.write_text("FROM python\n# 中文与表情 🙂\n\tRUN echo ok\n", encoding="utf-8")
    assert should_index_file(str(file))
    assert not should_index_file(str(tmp_path))
    assert not should_index_file(str(tmp_path / "missing"))


def test_skip_scrape_cache_even_for_single_file(tmp_path):
    cache = tmp_path / ".firecrawl"
    cache.mkdir()
    file = cache / "page.md"
    file.write_text("抓取缓存", encoding="utf-8")
    assert not should_index_dir(str(cache))
    assert not should_index_file(str(file))


def test_small_structural_chunk_is_preserved():
    chunk = make_chunk("def run(self):\n    pass\n")
    assert split_oversized_chunks([chunk], chunk_size=10, chunk_overlap=2) == [chunk]
    assert split_oversized_chunks([make_chunk(" \n\t")]) == []


@pytest.mark.parametrize("content", ["x" * 26000, "代码🙂" * 7000, "重复行\n" * 4000])
def test_long_chunks_bounded_without_losing_content(content):
    original = make_chunk(content)
    chunks = split_oversized_chunks([original], chunk_size=500, chunk_overlap=0)
    assert len(chunks) > 1
    assert "".join(c.content for c in chunks) == content
    assert all(len(c.content.encode("utf-8")) <= 8000 for c in chunks)
    assert len({c.name for c in chunks}) == len(chunks)
    assert all(c.parent_name == "Worker" and c.chunk_type == "method" for c in chunks)
    assert all(c.signature == original.signature and c.source == "project" for c in chunks)
    offset = 0
    for chunk in chunks:
        assert chunk.start_line == 10 + content.count("\n", 0, offset)
        assert chunk.end_line == chunk.start_line + chunk.content.count("\n") - int(chunk.content.endswith("\n"))
        offset += len(chunk.content)


def test_overlap_and_small_byte_budget():
    content = "🙂" * 30
    chunks = split_oversized_chunks([make_chunk(content)], chunk_size=20, chunk_overlap=5, max_input_bytes=32)
    assert all(len(c.content.encode("utf-8")) <= 32 for c in chunks)
    assert chunks[0].content[-5:] == chunks[1].content[:5]
    assert chunks[-1].content.endswith("🙂")


@pytest.mark.parametrize("config", [dict(chunk_size=0), dict(chunk_overlap=500), dict(chunk_overlap=-1), dict(max_input_bytes=3)])
def test_bad_split_config_rejected(config):
    with pytest.raises(ValueError, match="分块配置无效"):
        split_oversized_chunks([make_chunk("text")], **config)


class MemoryVectors:
    """模拟 Chroma 的按 ID upsert 与可控失败，不调用外部接口。"""

    def __init__(self):
        self.docs = {}
        self.fail_text = None
        self.fail_batch = False
        self.calls = []
        self.deleted = []

    def get(self, where):
        return {"ids": [key for key, doc in self.docs.items() if doc.metadata["file_path"] == where["file_path"]]}

    def add_documents(self, docs, ids):
        self.calls.append(docs)
        if self.fail_batch and len(docs) > 1:
            raise RuntimeError("模拟批次失败")
        if self.fail_text and any(self.fail_text in doc.page_content for doc in docs):
            raise RuntimeError("模拟单文档失败")
        assert all(isinstance(doc.page_content, str) for doc in docs)
        assert all(len(doc.page_content.encode("utf-8")) <= 8000 for doc in docs)
        self.docs.update(zip(ids, docs))

    def delete(self, ids):
        self.deleted.extend(ids)
        for key in ids:
            self.docs.pop(key, None)


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr("settings.get", lambda key, default=None: default)
    pipeline = CodeRAGPipeline.__new__(CodeRAGPipeline)
    pipeline.vector_store = MemoryVectors()
    pipeline.keyword_index = KeywordIndex(str(tmp_path / "index" / "keywords.db"))
    pipeline._metadata_path = str(tmp_path / "index" / "metadata.json")
    pipeline._indexed_files = {}
    pipeline.search_cache = Mock()
    yield pipeline
    pipeline.keyword_index._conn.close()


def test_pipeline_skips_ds_store_without_embedding(pipeline, tmp_path):
    file = tmp_path / ".DS_Store"
    file.write_bytes(b"binary" * 5000)
    assert pipeline.index_file(str(file)) == 0
    assert pipeline.vector_store.calls == []
    assert pipeline.keyword_index.count() == 0


@pytest.mark.parametrize("name, content", [
    ("data.json", '{"data":"' + "长文本🙂" * 3000 + '"}'),
    ("module.py", "def long_func():\n" + "    value = '代码🙂'\n" * 1000),
])
def test_pipeline_bounds_fallback_and_ast_chunks(pipeline, tmp_path, name, content):
    file = tmp_path / name
    file.write_text(content, encoding="utf-8")
    count = pipeline.index_file(str(file), source="test")
    assert count > 1
    assert pipeline.keyword_index.count() == count == len(pipeline.vector_store.docs)
    assert pipeline._indexed_files[f"test::{file}"]["index_complete"] is True
    assert pipeline.index_file(str(file), source="test") == 0


def test_failed_index_retries_even_if_file_unchanged(pipeline, tmp_path):
    file = tmp_path / "module.py"
    file.write_text("def good(): pass\ndef bad(): pass\n")
    pipeline.vector_store.fail_text = "bad"
    key = f"test::{file}"
    with pytest.raises(RuntimeError, match="向量索引写入失败"):
        pipeline.index_file(str(file), source="test")
    assert pipeline._indexed_files[key]["index_complete"] is False
    assert pipeline.keyword_index.count() == 0
    assert key in pipeline._quick_freshness_check()["stale_files"]
    pipeline.vector_store.fail_text = None
    assert pipeline.index_file(str(file), source="test") == 2
    assert pipeline._indexed_files[key]["index_complete"] is True
    assert pipeline.keyword_index.count() == len(pipeline.vector_store.docs) == 2


def test_batch_failure_with_successful_individual_retry(pipeline, tmp_path):
    file = tmp_path / "module.py"
    file.write_text("def first(): pass\ndef second(): pass\n")
    pipeline.vector_store.fail_batch = True
    assert pipeline.index_file(str(file)) == 2
    assert pipeline._indexed_files[str(file)]["index_complete"] is True


def test_old_vectors_not_deleted_on_embedding_failure(pipeline, tmp_path):
    file = tmp_path / "module.py"
    file.write_text("def old(): pass\n")
    pipeline.index_file(str(file))
    old_ids = set(pipeline.vector_store.docs)
    file.write_text("def new(): pass\n")
    pipeline.vector_store.fail_text = "new"
    with pytest.raises(RuntimeError):
        pipeline.index_file(str(file))
    assert old_ids <= set(pipeline.vector_store.docs)
    assert pipeline.vector_store.deleted == []
    assert pipeline.keyword_index.search("old")
    pipeline.vector_store.fail_text = None
    assert pipeline.index_file(str(file)) == 1
    assert not old_ids & set(pipeline.vector_store.docs)
    assert not pipeline.keyword_index.search("old")


def test_legacy_metadata_rebuilt_without_file_change(pipeline, tmp_path):
    file = tmp_path / "module.py"
    file.write_text("def good(): pass\n")
    pipeline._indexed_files[str(file)] = {
        "hash": pipeline._file_hash(str(file)), "timestamp": file.stat().st_mtime + 1, "chunks": 1,
    }
    assert str(file) in pipeline._quick_freshness_check()["stale_files"]
    assert pipeline.index_file(str(file)) == 1
    assert pipeline._quick_freshness_check()["fresh_count"] == 1


def test_legacy_filtered_file_cleaned_by_sync(pipeline, tmp_path):
    file = tmp_path / ".DS_Store"
    file.write_text("旧索引垃圾")
    chunk = make_chunk("旧索引垃圾", file_path=str(file))
    pipeline.keyword_index.add_chunks([chunk])
    pipeline.vector_store.docs["legacy"] = Document(page_content=chunk.content, metadata=chunk.to_metadata())
    pipeline._indexed_files[str(file)] = {"hash": "legacy", "timestamp": file.stat().st_mtime + 1}
    assert pipeline.sync_before_search()["deleted"] == 1
    assert pipeline.keyword_index.count() == 0
    assert pipeline.vector_store.docs == {}
    assert pipeline._indexed_files == {}


def test_config_change_invalidates_index(pipeline, tmp_path, monkeypatch):
    file = tmp_path / "module.py"
    file.write_text("def good(): pass\n")
    pipeline.index_file(str(file))
    monkeypatch.setattr("settings.get", lambda key, default=None: 400 if key == "rag.chunk_size" else default)
    assert str(file) in pipeline._quick_freshness_check()["stale_files"]
    assert pipeline.index_file(str(file)) == 1


def test_empty_file_removes_old_index(pipeline, tmp_path):
    file = tmp_path / "module.py"
    file.write_text("def old(): pass\n")
    pipeline.index_file(str(file))
    file.write_text("")
    assert pipeline.index_file(str(file)) == 0
    assert pipeline.keyword_index.count() == 0
    assert pipeline.vector_store.docs == {}
    assert pipeline._indexed_files[str(file)]["index_complete"] is True


def test_keyword_failure_is_not_marked_complete(pipeline, tmp_path, monkeypatch):
    file = tmp_path / "module.py"
    file.write_text("def good(): pass\n")
    monkeypatch.setattr(pipeline.keyword_index, "add_chunks", Mock(side_effect=RuntimeError("模拟关键词失败")))
    with pytest.raises(RuntimeError, match="模拟关键词失败"):
        pipeline.index_file(str(file))
    assert pipeline._indexed_files[str(file)]["index_complete"] is False


def test_cleanup_failure_retains_metadata_for_retry(pipeline, tmp_path, monkeypatch):
    file = tmp_path / ".DS_Store"
    file.write_text("旧文件")
    pipeline._indexed_files[str(file)] = {"hash": "legacy", "timestamp": 0}
    monkeypatch.setattr(pipeline.vector_store, "get", Mock(side_effect=RuntimeError("模拟清理失败")))
    assert pipeline.sync_before_search()["deleted"] == 0
    assert str(file) in pipeline._indexed_files


def test_split_line_range_keeps_trailing_blank_lines():
    content = ("code\n\n\n" * 2000)
    chunks = split_oversized_chunks([make_chunk(content)], chunk_overlap=0)
    offset = 0
    for chunk in chunks:
        assert chunk.end_line == 10 + content.count("\n", 0, offset + len(chunk.content)) - int(chunk.content.endswith("\n"))
        offset += len(chunk.content)


def test_empty_file_cleanup_failure_requires_retry(pipeline, tmp_path, monkeypatch):
    file = tmp_path / "module.py"
    file.write_text("def old(): pass\n")
    pipeline.index_file(str(file))
    file.write_text("")
    monkeypatch.setattr(pipeline.vector_store, "get", Mock(side_effect=RuntimeError("模拟清理失败")))
    with pytest.raises(RuntimeError, match="模拟清理失败"):
        pipeline.index_file(str(file))
    assert pipeline._indexed_files[str(file)]["index_complete"] is False


def test_real_chroma_upsert_and_cleanup_with_local_embeddings(pipeline, tmp_path):
    """用本地假向量验证真实 Chroma 的重建与过期 ID 清理，不访问模型接口。"""
    from langchain_chroma import Chroma
    from langchain_core.embeddings import Embeddings

    class LocalEmbeddings(Embeddings):
        def embed_documents(self, texts):
            assert all(isinstance(text, str) and len(text.encode("utf-8")) <= 8000 for text in texts)
            return [[float(len(text)), 1.0, 0.5] for text in texts]

        def embed_query(self, text):
            return self.embed_documents([text])[0]

    pipeline.vector_store = Chroma(
        collection_name="index_safety_regression",
        persist_directory=str(tmp_path / "chroma"),
        embedding_function=LocalEmbeddings(),
    )
    file = tmp_path / "data.json"
    file.write_text('{"data":"' + "代码🙂" * 3000 + '"}', encoding="utf-8")
    count = pipeline.index_file(str(file))
    assert len(pipeline.vector_store.get()["ids"]) == pipeline.keyword_index.count() == count
    assert pipeline.index_file(str(file)) == 0
    file.write_text('{"data":"新的短内容"}', encoding="utf-8")
    assert pipeline.index_file(str(file)) == 1
    assert len(pipeline.vector_store.get()["ids"]) == pipeline.keyword_index.count() == 1
    file.write_text("")
    pipeline.index_file(str(file))
    assert pipeline.vector_store.get()["ids"] == []
    assert pipeline.keyword_index.count() == 0
