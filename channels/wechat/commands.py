"""Strict slash-command parsing for WeChat messages."""

from __future__ import annotations

from dataclasses import dataclass
import shlex
from typing import Optional


KNOWN_COMMANDS = {
    "/help",
    "/status",
    "/clear",
    "/compact",
    "/pause",
    "/resume",
    "/stop",
}


@dataclass(frozen=True)
class WechatCommand:
    name: str
    args: tuple[str, ...] = ()


def parse_command(text: str) -> Optional[WechatCommand]:
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None
    try:
        parts = shlex.split(stripped)
    except ValueError:
        return None
    if not parts:
        return None
    name = parts[0].lower()
    if name not in KNOWN_COMMANDS:
        return None
    return WechatCommand(name=name, args=tuple(parts[1:]))
