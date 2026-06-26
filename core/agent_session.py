"""UI-independent Agent conversation session.

Both terminal and remote channels can use this class without depending on
prompt_toolkit, Rich rendering, or terminal HITL input.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from core.cancellation import AgentCancelledError, CancellationToken

logger = logging.getLogger(__name__)


@dataclass
class AgentRunResult:
    text: str = ""
    success: bool = True
    cancelled: bool = False
    error: Optional[str] = None
    messages: list[BaseMessage] = field(default_factory=list)


class AgentSession:
    """Owns one conversation and serializes access to one AgentLoop."""

    def __init__(
        self,
        agent_loop,
        *,
        cancellation_token: Optional[CancellationToken] = None,
        max_messages: int = 40,
    ):
        self.agent_loop = agent_loop
        self.cancellation_token = cancellation_token or CancellationToken()
        self.max_messages = max_messages
        self.conversation: list[BaseMessage] = []
        self._run_lock = threading.Lock()
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def run(self, user_input: str) -> AgentRunResult:
        """Run one user turn and return only the final user-visible answer."""
        if not user_input or not user_input.strip():
            return AgentRunResult(text="", success=False, error="empty user input")

        with self._run_lock:
            self._running = True
            self.cancellation_token.reset()
            user_message = HumanMessage(content=user_input.strip())
            self.conversation.append(user_message)
            self._compact_if_needed()

            try:
                final_message = None
                new_messages: list[BaseMessage] = []

                for event in self.agent_loop.stream_with_history(self.conversation):
                    self.cancellation_token.raise_if_cancelled()
                    for state in event.values():
                        if not isinstance(state, dict):
                            continue
                        messages = state.get("messages") or []
                        for message in messages:
                            if (
                                isinstance(message, AIMessage)
                                and not getattr(message, "tool_calls", None)
                            ):
                                final_message = message

                if final_message is None:
                    return AgentRunResult(
                        text="",
                        success=False,
                        error="Agent did not produce a final response",
                    )

                text = self._extract_text(final_message.content)
                if text.strip():
                    self.conversation.append(final_message)
                    new_messages.append(final_message)
                else:
                    return AgentRunResult(
                        text="",
                        success=False,
                        error="Agent returned an empty response",
                    )

                self._trim_history()
                return AgentRunResult(
                    text=text,
                    success=True,
                    messages=new_messages,
                )
            except AgentCancelledError:
                cancelled_message = AIMessage(content="任务已取消。")
                self.conversation.append(cancelled_message)
                self._trim_history()
                return AgentRunResult(
                    text="任务已取消。",
                    success=False,
                    cancelled=True,
                    messages=[cancelled_message],
                )
            except Exception as exc:
                logger.exception("Agent session failed")
                return AgentRunResult(
                    text="",
                    success=False,
                    error=str(exc),
                )
            finally:
                self._running = False

    def cancel(self) -> bool:
        if not self._running:
            return False
        self.cancellation_token.cancel()
        return True

    def clear(self) -> None:
        if self._running:
            raise RuntimeError("cannot clear a running session")
        self.conversation.clear()

    def compact(self) -> bool:
        if self._running:
            raise RuntimeError("cannot compact a running session")
        compactor = getattr(self.agent_loop, "compactor", None)
        if not compactor or not self.conversation:
            return False
        compacted = compactor.compact(self.conversation)
        if not compacted:
            return False
        self.conversation = list(compacted)
        return True

    def _compact_if_needed(self) -> None:
        compactor = getattr(self.agent_loop, "compactor", None)
        if compactor and compactor.should_compact(self.conversation):
            compacted = compactor.compact(self.conversation)
            if compacted:
                self.conversation = list(compacted)

    def _trim_history(self) -> None:
        if len(self.conversation) > self.max_messages:
            self.conversation = self.conversation[-self.max_messages :]

    @staticmethod
    def _extract_text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
            return "".join(parts)
        return str(content or "")
