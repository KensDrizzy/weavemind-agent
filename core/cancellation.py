"""Cooperative cancellation primitives for long-running Agent sessions."""

from __future__ import annotations

import threading


class AgentCancelledError(RuntimeError):
    """Raised when an Agent run is cancelled by its owning channel."""


class CancellationToken:
    """Thread-safe cooperative cancellation token.

    Python threads cannot be forcefully terminated safely. Callers should check
    the token at orchestration boundaries and give blocking operations explicit
    timeouts.
    """

    def __init__(self):
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def reset(self) -> None:
        self._event.clear()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise AgentCancelledError("Agent task cancelled")
