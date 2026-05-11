"""Plan-and-Execute 数据模型。

将用户目标分解为 DAG 结构的原子任务，支持依赖管理和并行执行。
"""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field
import time
import uuid


class TaskStatus(str, Enum):
    """任务执行状态。"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanStatus(str, Enum):
    """计划整体状态。"""
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Task(BaseModel):
    """DAG 中的单个任务节点。"""
    id: str = Field(default_factory=lambda: f"task_{uuid.uuid4().hex[:8]}")
    description: str
    tool_name: Optional[str] = None
    tool_args: Optional[dict] = None
    dependencies: list[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def mark_started(self):
        self.status = TaskStatus.RUNNING
        self.started_at = time.time()

    def mark_completed(self, result: str):
        self.status = TaskStatus.COMPLETED
        self.result = result
        self.finished_at = time.time()

    def mark_failed(self, error: str):
        self.status = TaskStatus.FAILED
        self.error = error
        self.finished_at = time.time()

    def mark_skipped(self, reason: str = ""):
        self.status = TaskStatus.SKIPPED
        self.error = reason or "依赖任务失败"
        self.finished_at = time.time()


class Plan(BaseModel):
    """执行计划 — DAG 结构的任务集合。"""
    id: str = Field(default_factory=lambda: f"plan_{uuid.uuid4().hex[:8]}")
    goal: str
    tasks: list[Task] = Field(default_factory=list)
    status: PlanStatus = PlanStatus.CREATED

    def get_task(self, task_id: str) -> Optional[Task]:
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None

    @property
    def pending_tasks(self) -> list[Task]:
        return [t for t in self.tasks if t.status == TaskStatus.PENDING]

    @property
    def completed_ids(self) -> set[str]:
        return {t.id for t in self.tasks if t.status == TaskStatus.COMPLETED}

    def ready_tasks(self) -> list[Task]:
        """获取当前可执行的任务：PENDING 且所有依赖已完成。"""
        completed = self.completed_ids
        return [
            t for t in self.tasks
            if t.status == TaskStatus.PENDING
            and all(dep in completed for dep in t.dependencies)
        ]

    def is_finished(self) -> bool:
        """所有任务都不再 PENDING（完成、失败或跳过）。"""
        return all(t.status != TaskStatus.PENDING for t in self.tasks)

    def has_failure(self) -> bool:
        """是否有任务失败。"""
        return any(t.status == TaskStatus.FAILED for t in self.tasks)

    def summary(self) -> str:
        """生成计划摘要字符串，用于渲染。"""
        status_counts = {}
        for t in self.tasks:
            status_counts[t.status.value] = status_counts.get(t.status.value, 0) + 1
        parts = [f"{v} {k}" for k, v in sorted(status_counts.items())]
        return f"Plan[{self.id}] {self.goal} | {', '.join(parts)}"