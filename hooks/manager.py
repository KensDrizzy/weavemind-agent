"""事件钩子管理器 — 支持工具执行前后的回调。"""

from collections import defaultdict


class HookManager:
    def __init__(self):
        self._hooks: dict[str, list] = defaultdict(list)

    def register(self, event: str, callback, matcher: str = "*"):
        """注册事件回调。matcher 支持通配符 '*' 匹配所有工具。"""
        self._hooks[event].append((matcher, callback))

    def fire(self, event: str, tool_name: str, context=None):
        """触发事件（兼容旧接口：tool_name + context）。"""
        for matcher, cb in self._hooks.get(event, []):
            if matcher == "*" or matcher == tool_name:
                cb(tool_name, context)

    def emit(self, event: str, data: dict = None):
        """触发事件（新接口：统一 dict 传参）。"""
        payload = data or {}
        tool_name = payload.get("tool")
        for matcher, cb in self._hooks.get(event, []):
            if matcher != "*" and matcher != tool_name:
                continue
            cb(payload)
