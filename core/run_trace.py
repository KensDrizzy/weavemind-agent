"""本地运行轨迹：逐事件落盘，观测失败不影响 Agent 执行。"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import uuid

logger = logging.getLogger(__name__)
_current = ContextVar("weavemind_trace", default=None)
_SECRET_KEY = re.compile(r"api[_-]?key|password|passwd|secret|authorization|cookie|access[_-]?token|refresh[_-]?token|^token$", re.I)
_SECRET_TEXT = re.compile(
    r"(?i)(bearer\s+)[\w.\-]+|"
    r"((?:api[_-]?key|password|passwd|secret|access[_-]?token|refresh[_-]?token)[\"']?\s*[=:]\s*[\"']?)[^\s\"'&,;]+|"
    r"\bsk-[A-Za-z0-9_-]{8,}|data:image/[^\s]+"
)


def clean(value, limit=1500, depth=0):
    """仅存有界摘要；屏蔽常见凭证和图片载荷，不保存模型思维内容。"""
    if depth > 5:
        return "[嵌套内容省略]"
    if isinstance(value, dict):
        if isinstance(value.get("type"), str) and value["type"] in {"image", "image_url", "input_image", "thinking", "reasoning"}:
            return "[图片或思维内容已省略]"
        return {str(k): "[已隐藏]" if _SECRET_KEY.search(str(k)) or str(k) in
                {"reasoning_content", "thinking", "image_url", "base64"}
                else clean(v, limit, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        return [clean(v, limit, depth + 1) for v in value[:30]]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    text = _SECRET_TEXT.sub(lambda m: (m.group(1) or m.group(2) or "") + "[已隐藏]", str(value))
    return text if len(text) <= limit else text[:limit] + "…[已截断]"


def visible_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text", "")) for p in content
                       if isinstance(p, dict) and p.get("type") == "text")
    return ""


def trace_setting(key, default):
    import settings
    try:
        value = settings.get("trace." + key, default)
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


def current_trace():
    return _current.get()


def record_event(event, **data):
    trace = current_trace()
    if trace:
        trace.record(event, **data)


class RunTrace:
    def __init__(self, directory, *, session_id=None, mode="react", user_input="", max_events=2000):
        self.run_id = uuid.uuid4().hex
        self.session_id = session_id
        self.mode = mode
        self.path = Path(directory).expanduser().resolve() / (self.run_id + ".jsonl")
        self.started = time.monotonic()
        self.lock = threading.RLock()
        self.seq = 0
        self.max_events = max(2, int(max_events))
        self.dropped = 0
        self.closed = False
        self.disabled = False
        self.final_text = ""
        self.pending_tools = False
        self.stop_reason = None
        self.plan_failed = False
        self.review_status = None
        self.agent_results = {}
        self.record("run.start", mode=mode, user_input=user_input,
                    coverage="模型与工具回调、计划事件、图节点输出；未保存完整输入或思维内容")

    def record(self, event, **data):
        with self.lock:
            if self.closed or self.disabled:
                return
            if self.seq >= self.max_events and event != "run.end":
                self.dropped += 1
                return
            self.seq += 1
            item = {"schema_version": 1, "run_id": self.run_id, "session_id": self.session_id,
                    "seq": self.seq, "timestamp": time.time(),
                    "elapsed_ms": round((time.monotonic() - self.started) * 1000, 3),
                    "event": event, "data": clean(data)}
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as stream:
                    stream.write(json.dumps(item, ensure_ascii=False) + "\n")
            except (OSError, TypeError, ValueError):
                self.disabled = True
                logger.warning("运行轨迹写入失败，已停止本次采集", exc_info=True)

    def observe(self, state, node=""):
        if not isinstance(state, dict):
            return
        self.record("graph.step", node=node)
        for message in state.get("messages") or []:
            if getattr(message, "type", None) == "ai":
                self.final_text = visible_text(message.content)
                self.pending_tools = bool(getattr(message, "tool_calls", None))
        plan = state.get("plan")
        if isinstance(plan, dict):
            self.plan_failed |= str(plan.get("status")) == "failed"
        if self.mode == "team":
            for agent, result in (state.get("step_results") or {}).items():
                if self.agent_results.get(agent) != result:
                    self.record("agent.result", agent=agent, result=result)
                self.agent_results[agent] = result
            outputs = [str(result) for agent, result in self.agent_results.items() if agent.startswith("worker")]
            if outputs:
                self.final_text = "\n".join(outputs)
                self.pending_tools = False
        if "review_status" in state:
            self.review_status = state["review_status"]
            self.record("review.result", status=state["review_status"], retry_count=state.get("retry_count"))

    def finish(self, status="finished", error=None):
        with self.lock:
            if self.closed:
                return
            self.record("run.end", status=status, stop_reason=self.stop_reason,
                        final_response=self.final_text, pending_tool_calls=self.pending_tools,
                        output_present=bool(self.final_text.strip()) and not self.pending_tools,
                        plan_failed=self.plan_failed, error=error, dropped_events=self.dropped,
                        review_status=self.review_status,
                        task_success="unknown")
            self.closed = True


@contextmanager
def run_scope(owner, user_input="", mode="react"):
    if current_trace() or not trace_setting("enabled", True):
        yield current_trace()
        return
    try:
        trace = RunTrace(trace_setting("directory", ".weavemind/traces"),
                         session_id=getattr(owner, "trace_session_id", None), mode=mode,
                         user_input=user_input, max_events=trace_setting("max_events", 2000))
    except (OSError, ValueError, TypeError):
        logger.warning("运行轨迹初始化失败，本轮继续执行", exc_info=True)
        yield None
        return
    owner.last_run_id = trace.run_id
    token = _current.set(trace)
    try:
        yield trace
    except BaseException as exc:
        cancelled = isinstance(exc, (KeyboardInterrupt, GeneratorExit)) or type(exc).__name__ == "AgentCancelledError"
        trace.finish("cancelled" if cancelled else "error", str(exc))
        raise
    else:
        trace.finish()
    finally:
        _current.reset(token)


def _request(args):
    if not args:
        return ""
    if isinstance(args[0], str):
        return args[0]
    if isinstance(args[0], list):
        for msg in reversed(args[0]):
            if getattr(msg, "type", None) == "human":
                return visible_text(msg.content)
    return ""


def traced_stream(mode="react"):
    def decorate(fn):
        @wraps(fn)
        def wrapped(self, *args, **kwargs):
            actual_mode = "plan" if getattr(self, "force_plan_mode", False) else mode
            request = _request(args) or _request([kwargs.get("user_input", kwargs.get("conversation", ""))])
            with run_scope(self, request, actual_mode) as trace:
                iterator = fn(self, *args, **kwargs)
                try:
                    for event in iterator:
                        if trace and isinstance(event, dict):
                            for node, state in event.items():
                                trace.observe(state, node)
                        yield event
                finally:
                    iterator.close()
        return wrapped
    return decorate


def traced_invoke(mode="react"):
    def decorate(fn):
        @wraps(fn)
        def wrapped(self, *args, **kwargs):
            actual_mode = "plan" if getattr(self, "force_plan_mode", False) else mode
            with run_scope(self, _request(args) or kwargs.get("user_input", ""), actual_mode) as trace:
                result = fn(self, *args, **kwargs)
                if trace:
                    trace.observe(result, "result")
                return result
        return wrapped
    return decorate


def trace_config(config):
    """在实际图调用入口安装回调，覆盖 LangGraph 内部模型和工具。"""
    trace = current_trace()
    if not trace:
        return config
    from core.trace_callbacks import TraceCallback
    result = dict(config)
    result["callbacks"] = list(config.get("callbacks") or []) + [TraceCallback(trace)]
    trace.record("graph.config", thread_id=config.get("configurable", {}).get("thread_id"))
    return result


def capture_hook(event, data):
    """补充图回调看不到的审批拒绝、计划状态和停止原因。"""
    trace = current_trace()
    if not trace:
        return
    if event.startswith("Plan") or event == "PostToolUse" and data.get("error"):
        trace.record("hook." + event, **data)
        if event == "PlanError" or event == "PlanExecuteEnd" and data.get("status") == "failed":
            trace.plan_failed = True


def stop_trace(reason):
    trace = current_trace()
    if trace:
        trace.stop_reason = reason
        trace.record("run.stop", reason=reason)


def traced_task(fn):
    @wraps(fn)
    def wrapped(self, task, plan):
        record_event("task.start", plan_id=plan.id, task_id=task.id, tool=task.tool_name,
                     dependencies=task.dependencies)
        try:
            return fn(self, task, plan)
        finally:
            record_event("task.end", plan_id=plan.id, task_id=task.id, tool=task.tool_name,
                         status=task.status.value, error=task.error, result=task.result)
    return wrapped
