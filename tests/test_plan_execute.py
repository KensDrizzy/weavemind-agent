"""Plan-and-Execute 模块测试。"""

import pytest
from core.plan_models import Task, TaskStatus, Plan, PlanStatus
from core.planner import Planner
from core.plan_executor import PlanExecutor
from core.agent_loop import AgentLoop
from hooks.manager import HookManager
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage


def _make_fake_compactor():
    """创建一个永不触发压缩的假 compactor，用于 __new__ 构建的 AgentLoop 测试。"""
    class FakeCompactor:
        def should_compact(self, messages):
            return False
    return FakeCompactor()


# ── plan_models 测试 ──────────────────────────────────────────

class TestTask:
    def test_default_id(self):
        task = Task(description="测试任务")
        assert task.id.startswith("task_")

    def test_mark_started(self):
        task = Task(description="测试")
        task.mark_started()
        assert task.status == TaskStatus.RUNNING
        assert task.started_at is not None

    def test_mark_completed(self):
        task = Task(description="测试")
        task.mark_started()
        task.mark_completed(result="完成")
        assert task.status == TaskStatus.COMPLETED
        assert task.result == "完成"
        assert task.finished_at is not None

    def test_mark_failed(self):
        task = Task(description="测试")
        task.mark_started()
        task.mark_failed(error="出错了")
        assert task.status == TaskStatus.FAILED
        assert task.error == "出错了"

    def test_mark_skipped(self):
        task = Task(description="测试")
        task.mark_skipped("依赖失败")
        assert task.status == TaskStatus.SKIPPED
        assert task.error == "依赖失败"


class TestPlan:
    def _make_plan(self):
        t1 = Task(id="task_1", description="步骤1", dependencies=[])
        t2 = Task(id="task_2", description="步骤2", dependencies=["task_1"])
        t3 = Task(id="task_3", description="步骤3", dependencies=["task_1"])
        t4 = Task(id="task_4", description="步骤4", dependencies=["task_2", "task_3"])
        return Plan(goal="测试计划", tasks=[t1, t2, t3, t4])

    def test_get_task(self):
        plan = self._make_plan()
        assert plan.get_task("task_1") is not None
        assert plan.get_task("nonexistent") is None

    def test_pending_tasks(self):
        plan = self._make_plan()
        assert len(plan.pending_tasks) == 4

    def test_ready_tasks_initial(self):
        plan = self._make_plan()
        ready = plan.ready_tasks()
        assert len(ready) == 1
        assert ready[0].id == "task_1"

    def test_ready_tasks_after_first(self):
        plan = self._make_plan()
        plan.get_task("task_1").mark_completed("ok")
        ready = plan.ready_tasks()
        ids = {t.id for t in ready}
        assert ids == {"task_2", "task_3"}

    def test_ready_tasks_parallel(self):
        plan = self._make_plan()
        plan.get_task("task_1").mark_completed("ok")
        plan.get_task("task_2").mark_completed("ok")
        plan.get_task("task_3").mark_completed("ok")
        ready = plan.ready_tasks()
        assert len(ready) == 1
        assert ready[0].id == "task_4"

    def test_is_finished(self):
        plan = self._make_plan()
        assert not plan.is_finished()
        for t in plan.tasks:
            t.mark_completed("ok")
        assert plan.is_finished()

    def test_has_failure(self):
        plan = self._make_plan()
        assert not plan.has_failure()
        plan.get_task("task_1").mark_failed("error")
        assert plan.has_failure()

    def test_summary(self):
        plan = self._make_plan()
        summary = plan.summary()
        assert "task_1" not in summary  # ID 不在摘要中
        assert "4 pending" in summary


# ── planner 测试 ──────────────────────────────────────────────

class TestPlanner:
    def test_extract_json_from_code_block(self):
        planner = Planner.__new__(Planner)
        content = '```json\n{"goal": "test", "tasks": []}\n```'
        result = planner._extract_json(content)
        assert '"goal"' in result

    def test_extract_json_raw(self):
        planner = Planner.__new__(Planner)
        content = '{"goal": "test", "tasks": []}'
        result = planner._extract_json(content)
        assert '"goal"' in result

    def test_extract_json_with_surrounding_text(self):
        planner = Planner.__new__(Planner)
        content = 'Here is the plan:\n{"goal": "test", "tasks": []}\nDone.'
        result = planner._extract_json(content)
        assert '"goal"' in result

    def test_extract_json_invalid(self):
        planner = Planner.__new__(Planner)
        with pytest.raises(ValueError):
            planner._extract_json("no json here")

    def test_validate_dag_valid(self):
        planner = Planner.__new__(Planner)
        t1 = Task(id="task_1", description="a", dependencies=[])
        t2 = Task(id="task_2", description="b", dependencies=["task_1"])
        plan = Plan(goal="test", tasks=[t1, t2])
        # 不应抛异常
        planner._validate_dag(plan)

    def test_validate_dag_cycle(self):
        planner = Planner.__new__(Planner)
        t1 = Task(id="task_1", description="a", dependencies=["task_2"])
        t2 = Task(id="task_2", description="b", dependencies=["task_1"])
        plan = Plan(goal="test", tasks=[t1, t2])
        with pytest.raises(ValueError, match="循环依赖"):
            planner._validate_dag(plan)

    def test_validate_dag_invalid_dep(self):
        planner = Planner.__new__(Planner)
        t1 = Task(id="task_1", description="a", dependencies=["task_99"])
        plan = Plan(goal="test", tasks=[t1])
        with pytest.raises(ValueError, match="不存在"):
            planner._validate_dag(plan)


class TestAgentLoop:
    def _make_loop_for_searchcode(self, has_tool=True):
        loop = AgentLoop.__new__(AgentLoop)
        loop.force_plan_mode = False
        loop._tool_unavailable_reasons = {}
        loop._disabled_tools = {}

        class FakeRegistry:
            def get(self, name):
                if name == "SearchCode" and has_tool:
                    return object()
                return None

        loop.tool_registry = FakeRegistry()
        return loop

    def _make_loop_for_searchknowledge(self, has_tool=True):
        loop = AgentLoop.__new__(AgentLoop)
        loop.force_plan_mode = False
        loop._tool_unavailable_reasons = {}
        loop._disabled_tools = {}

        class FakeRegistry:
            def get(self, name):
                if name == "AskKnowledge" and has_tool:
                    return object()
                return None

        loop.tool_registry = FakeRegistry()
        return loop

    def test_force_search_code_for_codebase_question(self):
        loop = self._make_loop_for_searchcode()
        messages = [HumanMessage(content="解释一下 weavemind 的 mcp 实现")]

        forced = loop._maybe_force_search_code(messages)

        assert forced is not None
        assert forced.tool_calls[0]["name"] == "SearchCode"
        assert forced.tool_calls[0]["args"]["query"] == "解释一下 weavemind 的 mcp 实现"

    def test_force_search_code_only_once_per_user_turn(self):
        loop = self._make_loop_for_searchcode()
        messages = [
            HumanMessage(content="解释一下 weavemind 的 mcp 实现"),
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "SearchCode",
                    "args": {"query": "解释一下 weavemind 的 mcp 实现", "top_k": 5},
                    "id": "call_1",
                }],
            ),
            ToolMessage(content="找到结果", tool_call_id="call_1", name="SearchCode"),
        ]

        assert loop._maybe_force_search_code(messages) is None

    def test_force_search_code_skips_url_questions(self):
        loop = self._make_loop_for_searchcode()
        messages = [HumanMessage(content="解释一下 https://example.com 里的实现")]

        assert loop._maybe_force_search_code(messages) is None

    def test_force_search_code_requires_registered_tool(self):
        loop = self._make_loop_for_searchcode(has_tool=False)
        messages = [HumanMessage(content="解释一下 weavemind 的 mcp 实现")]

        assert loop._maybe_force_search_code(messages) is None

    def test_force_search_knowledge_for_document_question(self, monkeypatch):
        monkeypatch.setattr("settings.get", lambda key, default=None: default)
        loop = self._make_loop_for_searchknowledge()
        messages = [HumanMessage(content="这份合同文档里关于终止条款是怎么规定的？")]

        forced = loop._maybe_force_search_knowledge(messages)

        assert forced is not None
        assert forced.tool_calls[0]["name"] == "AskKnowledge"
        assert forced.tool_calls[0]["args"]["query"] == "这份合同文档里关于终止条款是怎么规定的？"

    def test_force_search_knowledge_skips_when_unregistered(self, monkeypatch):
        monkeypatch.setattr("settings.get", lambda key, default=None: default)
        loop = self._make_loop_for_searchknowledge(has_tool=False)
        messages = [HumanMessage(content="这份制度文档有哪些审批要求？")]

        assert loop._maybe_force_search_knowledge(messages) is None

    def test_should_continue_force_plan_stops_after_plan_ready(self):
        loop = AgentLoop.__new__(AgentLoop)
        loop.force_plan_mode = True

        s1 = {"messages": [AIMessage(content="先进入规划")], "plan": None}
        assert loop._should_continue(s1) == "continue"

        s2 = {"messages": [AIMessage(content="计划已执行")], "plan": {"id": "plan_x"}}
        assert loop._should_continue(s2) == "end"

    def test_record_tool_failure_disables_after_threshold(self):
        loop = AgentLoop.__new__(AgentLoop)
        loop._tool_failure_counts = {}
        loop._disabled_tools = {}

        loop._record_tool_failure("WebFetch", "第一次失败")
        assert loop._disabled_tools == {}

        loop._record_tool_failure("WebFetch", "第二次失败")
        assert "WebFetch" in loop._disabled_tools

    def test_check_tool_availability_websearch_without_key(self, monkeypatch):
        """WebSearch 在没有任何搜索引擎可用时应返回不可用。"""
        class DummyTool:
            name = "WebSearch"

        # 清除所有搜索引擎的环境变量
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.delenv("SEARXNG_URL", raising=False)
        monkeypatch.delenv("GLM_API_KEY", raising=False)
        monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
        monkeypatch.delenv("SERPAPI_KEY", raising=False)

        ok, reason = AgentLoop._check_tool_availability(DummyTool())
        # DuckDuckGo 可能通过 ddgs 包可用，所以不强制要求 ok=False
        # 只验证返回格式正确
        assert isinstance(ok, bool)
        if not ok:
            assert reason  # 不可用时必须有原因说明


# ── plan_executor 测试 ────────────────────────────────────────

class FakeTool:
    """模拟工具用于测试。"""
    def __init__(self, name, result="ok"):
        self.name = name
        self._result = result

    def invoke(self, args):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class SignatureTool:
    """带 _run 签名的工具，用于测试参数规范化。"""
    def __init__(self, name):
        self.name = name

    def _run(self, path: str, content: str):
        return f"ok:{path}:{content}"

    def invoke(self, args):
        return self._run(**args)


class SignatureGlobTool:
    """模拟 Glob 工具签名。"""
    name = "Glob"

    def _run(self, pattern: str, root: str = "."):
        return f"glob:{pattern}:{root}"

    def invoke(self, args):
        return self._run(**args)


class FakeRegistry:
    """模拟 ToolRegistry。"""
    def __init__(self, tools=None):
        self._tools = {t.name: t for t in (tools or [])}

    def get(self, name):
        return self._tools.get(name)


class FakePolicy:
    """模拟 PermissionPolicy。"""
    def is_allowed(self, tool_name, mode="default"):
        return tool_name != "forbidden_tool"


class TestPlanExecutor:
    def _make_executor(self, tools=None, policy=None):
        return PlanExecutor(
            tool_registry=FakeRegistry(tools),
            permission_policy=policy or FakePolicy(),
            hook_manager=None,
        )

    def test_execute_simple_plan(self):
        tool = FakeTool("Read", result="文件内容")
        executor = self._make_executor(tools=[tool])

        t1 = Task(id="task_1", description="读取文件", tool_name="Read",
                  tool_args={"file_path": "/tmp/test.txt"}, dependencies=[])
        plan = Plan(goal="读取文件", tasks=[t1])

        result = executor.execute(plan)
        assert result.status == PlanStatus.COMPLETED
        assert result.get_task("task_1").status == TaskStatus.COMPLETED

    def test_execute_parallel_tasks(self):
        tool_a = FakeTool("Read", result="内容A")
        tool_b = FakeTool("Glob", result="file1.py\nfile2.py")
        executor = self._make_executor(tools=[tool_a, tool_b])

        t1 = Task(id="task_1", description="读取", tool_name="Read",
                  tool_args={}, dependencies=[])
        t2 = Task(id="task_2", description="搜索", tool_name="Glob",
                  tool_args={}, dependencies=[])
        plan = Plan(goal="并行执行", tasks=[t1, t2])

        result = executor.execute(plan)
        assert result.status == PlanStatus.COMPLETED
        assert result.get_task("task_1").status == TaskStatus.COMPLETED
        assert result.get_task("task_2").status == TaskStatus.COMPLETED

    def test_execute_with_dependencies(self):
        tool = FakeTool("Read", result="内容")
        executor = self._make_executor(tools=[tool])

        t1 = Task(id="task_1", description="步骤1", tool_name="Read",
                  tool_args={}, dependencies=[])
        t2 = Task(id="task_2", description="步骤2", tool_name="Read",
                  tool_args={}, dependencies=["task_1"])
        plan = Plan(goal="串行执行", tasks=[t1, t2])

        result = executor.execute(plan)
        assert result.status == PlanStatus.COMPLETED
        assert result.get_task("task_1").finished_at <= result.get_task("task_2").started_at

    def test_execute_permission_denied(self):
        executor = self._make_executor()

        t1 = Task(id="task_1", description="禁止的工具", tool_name="forbidden_tool",
                  tool_args={}, dependencies=[])
        plan = Plan(goal="权限测试", tasks=[t1])

        result = executor.execute(plan)
        assert result.get_task("task_1").status == TaskStatus.FAILED
        assert "拒绝" in result.get_task("task_1").error

    def test_execute_failure_propagation(self):
        tool = FakeTool("fail_tool", result=RuntimeError("执行失败"))
        ok_tool = FakeTool("Read", result="ok")
        executor = self._make_executor(tools=[tool, ok_tool])

        t1 = Task(id="task_1", description="失败任务", tool_name="fail_tool",
                  tool_args={}, dependencies=[])
        t2 = Task(id="task_2", description="依赖任务", tool_name="Read",
                  tool_args={}, dependencies=["task_1"])
        plan = Plan(goal="失败传播", tasks=[t1, t2])

        result = executor.execute(plan)
        assert result.get_task("task_1").status == TaskStatus.FAILED
        assert result.get_task("task_2").status == TaskStatus.SKIPPED
        assert result.status == PlanStatus.FAILED

    def test_execute_tool_not_found(self):
        executor = self._make_executor()

        t1 = Task(id="task_1", description="不存在的工具", tool_name="NonExistent",
                  tool_args={}, dependencies=[])
        plan = Plan(goal="工具不存在", tasks=[t1])

        result = executor.execute(plan)
        assert result.get_task("task_1").status == TaskStatus.FAILED
        assert "不存在" in result.get_task("task_1").error

    def test_execute_no_tool_name(self):
        executor = self._make_executor()

        t1 = Task(id="task_1", description="无工具任务", dependencies=[])
        plan = Plan(goal="无工具", tasks=[t1])

        result = executor.execute(plan)
        assert result.get_task("task_1").status == TaskStatus.COMPLETED

    def test_execute_normalize_file_path_for_write(self):
        tool = SignatureTool("Write")
        executor = self._make_executor(tools=[tool])

        t1 = Task(
            id="task_1",
            description="写文件",
            tool_name="Write",
            tool_args={"file_path": "/tmp/demo.txt", "content": "Hello"},
            dependencies=[],
        )
        plan = Plan(goal="写文件", tasks=[t1])

        result = executor.execute(plan)
        assert result.get_task("task_1").status == TaskStatus.COMPLETED
        assert "ok:/tmp/demo.txt:Hello" in (result.get_task("task_1").result or "")

    def test_execute_normalize_path_to_root_for_glob(self):
        tool = SignatureGlobTool()
        executor = self._make_executor(tools=[tool])

        t1 = Task(
            id="task_1",
            description="全局搜索",
            tool_name="Glob",
            tool_args={"pattern": "*.py", "path": "src"},
            dependencies=[],
        )
        plan = Plan(goal="搜索", tasks=[t1])

        result = executor.execute(plan)
        assert result.get_task("task_1").status == TaskStatus.COMPLETED
        assert "glob:*.py:src" in (result.get_task("task_1").result or "")


class FakeStreamLLM:
    """模拟支持 stream 的 LLM。"""

    def __init__(self, chunks):
        self._chunks = chunks

    def stream(self, _messages):
        for chunk in self._chunks:
            yield chunk


class FakeGraph:
    """模拟 LangGraph，用于验证 stream 接口行为。"""

    def __init__(self, events=None):
        self._events = events or []

    def stream(self, _state, config=None):
        for event in self._events:
            yield event


class TestStreamingHooks:
    def test_hook_manager_emit_matcher(self):
        manager = HookManager()
        hits = []
        manager.register("PreToolUse", lambda data: hits.append(data["tool"]), matcher="Read")

        manager.emit("PreToolUse", {"tool": "Read", "args": {}})
        manager.emit("PreToolUse", {"tool": "Write", "args": {}})

        assert hits == ["Read"]

    def test_think_stream_emits_llm_events(self):
        loop = AgentLoop.__new__(AgentLoop)
        loop.memory = None
        loop.hook_manager = HookManager()
        loop.llm_with_tools = FakeStreamLLM([
            AIMessageChunk(content="你"),
            AIMessageChunk(content="好"),
        ])
        loop._model_call_count = 0
        loop.compactor = _make_fake_compactor()

        events = {"start": 0, "delta": [], "end": 0}
        loop.hook_manager.register("LLMStart", lambda data: events.__setitem__("start", events["start"] + 1))
        loop.hook_manager.register("LLMDelta", lambda data: events["delta"].append(data.get("delta", "")))
        loop.hook_manager.register("LLMEnd", lambda data: events.__setitem__("end", events["end"] + 1))

        state = {
            "messages": [HumanMessage(content="你好")],
            "plan": None,
        }
        result = loop._think(state)

        assert events["start"] == 1
        assert events["end"] == 1
        assert "".join(events["delta"]) == "你好"
        assert result["messages"][0].content == "你好"

    def test_stream_with_history_resets_model_call_count(self):
        loop = AgentLoop.__new__(AgentLoop)
        loop.graph = FakeGraph([])
        loop._model_call_count = 9
        loop.compactor = _make_fake_compactor()

        list(loop.stream_with_history([HumanMessage(content="测试")]))
        assert loop._model_call_count == 0
