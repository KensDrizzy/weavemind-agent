"""离线验证轨迹关联、诊断证据和异常边界，不调用外部模型。"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langgraph.graph import StateGraph, END

import settings
from core.agent_loop import AgentLoop, AgentState
from core.run_trace import RunTrace, run_scope, trace_config, clean, current_trace
from core.run_diagnosis import read_trace, diagnose_run, analyze, list_runs
from core.trace_callbacks import TraceCallback
from hooks.manager import HookManager
from tools.builtin.glob import GlobTool
from tools.builtin.bash import BashTool
from tools.builtin.diagnosis_tools import DiagnoseRunTool


@pytest.fixture
def trace_dir(tmp_path, monkeypatch):
    folder = tmp_path / "traces"
    monkeypatch.setattr(settings, "_config", {"trace": {"enabled": True, "directory": str(folder)}})
    return folder


def events_for(directory, owner):
    return read_trace(directory / (owner.last_run_id + ".jsonl"))[0]


def make_loop(graph):
    loop = AgentLoop.__new__(AgentLoop)
    loop.graph = graph
    loop.checkpointer_provider = None
    loop.cancellation_token = None
    loop.trace_session_id = "session-a"
    loop.compactor = SimpleNamespace(should_compact=lambda _: False)
    return loop


def test_real_graph_captures_llm_tool_and_parent_spans(trace_dir, tmp_path):
    model = FakeMessagesListChatModel(responses=[AIMessage(content="准备检索")])
    builder = StateGraph(AgentState)

    def search(state):
        model.invoke(state["messages"])
        GlobTool().invoke({"pattern": "*.missing", "root": str(tmp_path)})
        return {"messages": [AIMessage(content="没有匹配，尚未完成任务")]}

    builder.add_node("search", search)
    builder.set_entry_point("search")
    builder.add_edge("search", END)
    loop = make_loop(builder.compile())
    list(loop.stream_with_history([HumanMessage(content="查找文件")]))
    events = events_for(trace_dir, loop)
    assert events[0]["event"] == "run.start"
    assert events[-1]["event"] == "run.end"
    assert all(e["session_id"] == "session-a" for e in events)
    assert len({e["run_id"] for e in events}) == 1
    spans = {e["data"]["span_id"] for e in events if e["event"].endswith(".start") and "span_id" in e["data"]}
    tools = [e for e in events if e["event"] == "tool.end"]
    assert len(tools) == 1
    assert tools[0]["data"]["status"] == "empty"
    assert tools[0]["data"]["parent_span_id"] in spans
    assert any(e["event"] == "llm.end" for e in events)
    report = diagnose_run(loop.last_run_id)
    assert report["counts"]["tool_calls"] == 1
    assert report["findings"][0]["category"] == "retrieval_failure"
    assert report["task_success"] == "unknown"
    assert (trace_dir / (loop.last_run_id + ".jsonl")).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("entry", ["stream", "invoke"])
def test_graph_exception_is_recorded_and_propagated(trace_dir, entry):
    class Graph:
        def stream(self, state, config):
            raise RuntimeError("测试异常")
            yield

        def invoke(self, state, config):
            raise RuntimeError("测试异常")

    loop = make_loop(Graph())
    with pytest.raises(RuntimeError, match="测试异常"):
        result = getattr(loop, entry)("测试")
        if entry == "stream":
            list(result)
    events = events_for(trace_dir, loop)
    assert events[-1]["data"]["status"] == "error"
    assert current_trace() is None


def test_generator_close_and_no_final_response(trace_dir):
    class Graph:
        def stream(self, state, config):
            yield {"think": {"messages": [AIMessage(content="", tool_calls=[{"id": "call", "name": "Read", "args": {}}])]}}
    loop = make_loop(Graph())
    iterator = loop.stream("执行")
    next(iterator)
    iterator.close()
    assert events_for(trace_dir, loop)[-1]["data"]["status"] == "cancelled"
    list(loop.stream("再执行"))
    report = diagnose_run(loop.last_run_id)
    assert any(f["category"] == "closing_failure" for f in report["findings"])


def test_bash_nonzero_is_not_successful_tool_output(trace_dir):
    owner = SimpleNamespace()
    with run_scope(owner, "测试 shell") as trace:
        result = BashTool().invoke({"command": "exit 7"}, config=trace_config({}))
        trace.final_text = "执行失败"
    assert "[exit code: 7]" in result
    report = diagnose_run(owner.last_run_id)
    assert report["counts"]["tool_errors"] == 1
    assert any(f["category"] == "tool_failure" for f in report["findings"])


def test_latest_excludes_diagnosis_current_and_other_sessions(trace_dir):
    owner = SimpleNamespace(trace_session_id="a")
    with run_scope(owner, "原任务"):
        pass
    original_id = owner.last_run_id
    with run_scope(SimpleNamespace(trace_session_id="b"), "其他会话"):
        pass
    for _ in range(2):
        with run_scope(owner, "为什么失败"):
            result = DiagnoseRunTool().invoke({}, config=trace_config({}))
            assert original_id in result
    assert len(list_runs(session_id="a")) == 1


def test_parallel_runs_do_not_mix(trace_dir):
    def run(index):
        owner = SimpleNamespace(trace_session_id=str(index))
        with run_scope(owner, str(index)) as trace:
            trace.record("marker", index=index)
        return owner
    with ThreadPoolExecutor(max_workers=4) as pool:
        owners = list(pool.map(run, range(8)))
    assert len({o.last_run_id for o in owners}) == 8
    for i, owner in enumerate(owners):
        assert all(e["session_id"] == str(i) for e in events_for(trace_dir, owner))


def test_plan_threads_and_failed_dependencies_are_recorded(trace_dir):
    from core.plan_models import Plan, Task
    from core.plan_executor import PlanExecutor
    class FailingTool:
        def invoke(self, args):
            raise RuntimeError("上游失败")
    executor = PlanExecutor(SimpleNamespace(get=lambda _: FailingTool()),
                            SimpleNamespace(is_allowed=lambda _: True), hook_manager=HookManager())
    plan = Plan(goal="任务", tasks=[Task(id="a", description="上游", tool_name="Read"),
                                  Task(id="b", description="下游", tool_name="Read", dependencies=["a"])])
    owner = SimpleNamespace()
    with run_scope(owner):
        executor.execute(plan)
    events = events_for(trace_dir, owner)
    assert any(e["event"] == "task.end" and e["data"]["task_id"] == "a" for e in events)
    assert any(e["event"] == "task.skipped" and e["data"]["dependency"] == "a" for e in events)
    assert any(f["category"] == "planning_failure" for f in analyze(events)["findings"])


def test_repeated_calls_are_hypotheses_and_secrets_are_redacted(trace_dir):
    owner = SimpleNamespace()
    with run_scope(owner, "password=hunter2") as trace:
        callback = TraceCallback(trace)
        for i in range(3):
            callback.on_tool_start({"name": "WebSearch"}, "", run_id=str(i),
                                   inputs={"query": "示例", "api_key": "dont-store-this"})
            callback.on_tool_end("未找到结果", run_id=str(i))
    raw = (trace_dir / (owner.last_run_id + ".jsonl")).read_text()
    assert "dont-store-this" not in raw and "hunter2" not in raw
    report = diagnose_run(owner.last_run_id)
    repeated = next(f for f in report["findings"] if f["category"] == "repeated_calls")
    assert repeated["certainty"] == "hypothesis" and len(repeated["evidence"]) == 3
    assert clean({"reasoning_content": "hidden-thought"})["reasoning_content"] == "[已隐藏]"
    assert "json-secret" not in clean('{"api_key": "json-secret"}')
    assert "image-data" not in str(clean({"type": "image", "source": {"data": "image-data"}}))


def test_trace_limits_corruption_and_io_failure_are_nonfatal(trace_dir, tmp_path):
    trace = RunTrace(trace_dir, max_events=2)
    trace.record("first")
    trace.record("dropped")
    trace.finish()
    with trace.path.open("a") as stream:
        stream.write('{"broken":')
    events, warnings = read_trace(trace.path)
    assert warnings and events[-1]["event"] == "run.end"
    assert events[-1]["data"]["dropped_events"] == 1
    assert analyze(events, warnings)["warnings"]
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("文件")
    broken = RunTrace(blocked)
    broken.finish()
    assert broken.disabled


def test_disabled_trace_and_invalid_run_id(trace_dir, monkeypatch):
    monkeypatch.setitem(settings._config["trace"], "enabled", False)
    with run_scope(SimpleNamespace()) as trace:
        assert trace is None
    assert not trace_dir.exists()
    assert "error" in diagnose_run("../../secret")


def test_skill_resources_load_and_script_matches_tool(trace_dir):
    import subprocess
    import sys
    from skills.registry import SkillRegistry
    from skills.buffer import SkillContextBuffer
    from tools.builtin.skill_tools import LoadSkillTool
    root = Path(__file__).resolve().parents[1]
    registry = SkillRegistry(root / "skills/builtin", trace_dir / "user", trace_dir / "project")
    registry.reload()
    skill = registry.find_skill("agent-run-diagnosis")
    assert skill and skill.references_dir.is_dir()
    buffer = SkillContextBuffer()
    LoadSkillTool(skill_registry=registry, skill_buffer=buffer)._run(skill.name)
    assert str(skill.skill_md_path.parent) in buffer.drain()
    owner = SimpleNamespace()
    with run_scope(owner, "无输出的任务"):
        pass
    result = subprocess.run([sys.executable, str(skill.skill_md_path.parent / "scripts/diagnose.py"),
                             "--trace-dir", str(trace_dir), "--run-id", owner.last_run_id, "--format", "json"],
                            capture_output=True, text=True, check=True)
    report = json.loads(result.stdout)
    assert report["findings"] == diagnose_run(owner.last_run_id)["findings"]


def test_batch_children_keep_trace_callbacks(trace_dir, tmp_path, monkeypatch):
    from agents.batch_delegate import BatchDelegateTool
    def child(**kwargs):
        assert current_trace() is not None
        return GlobTool().invoke({"pattern": "*.absent", "root": str(tmp_path)})
    monkeypatch.setattr("agents.batch_delegate.run_subagent", child)
    batch = BatchDelegateTool()
    builder = StateGraph(AgentState)
    def delegate(state):
        batch.invoke({"tasks": [{"goal": "一"}, {"goal": "二"}]})
        return {"messages": [AIMessage(content="检查结束")]}
    builder.add_node("delegate", delegate)
    builder.set_entry_point("delegate")
    builder.add_edge("delegate", END)
    loop = make_loop(builder.compile())
    list(loop.stream("查找两类文件"))
    events = events_for(trace_dir, loop)
    children = [e for e in events if e["event"] == "tool.end" and e["data"]["tool"] == "Glob"]
    assert len(children) == 2
    assert children[0]["data"]["parent_span_id"] == children[1]["data"]["parent_span_id"]


@pytest.mark.parametrize("entry", ["stream", "run"])
def test_team_tracks_worker_output_and_rejected_review(trace_dir, entry):
    from agents.orchestrator import MultiAgentOrchestrator
    updates = {"step_results": {"worker-1": "只完成了第一步"}, "review_status": "max_retries_exceeded"}
    class Graph:
        def invoke(self, state, config):
            return updates
        def stream(self, state, config):
            yield {"worker-1": {"step_results": updates["step_results"]}}
            yield {"reviewer": {"review_status": updates["review_status"]}}
    team = MultiAgentOrchestrator.__new__(MultiAgentOrchestrator)
    team.graph = Graph()
    team.checkpointer = None
    team._configure_graph_for_task = lambda _: None
    result = getattr(team, entry)("完成开发并验收")
    if entry == "stream":
        list(result)
    events = events_for(trace_dir, team)
    assert events[-1]["data"]["output_present"] is True
    report = diagnose_run(team.last_run_id)
    assert any("审查未通过" in f["summary"] for f in report["findings"])


def test_permission_denial_is_visible_without_executing_tool(trace_dir):
    from core.hitl_policy import requires_approval
    assert not requires_approval("DiagnoseRun")
    loop = make_loop(None)
    loop._tool_unavailable_reasons = {}
    loop._tool_failure_counts = {}
    loop._disabled_tools = {}
    loop.hook_manager = HookManager()
    loop.permission_policy = SimpleNamespace(is_allowed=lambda _: False)
    state = {"messages": [AIMessage(content="", tool_calls=[{"id": "call", "name": "Read", "args": {"path": "x"}}])]}
    with run_scope(loop, "读取"):
        loop._act(state)
    report = diagnose_run(loop.last_run_id)
    assert any(f["category"] == "tool_failure" for f in report["findings"])
