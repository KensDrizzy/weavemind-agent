"""BatchDelegateTool — parallel sub-agent delegation with failure isolation."""

import concurrent.futures
import logging
import time
import uuid
from typing import Any, ClassVar

from agents.monitor import SubAgentMonitor, SubAgentStatus
from agents.subagent import SUBAGENT_BLOCKED_TOOLS, _settings_get, run_subagent
from pydantic import PrivateAttr
from tools.base import WeaveMindTool

logger = logging.getLogger(__name__)


class BatchDelegateTool(WeaveMindTool):
    """Launch multiple independent sub-agents in parallel."""

    name: str = "BatchDelegate"
    description: str = (
        "Launch multiple sub-agents in parallel for independent tasks. "
        "Args: tasks=[{goal, subagent_type}], max_parallel(optional), timeout(optional). "
        "Use only when tasks do not depend on each other."
    )

    DEFAULT_MAX_PARALLEL: ClassVar[int] = 3
    DEFAULT_CHILD_TIMEOUT: ClassVar[int] = 600
    DEFAULT_MAX_RESULTS_CHARS: ClassVar[int] = 8000
    DEFAULT_RESULT_CHARS_PER_TASK: ClassVar[int] = 2000

    agent_defs: dict = {}
    subagent_monitor: SubAgentMonitor | None = None
    blocked_tools: frozenset[str] = SUBAGENT_BLOCKED_TOOLS
    auto_approve: bool | None = None
    _tool_registry: Any = PrivateAttr(default=None)

    def _run(
        self,
        tasks: list[dict],
        max_parallel: int | None = None,
        timeout: int | None = None,
    ) -> str:
        """Run sub-agent tasks concurrently and summarize all outcomes."""
        if not isinstance(tasks, list) or not tasks:
            return "[BatchDelegate] tasks 必须是非空列表。"

        max_parallel = max_parallel or _settings_get(
            "delegation.max_concurrent_children",
            self.DEFAULT_MAX_PARALLEL,
        )
        timeout = timeout or _settings_get(
            "delegation.child_timeout_seconds",
            self.DEFAULT_CHILD_TIMEOUT,
        )
        max_parallel = max(1, min(int(max_parallel), len(tasks)))
        timeout = max(0.001, float(timeout))

        results: list[dict] = []
        errors: list[dict] = []
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_parallel,
            thread_name_prefix="subagent",
        )
        future_map = {}

        try:
            for index, task in enumerate(tasks):
                task_id = f"batch-{index + 1}-{uuid.uuid4().hex[:8]}"
                future = executor.submit(self._run_single_task, task, task_id)
                future_map[future] = {
                    "task": task,
                    "task_id": task_id,
                    "deadline": time.monotonic() + timeout,
                }
                if self.subagent_monitor:
                    self.subagent_monitor.attach_future(task_id, future)

            pending = set(future_map)
            while pending:
                done, pending = concurrent.futures.wait(
                    pending,
                    timeout=0.2,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )

                for future in done:
                    meta = future_map[future]
                    task = meta["task"]
                    task_label = _task_label(task)
                    try:
                        result = future.result()
                        results.append({
                            "task": task_label,
                            "status": "completed",
                            "summary": self._truncate(
                                result,
                                self.DEFAULT_RESULT_CHARS_PER_TASK,
                            ),
                        })
                    except Exception as exc:
                        logger.exception("BatchDelegate 子任务失败: %s", task_label)
                        errors.append({
                            "task": task_label,
                            "status": "error",
                            "detail": str(exc),
                        })

                now = time.monotonic()
                timed_out = [
                    future for future in pending
                    if now >= future_map[future]["deadline"]
                ]
                for future in timed_out:
                    meta = future_map[future]
                    task_label = _task_label(meta["task"])
                    pending.remove(future)
                    future.cancel()
                    if self.subagent_monitor:
                        self.subagent_monitor.interrupt(meta["task_id"])
                    errors.append({
                        "task": task_label,
                        "status": "timeout",
                        "detail": f"exceeded {timeout}s",
                    })
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        return self._summarize_results(results, errors)

    def _run_single_task(self, task: dict, subagent_id: str | None = None) -> str:
        """Run one child task through the same isolated path as SubAgentTool."""
        if not isinstance(task, dict):
            raise ValueError("task 必须是 dict")

        subagent_type = task.get("subagent_type", "default")
        prompt = task.get("prompt") or task.get("goal") or task.get("description") or ""
        if not prompt:
            raise ValueError("task 缺少 goal/prompt/description")

        monitor = self.subagent_monitor
        actual_id = subagent_id or f"{subagent_type}-{uuid.uuid4().hex[:8]}"
        if monitor:
            if monitor.is_paused:
                return "[拒绝] 子 Agent 委托已暂停，未创建新任务。"
            monitor.register(actual_id)
            monitor.heartbeat(actual_id, SubAgentStatus.THINKING)

        status = SubAgentStatus.COMPLETED
        try:
            return run_subagent(
                agent_defs=self.agent_defs,
                tool_registry=getattr(self, "_tool_registry", None),
                subagent_type=subagent_type,
                prompt=prompt,
                monitor=monitor,
                subagent_id=actual_id,
                blocked_tools=self.blocked_tools,
                auto_approve=self.auto_approve,
            )
        except Exception:
            status = SubAgentStatus.FAILED
            if monitor:
                monitor.heartbeat(actual_id, SubAgentStatus.FAILED)
            raise
        finally:
            if monitor:
                monitor.heartbeat(actual_id, status)
                monitor.unregister(actual_id)

    def _summarize_results(self, results: list[dict], errors: list[dict]) -> str:
        parts = []
        if results:
            parts.append(f"## 成功完成 ({len(results)} 个)")
            for result in results:
                parts.append(f"### {result['task']}\n{result['summary']}")

        if errors:
            parts.append(f"## 失败/超时 ({len(errors)} 个)")
            for error in errors:
                line = f"- {error['task']}: {error['status']}"
                if error.get("detail"):
                    line += f" ({error['detail']})"
                parts.append(line)

        if not parts:
            parts.append("## 无结果")

        max_chars = _settings_get(
            "delegation.max_result_chars",
            self.DEFAULT_MAX_RESULTS_CHARS,
        )
        return self._truncate("\n\n".join(parts), int(max_chars))

    @staticmethod
    def _truncate(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + f"\n\n[... 已截断，原文 {len(text)} 字符]"


def _task_label(task: dict) -> str:
    if not isinstance(task, dict):
        return str(task)
    return task.get("goal") or task.get("prompt") or task.get("description") or "未命名任务"
