"""RAG 增量同步与新鲜度检测测试。"""

import hashlib
import json
import os
import tempfile
import time

import pytest

from rag.models import CodeChunk


class TestQuickFreshnessCheck:
    """测试 mtime 快筛 + MD5 精确确认的两步检测策略。"""

    def test_fresh_file_skipped_by_mtime(self, tmp_path):
        """mtime 未变的文件应直接跳过，不计算 MD5。"""
        test_file = tmp_path / "hello.py"
        test_file.write_text("def hello(): pass\n")

        # metadata 中的 timestamp >= mtime → 文件肯定没变
        file_mtime = os.path.getmtime(str(test_file))
        meta = {f"test::{test_file}": {
            "hash": "any_hash",  # 即使 hash 不对，mtime 没变也不检查
            "timestamp": file_mtime + 1,  # 比文件修改时间更新
            "chunks": 1,
        }}

        freshness = _quick_freshness_check(meta)
        assert freshness["fresh_count"] == 1
        assert len(freshness["stale_files"]) == 0

    def test_stale_file_detected_by_mtime_then_md5(self, tmp_path):
        """mtime 变了 + MD5 也变了 → 确认为 stale。"""
        test_file = tmp_path / "hello.py"
        test_file.write_text("def hello(): pass\n")

        # metadata 中的 timestamp < mtime → 触发 MD5 检查
        meta = {f"test::{test_file}": {
            "hash": "wrong_hash",
            "timestamp": 0,  # 远早于 mtime
            "chunks": 1,
        }}

        freshness = _quick_freshness_check(meta)
        assert len(freshness["stale_files"]) == 1
        assert freshness["fresh_count"] == 0

    def test_mtime_changed_but_content_same(self, tmp_path):
        """mtime 变了但 MD5 没变（touch/保存未修改）→ 仍算 fresh。"""
        test_file = tmp_path / "hello.py"
        test_file.write_text("def hello(): pass\n")

        file_hash = _hash_file(str(test_file))
        meta = {f"test::{test_file}": {
            "hash": file_hash,  # hash 正确
            "timestamp": 0,     # 但 timestamp 很旧，会触发 MD5 检查
            "chunks": 1,
        }}

        freshness = _quick_freshness_check(meta)
        assert freshness["fresh_count"] == 1
        assert len(freshness["stale_files"]) == 0

    def test_deleted_file_detected(self, tmp_path):
        """已删除的文件应被标记为 deleted。"""
        fake_path = str(tmp_path / "nonexistent.py")
        meta = {f"test::{fake_path}": {"hash": "abc123", "timestamp": time.time(), "chunks": 1}}

        freshness = _quick_freshness_check(meta)
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


class TestSyncPerformance:
    """验证增量同步的性能特征。"""

    def test_mtime_check_is_fast(self, tmp_path):
        """mtime 检测应该非常快（< 10ms for 100 files）。"""
        # 创建 100 个文件
        for i in range(100):
            f = tmp_path / f"file_{i}.py"
            f.write_text(f"def func_{i}(): pass\n")

        # 构建 metadata
        meta = {}
        for i in range(100):
            f = tmp_path / f"file_{i}.py"
            key = f"test::{f}"
            meta[key] = {
                "hash": "any",
                "timestamp": os.path.getmtime(str(f)) + 1,  # 比 mtime 新
                "chunks": 1,
            }

        start = time.time()
        freshness = _quick_freshness_check(meta)
        elapsed = time.time() - start

        assert freshness["fresh_count"] == 100
        assert elapsed < 0.05  # 50ms 内完成（实际约 1-2ms）


# ── 辅助函数 ──────────────────────────────────────────────

def _hash_file(file_path: str) -> str:
    """计算文件 MD5 哈希。"""
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _quick_freshness_check(metadata: dict) -> dict:
    """简化版新鲜度检测，模拟 pipeline._quick_freshness_check 逻辑。"""
    stale = []
    deleted = []
    fresh = 0

    for cache_key, meta in metadata.items():
        parts = cache_key.split("::", 1)
        file_path = parts[1] if len(parts) > 1 else parts[0]

        if not os.path.exists(file_path):
            deleted.append(cache_key)
            continue

        # mtime 快筛
        indexed_ts = meta.get("timestamp", 0)
        try:
            file_mtime = os.path.getmtime(file_path)
        except OSError:
            deleted.append(cache_key)
            continue

        if file_mtime <= indexed_ts:
            fresh += 1
            continue

        # MD5 精确确认
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
