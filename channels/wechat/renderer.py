"""Adapt Agent final answers to WeChat-friendly text messages."""

from __future__ import annotations

import re

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class WechatRenderer:
    def __init__(self, max_chars: int = 3800):
        if max_chars < 100:
            raise ValueError("max_chars must be at least 100")
        self.max_chars = max_chars

    def render(self, text: str) -> list[str]:
        cleaned = self.clean(text)
        if not cleaned:
            return ["任务已完成，但没有可显示的文字结果。"]
        return self.split(cleaned)

    @staticmethod
    def clean(text: str) -> str:
        value = ANSI_RE.sub("", str(text or ""))
        value = CONTROL_RE.sub("", value)
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        value = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", value)
        value = re.sub(r"(?m)^```[A-Za-z0-9_+.-]+\s*$", "```", value)
        value = value.replace("**", "").replace("__", "")
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    def split(self, text: str) -> list[str]:
        chunks: list[str] = []
        remaining = text
        while len(remaining) > self.max_chars:
            boundary = self._find_boundary(remaining[: self.max_chars + 1])
            chunk = remaining[:boundary].strip()
            if not chunk:
                boundary = self.max_chars
                chunk = remaining[:boundary]
            chunks.append(chunk)
            remaining = remaining[boundary:].lstrip()
        if remaining:
            chunks.append(remaining)
        return chunks

    @staticmethod
    def _find_boundary(window: str) -> int:
        candidates = [
            window.rfind("\n\n"),
            window.rfind("\n"),
            window.rfind("。"),
            window.rfind("！"),
            window.rfind("？"),
            window.rfind(". "),
            window.rfind(" "),
        ]
        boundary = max(candidates)
        if boundary < len(window) // 2:
            return len(window) - 1
        return boundary + 1
