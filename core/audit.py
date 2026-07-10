"""审计日志 — 记录图片加载等安全敏感事件。

日志以 JSONL 形式写入 `.weavemind/audit/audit-YYYY-MM-DD.jsonl`，
每天一个文件，按需自动创建目录。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import settings

logger = logging.getLogger(__name__)

DEFAULT_AUDIT_DIR = ".weavemind/audit"

_lock = threading.Lock()


def _audit_dir() -> Path:
    """返回审计日志目录，自动创建。"""
    path = Path(settings.get("audit.dir", DEFAULT_AUDIT_DIR)).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _today_file() -> Path:
    """返回当天的审计日志文件路径。"""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _audit_dir() / f"audit-{date_str}.jsonl"


def _is_enabled() -> bool:
    """检查审计是否启用。"""
    return bool(settings.get("audit.enabled", True))


def log_image_load(
    source_type: str,
    mime_type: str,
    size_bytes: int,
    path: str | None = None,
    success: bool = True,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """记录一次图片加载事件。

    Args:
        source_type: 图片来源，如 "path", "clipboard", "mcp" 等。
        mime_type: 图片 MIME 类型。
        size_bytes: 原始或 base64 解码后的大小（字节）。
        path: 本地路径（如有）。
        success: 是否成功。
        error: 失败原因（如失败）。
        extra: 其他自定义字段。
    """
    if not _is_enabled():
        return

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "image_load",
        "source_type": source_type,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "success": success,
    }
    if path is not None:
        record["path"] = str(path)
    if error is not None:
        record["error"] = error
    if extra:
        record["extra"] = extra

    try:
        with _lock:
            with _today_file().open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning(f"审计日志写入失败: {e}")


def list_logs() -> list[Path]:
    """列出所有审计日志文件（按文件名排序）。"""
    try:
        return sorted(_audit_dir().glob("audit-*.jsonl"))
    except OSError:
        return []


def read_log(log_file: Path | str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """读取审计日志，默认当天，limit 为 0 表示全部。"""
    path = Path(log_file) if log_file else _today_file()
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if limit > 0 and len(records) >= limit:
                    break
    except OSError as e:
        logger.warning(f"审计日志读取失败: {e}")

    return records
