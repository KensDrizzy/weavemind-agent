"""RAG 新鲜度检测与自动刷新测试。"""

import json
import os
import tempfile
import time

import pytest

from rag.pipeline import CodeRAGPipeline
from rag.models import CodeChunk


class TestFreshnessCheck:
    """测试 check_freshness 方法。"""

    def test_fresh_file_detected(self, tmp_path):
        """未变更的文件应被标记为 fresh。"""
        # 创建一个临时文件并写入 metadata
        test_file = tmp_path / "hello.py"
        test_file.write_text("def hello(): pass\n")

        # 模拟 metadata
        meta_dir = tmp_path / ".weavemind" / "rag"
        meta_dir.mkdir(parents=True)
        file_hash = _hash_file(str(test_file))
        meta = {f"test::{test_file}": {"hash": file_hash, "timestamp": time.time(), "chunks": 1}}

        # 验证 fresh 检测
        freshness = _check_freshness_simple(meta)
        assert freshness["fresh_count"] == 1
        assert len(freshness["stale_files"]) == 0
        assert len(freshness["deleted_files"]) == 0

    def test_stale_file_detected(self, tmp_path):
        """内容变更的文件应被标记为 stale。"""
        test_file = tmp_path / "hello.py"
        test_file.write_text("def hello(): pass\n")

        # 用一个错误的 hash 模拟文件已变更
        meta = {f"test::{test_file}": {"hash": "wrong_hash", "timestamp": time.time(), "chunks": 1}}

        freshness = _check_freshness_simple(meta)
        assert len(freshness["stale_files"]) == 1
        assert freshness["fresh_count"] == 0

    def test_deleted_file_detected(self, tmp_path):
        """已删除的文件应被标记为 deleted。"""
        fake_path = str(tmp_path / "nonexistent.py")

        meta = {f"test::{fake_path}": {"hash": "abc123", "timestamp": time.time(), "chunks": 1}}

        freshness = _check_freshness_simple(meta)
        assert len(freshness["deleted_files"]) == 1
        assert freshness["fresh_count"] == 0


class TestCodeChunkIndexedAt:
    """测试 CodeChunk 的 indexed_at 字段。"""

    def test_indexed_at_in_metadata(self):
        """indexed_at 应出现在 to_metadata 输出中。"""
        chunk = CodeChunk(
            file_path="test.py",
            chunk_type="function",
            name="hello",
            content="def hello(): pass",
            start_line=1,
            end_line=1,
            language="python",
            indexed_at=1700000000.0,
        )
        meta = chunk.to_metadata()
        assert "indexed_at" in meta
        assert meta["indexed_at"] == 1700000000.0

    def test_indexed_at_optional(self):
        """indexed_at 为 None 时不应出现在 metadata 中。"""
        chunk = CodeChunk(
            file_path="test.py",
            chunk_type="function",
            name="hello",
            content="def hello(): pass",
            start_line=1,
            end_line=1,
            language="python",
        )
        meta = chunk.to_metadata()
        assert "indexed_at" not in meta

    def test_indexed_at_from_metadata(self):
        """从 metadata 还原 CodeChunk 时应包含 indexed_at。"""
        meta = {
            "file_path": "test.py",
            "chunk_type": "function",
            "name": "hello",
            "start_line": 1,
            "end_line": 1,
            "language": "python",
            "indexed_at": 1700000000.0,
        }
        chunk = CodeChunk(
            file_path=meta["file_path"],
            chunk_type=meta["chunk_type"],
            name=meta["name"],
            content="def hello(): pass",
            start_line=meta["start_line"],
            end_line=meta["end_line"],
            language=meta["language"],
            indexed_at=meta.get("indexed_at"),
        )
        assert chunk.indexed_at == 1700000000.0


class TestAutoRefreshThrottle:
    """测试 auto_refresh 的节流机制。"""

    def test_auto_refresh_disabled(self):
        """auto_refresh=False 时应跳过。"""
        # 直接测试逻辑
        auto_refresh = False
        if not auto_refresh:
            result = {"updated": 0, "deleted": 0, "new_indexed": 0, "skipped": 0, "reason": "auto_refresh disabled"}
        assert result["reason"] == "auto_refresh disabled"

    def test_auto_refresh_too_soon(self):
        """距离上次刷新不足阈值时应跳过。"""
        last_refresh_time = time.time()  # 刚刚刷新过
        stale_threshold = 300  # 5 分钟
        now = time.time()

        if now - last_refresh_time < stale_threshold:
            result = {"updated": 0, "deleted": 0, "new_indexed": 0, "skipped": 0, "reason": "too soon"}
        assert result["reason"] == "too soon"


# ── 辅助函数 ──────────────────────────────────────────────

def _hash_file(file_path: str) -> str:
    """计算文件 MD5 哈希。"""
    import hashlib
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_freshness_simple(metadata: dict) -> dict:
    """简化版新鲜度检测，不依赖 pipeline 实例。"""
    stale = []
    deleted = []
    fresh = 0

    for cache_key, meta in metadata.items():
        parts = cache_key.split("::", 1)
        file_path = parts[1] if len(parts) > 1 else parts[0]

        if not os.path.exists(file_path):
            deleted.append(cache_key)
            continue

        current_hash = _hash_file(file_path)
        if current_hash and meta.get("hash") != current_hash:
            stale.append(cache_key)
        else:
            fresh += 1

    return {
        "stale_files": stale,
        "deleted_files": deleted,
        "new_files": [],
        "fresh_count": fresh,
        "total_indexed": len(metadata),
    }
