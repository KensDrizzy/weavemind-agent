"""LangChain 回调适配；仅记录可观测输入输出摘要，不记录模型思维。"""

import hashlib
import json
import time
import threading

from langchain_core.callbacks import BaseCallbackHandler

from core.run_trace import clean, visible_text

RETRIEVAL_TOOLS = {"SearchCode", "SearchKnowledge", "AskKnowledge", "WebSearch", "Grep", "Glob"}


def result_status(tool, text):
    """兼容现有工具的文本协议；只使用明确的错误/空结果标记。"""
    import re
    value = text.strip()
    if tool == "Bash" and re.search(r"(?im)^\[exit code: -?[1-9]\d*\]", value):
        return "error"
    if value.startswith(("Error:", "错误：", "错误:", "工具执行错误", "资料检索失败:", "资料问答失败:",
                         "检索失败:", "WebSearch 请求失败", "[拒绝]", "[HITL] 操作已被拒绝", "[权限] 用户拒绝")):
        return "error"
    if tool in RETRIEVAL_TOOLS and (not value or value.startswith((
            "未找到", "未在知识库中找到", "没有找到", "No matches", "No results", "(no matches)", "未检索到"))):
        return "empty"
    return "returned"


class TraceCallback(BaseCallbackHandler):
    run_inline = True
    raise_error = False

    def __init__(self, trace):
        self.trace = trace
        self.spans = {}
        self.lock = threading.Lock()

    def _start(self, kind, run_id, parent_run_id, **data):
        with self.lock:
            self.spans[str(run_id)] = (time.monotonic(), data.get("tool"))
        self.trace.record(kind + ".start", span_id=str(run_id), parent_span_id=str(parent_run_id or ""), **data)

    def _end(self, kind, run_id, parent_run_id, **data):
        with self.lock:
            start, tool = self.spans.pop(str(run_id), (None, None))
        if kind == "tool":
            data["tool"] = tool or data.get("tool", "unknown")
            if "result" in data and "status" not in data:
                data["status"] = result_status(data["tool"], data["result"])
        self.trace.record(kind + ".end", span_id=str(run_id), parent_span_id=str(parent_run_id or ""),
                          duration_ms=round((time.monotonic() - start) * 1000, 3) if start else None, **data)

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs):
        params = kwargs.get("invocation_params") or {}
        self._start("llm", run_id, parent_run_id, model=params.get("model") or params.get("model_name"),
                    message_count=sum(len(batch) for batch in messages))

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs):
        self._start("chain", run_id, parent_run_id, name=kwargs.get("name") or (serialized or {}).get("name"),
                    node=(kwargs.get("metadata") or {}).get("langgraph_node"))

    def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kwargs):
        self._end("chain", run_id, parent_run_id, status="returned")

    def on_chain_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        self._end("chain", run_id, parent_run_id, status="error", error=str(error))

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, **kwargs):
        self._start("llm", run_id, parent_run_id)

    def on_llm_end(self, response, *, run_id, parent_run_id=None, **kwargs):
        usage = {}
        calls = 0
        for batch in response.generations:
            for generation in batch:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None) or usage
                calls += len(getattr(message, "tool_calls", []) or [])
        if not usage:
            output = getattr(response, "llm_output", None) or {}
            usage = output.get("token_usage") or output.get("usage") or {}
        self._end("llm", run_id, parent_run_id, status="returned", usage=usage, tool_call_count=calls)

    def on_llm_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        self._end("llm", run_id, parent_run_id, status="error", error=str(error))

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, inputs=None, **kwargs):
        args = clean(inputs if inputs is not None else input_str)
        signature = hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        self._start("tool", run_id, parent_run_id, tool=(serialized or {}).get("name", "unknown"),
                    args=args, signature=signature)

    def on_tool_end(self, output, *, run_id, parent_run_id=None, **kwargs):
        content = getattr(output, "content", output)
        text = visible_text(content) if isinstance(content, list) else str(content)
        data = {"result": text}
        if getattr(output, "status", None) == "error":
            data["status"] = "error"
        self._end("tool", run_id, parent_run_id, **data)

    def on_tool_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        self._end("tool", run_id, parent_run_id, status="error", error=str(error))
