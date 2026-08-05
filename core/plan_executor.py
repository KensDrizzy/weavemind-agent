"""DAG 执行引擎 — 按拓扑序执行 Plan 中的任务，支持并行和失败处理。

复用现有 ToolRegistry、PermissionPolicy、HookManager 基础设施。
"""

import asyncio
import inspect
import logging
from typing import Optional

from core.plan_models import Plan, Task, TaskStatus, PlanStatus
from tools.registry import ToolRegistry
from permissions.policy import PermissionPolicy
from hooks.manager import HookManager
from core.cancellation import AgentCancelledError, CancellationToken

logger = logging.getLogger(__name__)


class PlanExecutionError(Exception):
    """计划执行过程中的错误。"""
    pass


class PlanExecutor:
    """DAG 执行引擎。

    每轮取所有 PENDING 且依赖已满足的任务并行执行。
    单任务流程：权限检查 → PreToolUse Hook → 工具调用 → PostToolUse Hook → 状态更新。
    """

    def __init__(
        self,
        tool_registry: ToolRegistry,
        permission_policy: PermissionPolicy,
        hook_manager: Optional[HookManager] = None,
        max_parallel: int = 4,
        cancellation_token: Optional[CancellationToken] = None,
        task_store=None,
    ):
        self.tool_registry = tool_registry
        self.permission_policy = permission_policy
        self.hook_manager = hook_manager
        self.max_parallel = max_parallel
        self.cancellation_token = cancellation_token
        self.task_store = task_store
        # 兼容规划器生成的常见参数别名（如 file_path -> path）
        self.arg_aliases = {
            "path": {"file_path", "filepath", "file", "target_path"},
            "content": {"text", "body", "file_content"},
            "old_string": {"old", "old_text", "before"},
            "new_string": {"new", "new_text", "after"},
            "command": {"cmd", "shell", "script"},
            "url": {"link", "uri"},
            "query": {"keyword"},
            "root": {"dir", "directory"},
        }

    def execute(self, plan: Plan) -> Plan:
        """同步执行计划（内部使用 asyncio 实现并行）。"""
        self._check_cancelled()
        plan.status = PlanStatus.RUNNING
        logger.info(f"开始执行计划 {plan.id}: {plan.goal}")

        try:
            asyncio.run(self._execute_plan_async(plan))
        except RuntimeError as e:
            if "Event loop is already running" in str(e):
                # 在已有事件循环中（如 Jupyter），使用 nest_asyncio 或回退到串行
                self._execute_plan_serial(plan)
            else:
                raise

        return plan

    async def _execute_plan_async(self, plan: Plan):
        """异步并行执行计划。"""
        while not plan.is_finished():
            self._check_cancelled()
            ready = plan.ready_tasks()
            if not ready:
                # 无就绪任务但计划未结束 → 存在不可达任务
                self._mark_unreachable(plan)
                break

            # 限制并行度
            batch = ready[:self.max_parallel]
            tasks_coros = [self._execute_task_async(t, plan) for t in batch]
            await asyncio.gather(*tasks_coros)

        self._finalize_plan(plan)

    def _execute_plan_serial(self, plan: Plan):
        """串行执行计划（回退方案）。"""
        while not plan.is_finished():
            self._check_cancelled()
            ready = plan.ready_tasks()
            if not ready:
                self._mark_unreachable(plan)
                break

            for task in ready:
                self._execute_task(task, plan)

        self._finalize_plan(plan)

    async def _execute_task_async(self, task: Task, plan: Plan):
        """异步执行单个任务。"""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._execute_task, task, plan)

    def _execute_task(self, task: Task, plan: Plan):
        """执行单个任务：权限检查 → Hook → 工具调用 → Hook → 更新状态。"""
        self._check_cancelled()
        task.mark_started()
        logger.info(f"执行任务 {task.id}: {task.description}")

        # 中断恢复重入：已有完成记录的任务直接回填结果，不重复执行工具。
        # 这是副作用幂等的兜底——LangGraph checkpoint 只到节点边界，
        # 节点内部已完成 task 的副作用靠这里避免重复。
        if self.task_store:
            record = self.task_store.get(plan.id, task.id)
            if record and record["status"] == TaskStatus.COMPLETED.value:
                logger.info(f"任务 {task.id} 已有完成记录，跳过重复执行（中断恢复）")
                task.mark_completed(result=record["result"] or "")
                return

        try:
            # 无工具名时，由 LLM 自由执行（后续迭代支持）
            if not task.tool_name:
                task.mark_completed(result="未指定工具，跳过执行")
                return

            # 权限检查
            allowed = self.permission_policy.is_allowed(task.tool_name)
            if not allowed:
                task.mark_failed(error=f"工具 {task.tool_name} 被权限策略拒绝")
                self._propagate_failure(task, plan)
                return

            tool = self.tool_registry.get(task.tool_name)
            if not tool:
                task.mark_failed(error=f"工具 {task.tool_name} 不存在")
                self._propagate_failure(task, plan)
                return

            # 构建工具输入
            tool_input = self._normalize_tool_args(tool, task.tool_args or {})

            # 检查必填参数是否齐全
            missing = self._check_required_args(tool, tool_input)
            if missing:
                task.mark_failed(
                    error=f"工具 {task.tool_name} 缺少必填参数: {', '.join(missing)}"
                )
                logger.error(
                    "任务 %s 参数不完整: tool=%s, missing=%s, got=%s",
                    task.id, task.tool_name, missing, tool_input,
                )
                return

            # PreToolUse Hook
            if self.hook_manager:
                self.hook_manager.emit("PreToolUse", {
                    "tool": task.tool_name,
                    "args": tool_input,
                    "task_id": task.id,
                })

            result = tool.invoke(tool_input)
            self._check_cancelled()

            # PostToolUse Hook
            if self.hook_manager:
                self.hook_manager.emit("PostToolUse", {
                    "tool": task.tool_name,
                    "args": tool_input,
                    "task_id": task.id,
                    "result": str(result)[:500],
                })

            task.mark_completed(result=str(result))
            if self.task_store:
                self.task_store.upsert(
                    plan.id, task.id, TaskStatus.COMPLETED.value, result=str(result)
                )
            logger.info(f"任务 {task.id} 完成")

        except AgentCancelledError:
            raise
        except Exception as e:
            logger.error(f"任务 {task.id} 执行失败: {e}")
            task.mark_failed(error=str(e))
            if self.task_store:
                self.task_store.upsert(
                    plan.id, task.id, TaskStatus.FAILED.value, error=str(e)
                )
            self._propagate_failure(task, plan)

    def _check_cancelled(self):
        token = getattr(self, "cancellation_token", None)
        if token:
            token.raise_if_cancelled()

    def _check_required_args(self, tool, tool_input: dict) -> list:
        """检查工具必填参数是否齐全，返回缺失的参数名列表。"""
        # @tool 装饰器创建的工具，参数定义在 input_schema 中
        schema = getattr(tool, "input_schema", None) or getattr(tool, "args_schema", None)
        if schema is None:
            return []

        if hasattr(schema, "model_fields"):
            # Pydantic v2
            required = [
                name for name, field in schema.model_fields.items()
                if field.is_required()
            ]
        elif hasattr(schema, "__fields__"):
            # Pydantic v1
            required = [
                name for name, field in schema.__fields__.items()
                if field.required
            ]
        else:
            return []

        return [name for name in required if name not in tool_input]

    def _normalize_tool_args(self, tool, tool_args: dict) -> dict:
        """按工具 _run 签名规范化参数，兼容常见别名并过滤无效参数。"""
        if not tool_args:
            return {}

        run_fn = getattr(tool, "_run", None)
        if run_fn is None:
            # 测试替身等无 _run 签名时，不做约束
            return dict(tool_args)

        try:
            sig = inspect.signature(run_fn)
        except (TypeError, ValueError):
            return dict(tool_args)

        expected = {name for name in sig.parameters.keys() if name != "self"}
        alias_to_canonical = {}
        for canonical, aliases in self.arg_aliases.items():
            for alias in aliases:
                alias_to_canonical[alias] = canonical

        normalized = {}
        for key, value in tool_args.items():
            if key in expected:
                normalized[key] = value
                continue

            # 兼容 Glob 等将 path 作为 root 的情况
            if key == "path" and "root" in expected and "path" not in expected and "root" not in normalized:
                normalized["root"] = value
                continue

            canonical = alias_to_canonical.get(key)
            if canonical and canonical in expected and canonical not in normalized:
                normalized[canonical] = value
                continue

            logger.debug(
                "任务参数被忽略: tool=%s, key=%s, expected=%s",
                getattr(tool, "name", "unknown"),
                key,
                sorted(expected),
            )

        return normalized

    def _propagate_failure(self, failed_task: Task, plan: Plan):
        """将失败传播到依赖链上的所有任务。"""
        failed_id = failed_task.id
        for task in plan.tasks:
            if task.status == TaskStatus.PENDING and failed_id in task.dependencies:
                task.mark_skipped(reason=f"依赖任务 {failed_id} 失败")
                # 递归传播
                self._propagate_failure(task, plan)

    def _mark_unreachable(self, plan: Plan):
        """标记所有不可达的 PENDING 任务为 SKIPPED。"""
        for task in plan.tasks:
            if task.status == TaskStatus.PENDING:
                task.mark_skipped(reason="不可达任务（可能依赖失败任务）")

    def _finalize_plan(self, plan: Plan):
        """根据任务执行结果确定计划最终状态。"""
        if plan.has_failure():
            plan.status = PlanStatus.FAILED
        elif all(t.status == TaskStatus.COMPLETED for t in plan.tasks):
            plan.status = PlanStatus.COMPLETED
        else:
            plan.status = PlanStatus.FAILED

        logger.info(f"计划 {plan.id} 执行结束: {plan.status.value}")
