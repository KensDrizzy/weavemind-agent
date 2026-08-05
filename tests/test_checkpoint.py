"""Checkpoint 中断恢复与幂等测试。

覆盖三层机制：
1. LangGraph super-step 边界 checkpoint + thread_id 恢复（sqlite 真实落盘）
2. PlanExecutor 任务级执行记录（plan 节点内部重入不重复副作用）
3. Edit/Write 工具幂等
"""

import operator
from typing import Annotated, TypedDict

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from core.agent_loop import AgentLoop
from core.checkpoint import CheckpointerProvider
from core.plan_executor import PlanExecutor
from core.plan_models import Plan, Task, TaskStatus
from core.task_records import TaskRecordStore
from tools.builtin.edit import EditTool
from tools.builtin.write import WriteTool


def _make_provider(tmp_path):
    return CheckpointerProvider(
        sqlite_path=str(tmp_path / "ck.sqlite3"),
        active_thread_path=str(tmp_path / "active.json"),
    )


# ── CheckpointerProvider 标记生命周期 ─────────────────────────

class TestCheckpointerProvider:
    def test_mark_and_read_active_thread(self, tmp_path):
        provider = _make_provider(tmp_path)
        assert provider.active_thread() is None
        provider.mark_active("thread-1")
        assert provider.active_thread() == "thread-1"

    def test_clear_active(self, tmp_path):
        provider = _make_provider(tmp_path)
        provider.mark_active("thread-1")
        provider.clear_active()
        assert provider.active_thread() is None

    def test_corrupt_marker_returns_none(self, tmp_path):
        provider = _make_provider(tmp_path)
        with open(provider.active_thread_path, "w") as f:
            f.write("not-json")
        assert provider.active_thread() is None


# ── TaskRecordStore ───────────────────────────────────────────

class TestTaskRecordStore:
    def test_upsert_and_get(self, tmp_path):
        store = TaskRecordStore(db_path=str(tmp_path / "tr.sqlite3"))
        assert store.get("plan-1", "task-1") is None
        store.upsert("plan-1", "task-1", "completed", result="done")
        record = store.get("plan-1", "task-1")
        assert record["status"] == "completed"
        assert record["result"] == "done"

    def test_upsert_overwrites(self, tmp_path):
        store = TaskRecordStore(db_path=str(tmp_path / "tr.sqlite3"))
        store.upsert("plan-1", "task-1", "failed", error="boom")
        store.upsert("plan-1", "task-1", "completed", result="ok")
        assert store.get("plan-1", "task-1")["status"] == "completed"


# ── PlanExecutor 中断恢复（任务级幂等）────────────────────────

class _CountingTool:
    def __init__(self, name):
        self.name = name
        self.calls = 0

    def invoke(self, args):
        self.calls += 1
        return f"result-of-{self.name}"


class _FakeRegistry:
    def __init__(self, tools):
        self._tools = {t.name: t for t in tools}

    def get(self, name):
        return self._tools.get(name)


class _FakePolicy:
    def is_allowed(self, tool_name, mode="default"):
        return True

    def needs_confirmation(self, tool_name, mode="default"):
        return False


class TestPlanExecutorRecovery:
    def test_completed_task_is_skipped_on_rerun(self, tmp_path):
        store = TaskRecordStore(db_path=str(tmp_path / "tr.sqlite3"))
        tool_a = _CountingTool("Write")
        tool_b = _CountingTool("Edit")
        executor = PlanExecutor(
            tool_registry=_FakeRegistry([tool_a, tool_b]),
            permission_policy=_FakePolicy(),
            hook_manager=None,
            task_store=store,
        )
        plan = Plan(
            id="plan-x",
            goal="test",
            tasks=[
                Task(id="task_1", description="t1", tool_name="Write",
                     tool_args={}, dependencies=[]),
                Task(id="task_2", description="t2", tool_name="Edit",
                     tool_args={}, dependencies=["task_1"]),
            ],
        )
        # 模拟上次中断前 task_1 已成功执行并落盘
        store.upsert("plan-x", "task_1", "completed", result="cached-result")

        result_plan = executor.execute(plan)

        assert tool_a.calls == 0  # 已完成任务不重复调用工具（副作用不重复）
        assert tool_b.calls == 1
        t1 = result_plan.get_task("task_1")
        assert t1.status == TaskStatus.COMPLETED
        assert t1.result == "cached-result"
        assert result_plan.get_task("task_2").status == TaskStatus.COMPLETED

    def test_success_is_recorded(self, tmp_path):
        store = TaskRecordStore(db_path=str(tmp_path / "tr.sqlite3"))
        tool = _CountingTool("Write")
        executor = PlanExecutor(
            tool_registry=_FakeRegistry([tool]),
            permission_policy=_FakePolicy(),
            hook_manager=None,
            task_store=store,
        )
        plan = Plan(
            id="plan-y",
            goal="test",
            tasks=[Task(id="task_1", description="t1", tool_name="Write",
                        tool_args={}, dependencies=[])],
        )
        executor.execute(plan)
        record = store.get("plan-y", "task_1")
        assert record["status"] == "completed"
        assert record["result"] == "result-of-Write"

    def test_without_store_behaves_as_before(self):
        tool = _CountingTool("Write")
        executor = PlanExecutor(
            tool_registry=_FakeRegistry([tool]),
            permission_policy=_FakePolicy(),
            hook_manager=None,
        )
        plan = Plan(goal="test", tasks=[
            Task(id="task_1", description="t1", tool_name="Write",
                 tool_args={}, dependencies=[]),
        ])
        executor.execute(plan)
        assert tool.calls == 1


# ── LangGraph sqlite checkpoint 崩溃恢复（机制层）─────────────

class TestLangGraphCrashResume:
    def test_resume_continues_from_last_superstep(self, tmp_path):
        from langgraph.graph import END, StateGraph

        class S(TypedDict):
            steps: Annotated[list, operator.add]

        executed = []
        fail = {"node_b": True}

        def node_a(state):
            executed.append("a")
            return {"steps": ["a"]}

        def node_b(state):
            executed.append("b")
            if fail["node_b"]:
                raise RuntimeError("node b crashed")
            return {"steps": ["b"]}

        builder = StateGraph(S)
        builder.add_node("a", node_a)
        builder.add_node("b", node_b)
        builder.set_entry_point("a")
        builder.add_edge("a", "b")
        builder.add_edge("b", END)

        provider = _make_provider(tmp_path)
        graph = builder.compile(checkpointer=provider.saver)
        config = {"configurable": {"thread_id": "t-1"}}

        with pytest.raises(RuntimeError):
            graph.invoke({"steps": []}, config=config)
        assert executed == ["a", "b"]

        # 修复故障后用同一 thread_id 恢复：已完成的 node a 不重跑
        fail["node_b"] = False
        result = graph.invoke(None, config=config)
        assert executed == ["a", "b", "b"]  # 只有崩溃节点被重试
        assert result["steps"] == ["a", "b"]


# ── AgentLoop checkpoint 生命周期 ─────────────────────────────

class _FakeGraph:
    def __init__(self, events=None, error=None):
        self._events = events or []
        self._error = error
        self.calls = []

    def stream(self, state, config=None):
        self.calls.append((state, config))
        for event in self._events:
            yield event
        if self._error:
            raise self._error


class TestAgentLoopCheckpointLifecycle:
    def _make_loop(self, provider, graph):
        loop = AgentLoop.__new__(AgentLoop)
        loop.graph = graph
        loop.checkpointer_provider = provider
        loop.cancellation_token = None
        return loop

    def test_successful_run_clears_marker(self, tmp_path):
        provider = _make_provider(tmp_path)
        graph = _FakeGraph(events=[{"think": {"messages": [AIMessage(content="hi")]}}])
        loop = self._make_loop(provider, graph)
        events = list(loop.stream("你好"))
        assert len(events) == 1
        assert provider.active_thread() is None
        _, config = graph.calls[0]
        assert config["configurable"]["thread_id"]

    def test_failed_run_keeps_marker(self, tmp_path):
        provider = _make_provider(tmp_path)
        graph = _FakeGraph(events=[{"think": {}}], error=RuntimeError("boom"))
        loop = self._make_loop(provider, graph)
        with pytest.raises(RuntimeError):
            list(loop.stream("你好"))
        assert provider.active_thread() is not None

    def test_resume_without_active_thread_raises(self, tmp_path):
        provider = _make_provider(tmp_path)
        loop = self._make_loop(provider, _FakeGraph())
        with pytest.raises(RuntimeError):
            list(loop.resume())

    def test_resume_consumes_and_clears(self, tmp_path):
        provider = _make_provider(tmp_path)
        provider.mark_active("thread-x")
        graph = _FakeGraph(events=[{"think": {"messages": [AIMessage(content="done")]}}])
        loop = self._make_loop(provider, graph)
        events = list(loop.resume("thread-x"))
        assert len(events) == 1
        assert graph.calls[0][0] is None  # 恢复时 graph 输入为 None
        assert graph.calls[0][1]["configurable"]["thread_id"] == "thread-x"
        assert provider.active_thread() is None

    def test_resume_requires_provider(self):
        loop = AgentLoop.__new__(AgentLoop)
        loop.checkpointer_provider = None
        loop.cancellation_token = None
        with pytest.raises(RuntimeError):
            list(loop.resume("t"))


# ── AgentLoop._act tool_call 级恢复 ───────────────────────────

class TestActToolCallRecovery:
    def _make_loop(self, store, tools, thread_id="thread-1"):
        loop = AgentLoop.__new__(AgentLoop)
        loop.task_store = store
        loop._current_thread_id = thread_id if store else None
        loop.cancellation_token = None
        loop.tool_registry = _FakeRegistry(tools)
        loop.permission_policy = _FakePolicy()
        loop.hook_manager = None
        loop.mcp_manager = None
        loop.mode = "default"
        loop._tool_unavailable_reasons = {}
        loop._disabled_tools = {}
        loop._tool_failure_counts = {}
        return loop

    @staticmethod
    def _state(*tool_calls):
        return {
            "messages": [
                HumanMessage(content="测试"),
                AIMessage(content="", tool_calls=list(tool_calls)),
            ],
            "plan": None,
        }

    def test_recorded_tool_call_is_replayed_not_executed(self, tmp_path):
        """中断恢复：已有记录的 tool_call 回放结果，不重复调用工具。"""
        store = TaskRecordStore(db_path=str(tmp_path / "tr.sqlite3"))
        store.upsert("act:thread-1", "tc-1", "completed", result="cached-output")

        tool_a = _CountingTool("Write")
        tool_b = _CountingTool("Edit")
        loop = self._make_loop(store, [tool_a, tool_b])

        state = self._state(
            {"name": "Write", "args": {}, "id": "tc-1"},
            {"name": "Edit", "args": {}, "id": "tc-2"},
        )
        result = loop._act(state)

        assert tool_a.calls == 0  # 已记录的 tool_call 不重复执行
        assert tool_b.calls == 1
        messages = result["messages"]
        assert messages[0].content == "cached-output"
        assert messages[0].tool_call_id == "tc-1"
        assert messages[1].tool_call_id == "tc-2"

    def test_tool_call_result_is_recorded(self, tmp_path):
        """执行后的结果写入记录，供崩溃后回放。"""
        store = TaskRecordStore(db_path=str(tmp_path / "tr.sqlite3"))
        tool = _CountingTool("Write")
        loop = self._make_loop(store, [tool])

        state = self._state({"name": "Write", "args": {}, "id": "tc-1"})
        loop._act(state)

        record = store.get("act:thread-1", "tc-1")
        assert record is not None
        assert record["status"] == "completed"
        assert record["result"] == "result-of-Write"

    def test_without_store_executes_normally(self):
        """未启用 checkpoint 时行为与改造前一致。"""
        tool = _CountingTool("Write")
        loop = self._make_loop(None, [tool])
        state = self._state({"name": "Write", "args": {}, "id": "tc-1"})
        result = loop._act(state)
        assert tool.calls == 1
        assert result["messages"][0].content == "result-of-Write"


# ── 工具幂等 ──────────────────────────────────────────────────

class TestToolIdempotency:
    def test_edit_reapply_is_success(self, tmp_path):
        path = str(tmp_path / "f.txt")
        WriteTool()._run(path=path, content="foo bar")
        EditTool()._run(path=path, old_string="foo", new_string="baz")
        # 中断恢复后重跑同一编辑：old_string 已不存在，但目标内容已就位
        result = EditTool()._run(path=path, old_string="foo", new_string="baz")
        assert "Error" not in result
        with open(path) as f:
            assert f.read() == "baz bar"

    def test_edit_genuine_not_found_still_errors(self, tmp_path):
        path = str(tmp_path / "f.txt")
        WriteTool()._run(path=path, content="foo bar")
        result = EditTool()._run(path=path, old_string="missing", new_string="zzz")
        assert "Error" in result

    def test_write_same_content_skips(self, tmp_path):
        path = str(tmp_path / "f.txt")
        WriteTool()._run(path=path, content="hello")
        result = WriteTool()._run(path=path, content="hello")
        assert "Written" in result
        with open(path) as f:
            assert f.read() == "hello"

    def test_write_different_content_overwrites(self, tmp_path):
        path = str(tmp_path / "f.txt")
        WriteTool()._run(path=path, content="hello")
        WriteTool()._run(path=path, content="world")
        with open(path) as f:
            assert f.read() == "world"
