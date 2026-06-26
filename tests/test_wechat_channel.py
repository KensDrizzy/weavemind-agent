import json
import os
import time
from pathlib import Path

import httpx
import pytest

from channels.wechat.account_store import AccountStore
from channels.wechat.commands import parse_command
from channels.wechat.engine import MessageDeduplicator, WechatMessageEngine
from channels.wechat.ilink_client import ILinkClient, SessionExpiredError
from channels.wechat.message_parser import parse_inbound_message
from channels.wechat.models import PollResult, WechatAccount
from channels.wechat.renderer import WechatRenderer
from channels.wechat.safety import (
    ScopedReadTool,
    WorkspaceGuard,
    WorkspaceViolationError,
)
from core.agent_session import AgentRunResult


def _account(tmp_path: Path) -> WechatAccount:
    return WechatAccount(
        bot_token="secret-token",
        bot_id="bot@im.bot",
        bound_user_id="owner@im.wechat",
        base_url="https://ilinkai.weixin.qq.com",
        workspace=str(tmp_path),
    )


def _raw_message(
    message_id=1,
    *,
    sender="owner@im.wechat",
    text="hello",
    context_token="ctx-1",
):
    return {
        "message_id": message_id,
        "from_user_id": sender,
        "to_user_id": "bot@im.bot",
        "message_type": 1,
        "message_state": 2,
        "context_token": context_token,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }


def test_account_store_is_atomic_and_private(tmp_path):
    store = AccountStore(tmp_path / "wechat" / "account.json")
    account = _account(tmp_path)

    store.save(account)
    loaded = store.load()

    assert loaded == account
    assert oct(os.stat(store.path).st_mode & 0o777) == "0o600"


def test_command_parser_is_exact():
    assert parse_command("/status").name == "/status"
    assert parse_command("/STATUS").name == "/status"
    assert parse_command("/status now").args == ("now",)
    assert parse_command("/status-extra") is None
    assert parse_command("please /status") is None


def test_message_parser_extracts_text_and_voice():
    raw = _raw_message()
    raw["item_list"].append(
        {"type": 3, "voice_item": {"text": "voice transcript"}}
    )

    message = parse_inbound_message(raw)

    assert message.text == "hello\nvoice transcript"
    assert message.attachments[0].kind == "voice"


def test_renderer_removes_terminal_markup_and_splits():
    renderer = WechatRenderer(max_chars=100)
    text = "\x1b[31m# Title\x1b[0m\n\n**bold**\n" + ("x" * 150)

    chunks = renderer.render(text)

    assert chunks[0].startswith("Title")
    assert all("\x1b" not in chunk for chunk in chunks)
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert len(chunks) >= 2


def test_workspace_guard_rejects_escape(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    guard = WorkspaceGuard(workspace)

    with pytest.raises(WorkspaceViolationError):
        guard.resolve("../outside.txt")


def test_scoped_read_only_reads_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "ok.txt").write_text("safe", encoding="utf-8")
    tool = ScopedReadTool(workspace=str(workspace))

    assert "safe" in tool.invoke({"path": "ok.txt"})
    with pytest.raises(WorkspaceViolationError):
        tool.invoke({"path": "../outside.txt"})


def test_ilink_get_updates_and_headers():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "ret": 0,
                "msgs": [_raw_message()],
                "get_updates_buf": "cursor-2",
                "longpolling_timeout_ms": 35000,
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = ILinkClient(token="token", client=http_client)

    result = client.get_updates("cursor-1")

    assert result.get_updates_buf == "cursor-2"
    assert len(result.messages) == 1
    request = requests[0]
    body = json.loads(request.content)
    assert body["get_updates_buf"] == "cursor-1"
    assert body["base_info"]["bot_agent"].startswith("WeaveMindAgent/")
    assert request.headers["Authorization"] == "Bearer token"
    assert request.headers["AuthorizationType"] == "ilink_bot_token"
    assert request.headers["iLink-App-Id"] == "bot"


def test_ilink_session_expired():
    def handler(_request):
        return httpx.Response(
            200,
            json={"ret": 1, "errcode": -14, "errmsg": "expired"},
        )

    client = ILinkClient(
        token="token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(SessionExpiredError):
        client.get_updates("")


def test_ilink_send_message_echoes_context_token():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ret": 0})

    client = ILinkClient(
        token="token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    client.send_message(to_user_id="owner", context_token="ctx", text="reply")

    msg = bodies[0]["msg"]
    assert msg["to_user_id"] == "owner"
    assert msg["context_token"] == "ctx"
    assert msg["message_type"] == 2
    assert msg["message_state"] == 2


def test_deduplicator_is_bounded():
    dedup = MessageDeduplicator(max_entries=2)
    assert dedup.add_if_new("1") is True
    assert dedup.add_if_new("1") is False
    assert dedup.add_if_new("2") is True
    assert dedup.add_if_new("3") is True
    assert dedup.add_if_new("1") is True


class _FakeClient:
    def __init__(self):
        self.sent = []
        self.typing = []

    def send_message(self, **kwargs):
        self.sent.append(kwargs)

    def get_typing_ticket(self, **_kwargs):
        return "ticket"

    def send_typing(self, **kwargs):
        self.typing.append(kwargs)

    def notify_start(self):
        pass

    def notify_stop(self):
        pass

    def get_updates(self, cursor, timeout_seconds):
        return PollResult(messages=(), get_updates_buf=cursor)


class _FakeSession:
    def __init__(self):
        self.inputs = []
        self.cleared = False

    def run(self, text):
        self.inputs.append(text)
        return AgentRunResult(text=f"reply: {text}")

    def cancel(self):
        return True

    def clear(self):
        self.cleared = True

    def compact(self):
        return True


def test_engine_queues_deduplicates_and_replies(tmp_path):
    store = AccountStore(tmp_path / "account.json")
    account = _account(tmp_path)
    store.save(account)
    client = _FakeClient()
    session = _FakeSession()
    engine = WechatMessageEngine(
        account=account,
        account_store=store,
        client=client,
        agent_session=session,
    )

    raw = _raw_message()
    engine._handle_raw_message(raw)
    engine._handle_raw_message(raw)
    engine._start_next_if_possible()
    for _ in range(100):
        if engine.current and engine.current.future.done():
            break
        time.sleep(0.005)
    engine._complete_current_if_ready()
    engine.stop()

    assert session.inputs == ["hello"]
    assert len(client.sent) == 1
    assert client.sent[0]["context_token"] == "ctx-1"
    assert client.sent[0]["text"] == "reply: hello"


def test_engine_ignores_unbound_user(tmp_path):
    store = AccountStore(tmp_path / "account.json")
    account = _account(tmp_path)
    store.save(account)
    engine = WechatMessageEngine(
        account=account,
        account_store=store,
        client=_FakeClient(),
        agent_session=_FakeSession(),
    )

    engine._handle_raw_message(_raw_message(sender="intruder"))

    assert len(engine.queue) == 0
    engine.stop()
