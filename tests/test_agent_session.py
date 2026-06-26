from langchain_core.messages import AIMessage

from core.agent_session import AgentSession
from core.cancellation import CancellationToken


class _NoopCompactor:
    def should_compact(self, _messages):
        return False

    def compact(self, messages):
        return list(messages)


class _FakeAgentLoop:
    def __init__(self, reply="done"):
        self.reply = reply
        self.compactor = _NoopCompactor()

    def stream_with_history(self, _conversation):
        yield {"think": {"messages": [AIMessage(content=self.reply)]}}


def test_agent_session_keeps_conversation():
    session = AgentSession(_FakeAgentLoop("hello"))

    result = session.run("hi")

    assert result.success is True
    assert result.text == "hello"
    assert len(session.conversation) == 2


def test_agent_session_clear():
    session = AgentSession(_FakeAgentLoop())
    session.run("hi")

    session.clear()

    assert session.conversation == []


def test_cancellation_token_round_trip():
    token = CancellationToken()
    token.cancel()
    assert token.is_cancelled() is True
    token.reset()
    assert token.is_cancelled() is False
