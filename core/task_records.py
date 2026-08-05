"""任务级执行记录 — Plan 内已完成任务的持久化记录，支撑中断后重入不重复执行。

背景：LangGraph 的 checkpoint 只打在节点（super-step）边界。`_execute_plan` 节点内部
会顺序/并行执行多个 Task，若节点执行中途崩溃，checkpoint 中 plan 仍是全部 PENDING，
恢复后整个节点重跑 → 已完成任务的工具副作用（写文件、调 API、发消息）会重复发生。

本模块用 SQLite 记录每个 (plan_id, task_id) 的执行结果。PlanExecutor 重跑时先查记录：
已完成的任务直接回填结果，不再调用工具。

幂等边界说明：任务状态从"工具执行成功"到"记录落盘"之间仍存在极小崩溃窗口，
该窗口内崩溃会导致该任务重复执行一次。这是 at-least-once 执行的固有边界，
对不可幂等工具（如 Bash）需要业务侧自行保证幂等键。
"""

import logging
import os
import sqlite3
import threading
import time
from typing import Optional

import settings

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = ".weavemind/task_records.sqlite3"


class TaskRecordStore:
    """(plan_id, task_id) 粒度的执行结果持久化。线程安全。"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or settings.get(
            "checkpoint.task_records_path", DEFAULT_DB_PATH
        )
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_records (
                    plan_id    TEXT NOT NULL,
                    task_id    TEXT NOT NULL,
                    status     TEXT NOT NULL,
                    result     TEXT,
                    error      TEXT,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (plan_id, task_id)
                )
                """
            )
            self._conn.commit()

    def get(self, plan_id: str, task_id: str) -> Optional[dict]:
        """读取任务执行记录；无记录返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT status, result, error FROM task_records"
                " WHERE plan_id = ? AND task_id = ?",
                (plan_id, task_id),
            ).fetchone()
        if row is None:
            return None
        return {"status": row[0], "result": row[1], "error": row[2]}

    def upsert(
        self,
        plan_id: str,
        task_id: str,
        status: str,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ):
        """写入/更新任务执行记录。"""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO task_records (plan_id, task_id, status, result, error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (plan_id, task_id) DO UPDATE SET
                    status = excluded.status,
                    result = excluded.result,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (plan_id, task_id, status, result, error, time.time()),
            )
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()
