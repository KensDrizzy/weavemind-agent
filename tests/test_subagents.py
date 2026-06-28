import time

from agents.batch_delegate import BatchDelegateTool
from agents.loader import load_agent_def
from agents.monitor import SubAgentMonitor, SubAgentStatus
from agents.subagent import (
    SUBAGENT_BLOCKED_TOOLS,
    _infer_provider,
    _load_subagent_tools,
)
from tools.base import WeaveMindTool


def test_load_agent_def(tmp_path):
    md = tmp_path / "test-agent.md"
    md.write_text("---\nname: test\ntools: [Read]\n---\nYou are a test agent.")
    defn = load_agent_def(str(md))
    assert defn["name"] == "test"
    assert defn["tools"] == ["Read"]
    assert "test agent" in defn["system_prompt"]


class TestInferProvider:
    """测试 _infer_provider 模型名到 provider 的推断。"""

    def test_deepseek(self):
        assert _infer_provider("deepseek-v4-pro") == "deepseek"
        assert _infer_provider("deepseek-chat") == "deepseek"

    def test_claude(self):
        assert _infer_provider("claude-haiku-4-5-20251001") == "anthropic"
        assert _infer_provider("claude-sonnet-4-6") == "anthropic"

    def test_gpt(self):
        assert _infer_provider("gpt-4o") == "openai"
        assert _infer_provider("o1-preview") == "openai"

    def test_mimo(self):
        assert _infer_provider("mimo-v2.5-pro") == "mimo"

    def test_unknown_defaults_to_openai(self):
        assert _infer_provider("some-unknown-model") == "openai"


class DummyTool(WeaveMindTool):
    name: str = "Read"
    description: str = "Read a file. Args: path"

    def _run(self, path: str = "") -> str:
        return f"read:{path}"


class DangerousDummyTool(WeaveMindTool):
    name: str = "Bash"
    description: str = "Run shell. Args: command"

    def _run(self, command: str, timeout: int = 120) -> str:
        raise AssertionError("dangerous tool should have been denied")


class RegistryStub:
    def __init__(self, tools):
        self.tools = {tool.name: tool for tool in tools}

    def get(self, name):
        return self.tools.get(name)

    def get_langchain_tools(self):
        return list(self.tools.values())


class TestSubAgentToolIsolation:
    def test_filters_blocked_tools_when_explicitly_requested(self):
        registry = RegistryStub([DummyTool(), DangerousDummyTool()])
        tools = _load_subagent_tools(
            registry,
            tool_names=["Read", "AskUser", "MemoryAdd"],
            blocked_tools=SUBAGENT_BLOCKED_TOOLS,
        )

        assert [tool.name for tool in tools] == ["Read"]

    def test_default_toolset_excludes_blocked_tools(self):
        class AskUserDummy(DummyTool):
            name: str = "AskUser"

        registry = RegistryStub([DummyTool(), AskUserDummy(), DangerousDummyTool()])
        tools = _load_subagent_tools(
            registry,
            tool_names=[],
            blocked_tools=SUBAGENT_BLOCKED_TOOLS,
        )

        assert "Read" in [tool.name for tool in tools]
        assert "Bash" in [tool.name for tool in tools]
        assert "AskUser" not in [tool.name for tool in tools]

    def test_dangerous_tool_is_auto_denied_by_default(self):
        registry = RegistryStub([DangerousDummyTool()])
        tools = _load_subagent_tools(
            registry,
            tool_names=["Bash"],
            blocked_tools=SUBAGENT_BLOCKED_TOOLS,
            auto_approve=False,
        )

        result = tools[0]._run(command="rm -rf /tmp/weavemind-test")

        assert "已拒绝" in result


class SlowBatchDelegateTool(BatchDelegateTool):
    delays: dict = {}
    failures: set = set()

    def _run_single_task(self, task: dict, subagent_id: str | None = None) -> str:
        delay = self.delays.get(task["goal"], 0)
        if delay:
            time.sleep(delay)
        if task["goal"] in self.failures:
            raise RuntimeError("boom")
        return f"done:{task['goal']}"


class TestBatchDelegateTool:
    def test_parallel_execution_finishes_near_slowest_task(self):
        tool = SlowBatchDelegateTool(delays={"a": 0.2, "b": 0.2, "c": 0.2})
        start = time.monotonic()

        result = tool._run(
            tasks=[{"goal": "a"}, {"goal": "b"}, {"goal": "c"}],
            max_parallel=3,
            timeout=2,
        )

        elapsed = time.monotonic() - start
        assert elapsed < 0.45
        assert "成功完成 (3 个)" in result

    def test_partial_failure_does_not_hide_successes(self):
        tool = SlowBatchDelegateTool(failures={"b"})

        result = tool._run(
            tasks=[{"goal": "a"}, {"goal": "b"}],
            max_parallel=2,
            timeout=2,
        )

        assert "成功完成 (1 个)" in result
        assert "失败/超时 (1 个)" in result
        assert "b: error" in result

    def test_timeout_is_reported_without_waiting_for_child_completion(self):
        tool = SlowBatchDelegateTool(delays={"slow": 0.5, "fast": 0.02})
        start = time.monotonic()

        result = tool._run(
            tasks=[{"goal": "slow"}, {"goal": "fast"}],
            max_parallel=2,
            timeout=0.1,
        )

        elapsed = time.monotonic() - start
        assert elapsed < 0.4
        assert "fast" in result
        assert "slow: timeout" in result


class FakeFuture:
    def __init__(self):
        self.cancelled = False

    def done(self):
        return False

    def cancel(self):
        self.cancelled = True
        return True


class TestSubAgentMonitor:
    def test_detects_stale_idle(self):
        monitor = SubAgentMonitor(heartbeat_interval=1, stale_cycles_idle=1)
        hb = monitor.register("child-1")
        hb.status = SubAgentStatus.IDLE
        hb.last_heartbeat = time.time() - 2

        assert monitor.check_stale() == ["child-1"]
        assert hb.status == SubAgentStatus.STALE

    def test_allows_longer_tool_execution_window(self):
        monitor = SubAgentMonitor(
            heartbeat_interval=1,
            stale_cycles_idle=1,
            stale_cycles_in_tool=40,
        )
        hb = monitor.register("child-1")
        hb.status = SubAgentStatus.IN_TOOL
        hb.last_heartbeat = time.time() - 10

        assert monitor.check_stale() == []
        assert hb.status == SubAgentStatus.IN_TOOL

    def test_interrupt_marks_only_target_child(self):
        monitor = SubAgentMonitor()
        future = FakeFuture()
        monitor.register("child-1", future=future)
        other = monitor.register("child-2")

        assert monitor.interrupt("child-1") is True
        assert future.cancelled is True
        assert monitor.get("child-1").status == SubAgentStatus.INTERRUPTED
        assert other.status == SubAgentStatus.RUNNING
