"""SkillContextBuffer — Skill body 注入缓冲区。

生命周期: load_skill → push → 下一轮 drain → 拼到 user message 前。
drain 是一次性消费，防止跨轮重复注入。
"""


class SkillContextBuffer:
    MAX_SKILLS = 3

    def __init__(self):
        self._entries: dict[str, str] = {}  # name → body, insertion order

    def push(self, name: str, body: str) -> None:
        if not name or not body:
            return
        # 同名替换 如果之前 push 过同一个 Skill，删掉旧的，新的放末尾
        self._entries.pop(name, None)
        self._entries[name] = body
        # LRU 淘汰  超过 3 个时，删掉最早 push 的
        while len(self._entries) > self.MAX_SKILLS:
            oldest = next(iter(self._entries))
            del self._entries[oldest]


# drain 是一次性消费——取出所有 body 后立即清空。
# 这保证了同一个 Skill 的 body 不会被重复注入到消息中。
    def drain(self) -> str:
        """一次性消费所有 Skill body，返回格式化文本后清空。"""
        if not self._entries:
            return ""
        parts = []
        for name, body in self._entries.items():
            parts.append(f"## 已加载 Skill：{name}\n{body.strip()}\n")
        parts.append("---\n")
        self._entries.clear()
        return "\n".join(parts)

    def is_empty(self) -> bool:
        return len(self._entries) == 0

    def size(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()
