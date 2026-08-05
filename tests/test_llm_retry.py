"""LLM 重试（错误分类+退避）、流式断流补全、通用停滞检测测试。"""

import time

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from core.agent_loop import AgentLoop
from core.llm_retry import call_with_retry, compute_delay, is_retryable
from hooks.manager import HookManager


class _FakeAPIError(Exception):
    def __init__(self, status_code):
        super().__init__(f"Error code: {status_code}")
        self.status_code = status_code


# ── 错误分类 ──────────────────────────────────────────────────

class TestIsRetryable:
    @pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
    def test_retryable_status_codes(self, code):
        assert is_retryable(_FakeAPIError(code)) is True

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
    def test_permanent_status_codes(self, code):
        assert is_retryable(_FakeAPIError(code)) is False

    def test_timeout_is_retryable(self):
        assert is_retryable(TimeoutError("request timed out")) is True

    def test_connection_reset_is_retryable(self):
        assert is_retryable(Exception("Connection reset by peer")) is True

    def test_ssl_is_not_retryable(self):
        assert is_retryable(Exception("SSL: certificate verify failed")) is False

    def test_unknown_error_is_not_retryable(self):
        assert is_retryable(ValueError("unexpected value")) is False


class TestComputeDelay:
    def test_exponential_backoff_within_jitter(self):
        d1 = compute_delay(1)
        assert 0.4 <= d1 <= 0.6  # 0.5 ± 20%
        d3 = compute_delay(3)
        assert 1.6 <= d3 <= 2.4  # 2.0 ± 20%

    def test_retry_after_takes_precedence(self):
        class _E(Exception):
            response = type("R", (), {"status_code": 429, "headers": {"Retry-After": "5"}})()
        assert compute_delay(1, _E("rate limited")) == 5.0


class TestCallWithRetry:
    def test_success_first_try(self):
        calls = []
        result = call_with_retry(lambda: calls.append(1) or "ok")
        assert result == "ok"
        assert len(calls) == 1

    def test_retries_transient_then_succeeds(self):
        sleeps = []
        state = {"n": 0}

        def flaky():
            state["n"] += 1
            if state["n"] < 3:
                raise _FakeAPIError(429)
            return "ok"

        result = call_with_retry(flaky, sleep=sleeps.append)
        assert result == "ok"
        assert state["n"] == 3
        assert len(sleeps) == 2

    def test_permanent_error_raises_immediately(self):
        state = {"n": 0}

        def bad():
            state["n"] += 1
            raise _FakeAPIError(401)

        with pytest.raises(_FakeAPIError):
            call_with_retry(bad, sleep=lambda s: None)
        assert state["n"] == 1

    def test_exhausts_attempts(self):
        state = {"n": 0}

        def always_limited():
            state["n"] += 1
            raise _FakeAPIError(429)

        with pytest.raises(_FakeAPIError):
            call_with_retry(always_limited, sleep=lambda s: None)
        assert state["n"] == 3


# ── 通用停滞检测 ──────────────────────────────────────────────

def _ai_call(name, args, i):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"tc{i}"}])


class TestStagnationDetection:
    def _detect(self, calls):
        loop = AgentLoop.__new__(AgentLoop)
        messages = [_ai_call(name, args, i) for i, (name, args) in enumerate(calls)]
        return loop._detect_stagnation(messages), messages

    def test_same_write_signature_three_times_triggers(self):
        calls = [("Bash", {"command": "ls"})] * 3
        triggered, messages = self._detect(calls)
        assert triggered is True
        assert any("系统中断" in str(m.content) for m in messages)

    def test_read_tool_relaxed_threshold(self):
        # Read 是读类工具，3 次不触发，5 次才触发
        calls = [("Read", {"path": "a.py"})] * 3
        triggered, _ = self._detect(calls)
        assert triggered is False

        calls = [("Read", {"path": "a.py"})] * 5
        triggered, _ = self._detect(calls)
        assert triggered is True

    def test_different_args_not_stagnation(self):
        calls = [("Bash", {"command": f"ls dir{i}"}) for i in range(4)]
        triggered, _ = self._detect(calls)
        assert triggered is False

    def test_below_threshold(self):
        calls = [("Edit", {"path": "a", "old_string": "x", "new_string": "y"})] * 2
        triggered, _ = self._detect(calls)
        assert triggered is False

    def test_streak_broken_by_different_call(self):
        calls = [("Bash", {"command": "ls"})] * 2 + [("Read", {"path": "x"})] + [("Bash", {"command": "ls"})]
        triggered, _ = self._detect(calls)
        assert triggered is False


# ── _think 重试与流式断流补全 ─────────────────────────────────

class _FakeCompactor:
    def should_compact(self, messages):
        return False


def _make_think_loop(llm_with_tools):
    loop = AgentLoop.__new__(AgentLoop)
    loop.memory = None
    loop.hook_manager = HookManager()
    loop.llm_with_tools = llm_with_tools
    loop.llm = None
    loop._model_call_count = 0
    loop.compactor = _FakeCompactor()
    loop.cancellation_token = None
    return loop


class TestThinkStreamRetryAndCompletion:
    def test_retryable_stream_error_retries_without_delta(self, monkeypatch):
        """未输出内容时流式失败（429）→ 退避后重试整个流。"""
        monkeypatch.setattr(time, "sleep", lambda s: None)

        class FlakyLLM:
            def __init__(self):
                self.stream_calls = 0

            def stream(self, messages):
                self.stream_calls += 1
                if self.stream_calls == 1:
                    raise RuntimeError("Error code: 429 rate limit")
                yield AIMessageChunk(content="你好")

            def invoke(self, messages):
                raise AssertionError("不应回退 invoke")

        llm = FlakyLLM()
        loop = _make_think_loop(llm)
        result = loop._think({"messages": [HumanMessage(content="hi")], "plan": None})
        assert llm.stream_calls == 2
        assert result["messages"][0].content == "你好"

    def test_broken_stream_after_delta_emits_full_answer(self):
        """流式吐字后断连 → 回退 invoke，并补发完整回答（不只留半截）。"""

        class BrokenLLM:
            def stream(self, messages):
                yield AIMessageChunk(content="半截")
                raise RuntimeError("Connection reset by peer")

            def invoke(self, messages):
                return AIMessage(content="半截后文完整回答")

        loop = _make_think_loop(BrokenLLM())
        deltas = []
        loop.hook_manager.register("LLMDelta", lambda d: deltas.append(d["delta"]))

        result = loop._think({"messages": [HumanMessage(content="hi")], "plan": None})

        assert result["messages"][0].content == "半截后文完整回答"
        assert deltas[0] == "半截"
        assert len(deltas) == 2
        assert "完整回答" in deltas[1]
        assert "半截后文完整回答" in deltas[1]

    def test_permanent_stream_error_no_retry(self):
        """确定性错误（SSL）不触发流重试，直接回退 invoke。"""

        class SSLLLM:
            def __init__(self):
                self.stream_calls = 0

            def stream(self, messages):
                self.stream_calls += 1
                raise RuntimeError("SSL: certificate verify failed")
                yield  # pragma: no cover

            def invoke(self, messages):
                return AIMessage(content="invoke 回答")

        llm = SSLLLM()
        loop = _make_think_loop(llm)
        result = loop._think({"messages": [HumanMessage(content="hi")], "plan": None})
        assert llm.stream_calls == 1  # 没有重试流
        assert result["messages"][0].content == "invoke 回答"
