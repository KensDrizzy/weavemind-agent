"""Checkpoint 基础设施 — LangGraph checkpointer 创建与运行中 thread 标记管理。

用于长任务中断恢复（进程崩溃 / 接口超时 / 长时间等待审批）：
- LangGraph 在每个 super-step 边界自动把图状态写入 checkpointer（SQLite）。
- 每轮运行使用唯一 thread_id 定位 checkpoint；运行期间写入 active 标记文件，
  正常结束清除，崩溃/异常时保留 → 下次启动可凭同一 thread_id 从最近 checkpoint 续跑。
"""

import json
import logging
import os
import sqlite3
import time
import uuid
from typing import Optional

import settings

logger = logging.getLogger(__name__)

DEFAULT_SQLITE_PATH = ".weavemind/checkpoints.sqlite3"
DEFAULT_ACTIVE_THREAD_PATH = ".weavemind/active_thread.json"


class CheckpointerProvider:
    """持有 LangGraph checkpointer，并管理"运行中 thread"标记文件。

    标记语义：
    - 一轮 graph 运行开始时 mark_active(thread_id)
    - 正常结束时 clear_active()
    - 崩溃/异常时标记保留 → 重启后 active_thread() 可读回用于 resume
    """

    def __init__(
        self,
        sqlite_path: Optional[str] = None,
        active_thread_path: Optional[str] = None,
    ):
        self.sqlite_path = sqlite_path or settings.get(
            "checkpoint.sqlite_path", DEFAULT_SQLITE_PATH
        )
        self.active_thread_path = active_thread_path or settings.get(
            "checkpoint.active_thread_path", DEFAULT_ACTIVE_THREAD_PATH
        )
        os.makedirs(os.path.dirname(self.sqlite_path) or ".", exist_ok=True)
        self.saver = self._create_saver()

    def _create_saver(self):
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
        except ImportError:
            from langgraph.checkpoint.memory import InMemorySaver
            logger.warning(
                "langgraph-checkpoint-sqlite 未安装，回退到内存 checkpoint"
                "（仅支持进程内恢复，重启后丢失）"
            )
            return InMemorySaver()

        conn = sqlite3.connect(self.sqlite_path, check_same_thread=False)
        saver = SqliteSaver(conn)
        logger.info("Checkpoint 持久化已启用: %s", self.sqlite_path)
        return saver

    # ── thread 管理 ──────────────────────────────────────────

    @staticmethod
    def new_thread_id() -> str:
        return uuid.uuid4().hex

    def mark_active(self, thread_id: str):
        """原子写入"运行中"标记。"""
        os.makedirs(os.path.dirname(self.active_thread_path) or ".", exist_ok=True)
        payload = {"thread_id": thread_id, "started_at": time.time()}
        tmp_path = self.active_thread_path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(payload, f)
        os.replace(tmp_path, self.active_thread_path)

    def clear_active(self):
        try:
            os.remove(self.active_thread_path)
        except FileNotFoundError:
            pass

    def active_thread(self) -> Optional[str]:
        """读取上次未完成运行的 thread_id；无标记或文件损坏返回 None。"""
        if not os.path.exists(self.active_thread_path):
            return None
        try:
            with open(self.active_thread_path) as f:
                return json.load(f).get("thread_id")
        except (json.JSONDecodeError, OSError):
            return None
