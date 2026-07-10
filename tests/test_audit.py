"""审计日志模块测试。"""

import json
from datetime import date
from pathlib import Path

import pytest

from core import audit


class TestAuditLogging:
    def test_log_image_load_creates_jsonl(self, tmp_path, monkeypatch):
        """记录图片加载事件应生成 JSONL 文件。"""
        monkeypatch.setattr(audit, "_audit_dir", lambda: tmp_path)
        audit.log_image_load(
            source_type="path",
            mime_type="image/png",
            size_bytes=1234,
            path="/tmp/test.png",
            success=True,
            extra={"width": 100, "height": 100},
        )

        log_file = tmp_path / f"audit-{date.today().isoformat()}.jsonl"
        assert log_file.exists()
        records = [json.loads(line) for line in log_file.read_text().strip().split("\n")]
        assert len(records) == 1
        assert records[0]["event"] == "image_load"
        assert records[0]["source_type"] == "path"
        assert records[0]["mime_type"] == "image/png"
        assert records[0]["size_bytes"] == 1234
        assert records[0]["path"] == "/tmp/test.png"
        assert records[0]["success"] is True
        assert records[0]["extra"]["width"] == 100
        assert "timestamp" in records[0]

    def test_log_image_load_error(self, tmp_path, monkeypatch):
        """失败事件应记录 error 字段。"""
        monkeypatch.setattr(audit, "_audit_dir", lambda: tmp_path)
        audit.log_image_load(
            source_type="clipboard",
            mime_type="unknown",
            size_bytes=0,
            success=False,
            error="剪贴板中没有图片数据",
        )

        records = audit.read_log(tmp_path / f"audit-{date.today().isoformat()}.jsonl")
        assert len(records) == 1
        assert records[0]["success"] is False
        assert records[0]["error"] == "剪贴板中没有图片数据"
        assert "path" not in records[0]

    def test_log_disabled(self, tmp_path, monkeypatch):
        """审计禁用时不应写入文件。"""
        monkeypatch.setattr(audit, "_is_enabled", lambda: False)
        monkeypatch.setattr(audit, "_audit_dir", lambda: tmp_path)
        audit.log_image_load(
            source_type="path",
            mime_type="image/png",
            size_bytes=100,
            success=True,
        )

        log_file = tmp_path / f"audit-{date.today().isoformat()}.jsonl"
        assert not log_file.exists()

    def test_read_log_limit(self, tmp_path, monkeypatch):
        """read_log 的 limit 参数应生效。"""
        monkeypatch.setattr(audit, "_audit_dir", lambda: tmp_path)
        for i in range(5):
            audit.log_image_load(
                source_type="path",
                mime_type="image/png",
                size_bytes=i,
                success=True,
            )

        log_file = tmp_path / f"audit-{date.today().isoformat()}.jsonl"
        records = audit.read_log(log_file, limit=2)
        assert len(records) == 2
        assert records[0]["size_bytes"] == 0
        assert records[1]["size_bytes"] == 1

    def test_read_log_returns_empty_for_missing_file(self, tmp_path):
        """读取不存在的日志应返回空列表。"""
        records = audit.read_log(tmp_path / "not-exists.jsonl")
        assert records == []

    def test_list_logs_sorted(self, tmp_path, monkeypatch):
        """list_logs 应按文件名排序。"""
        monkeypatch.setattr(audit, "_audit_dir", lambda: tmp_path)
        (tmp_path / "audit-2026-07-08.jsonl").write_text("")
        (tmp_path / "audit-2026-07-09.jsonl").write_text("")
        (tmp_path / "audit-2026-07-10.jsonl").write_text("")

        logs = audit.list_logs()
        assert [p.name for p in logs] == [
            "audit-2026-07-08.jsonl",
            "audit-2026-07-09.jsonl",
            "audit-2026-07-10.jsonl",
        ]
