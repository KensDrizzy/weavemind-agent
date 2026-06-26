"""Long-poll message engine for the WeChat iLink channel."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict, deque
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from channels.wechat.account_store import AccountStore
from channels.wechat.commands import WechatCommand, parse_command
from channels.wechat.ilink_client import ILinkClient, SessionExpiredError
from channels.wechat.message_parser import parse_inbound_message
from channels.wechat.models import InboundMessage, WechatAccount
from channels.wechat.renderer import WechatRenderer

logger = logging.getLogger(__name__)


class MessageDeduplicator:
    def __init__(self, max_entries: int = 2000):
        self.max_entries = max_entries
        self._seen: OrderedDict[str, None] = OrderedDict()

    def add_if_new(self, message_id: str) -> bool:
        if message_id in self._seen:
            self._seen.move_to_end(message_id)
            return False
        self._seen[message_id] = None
        while len(self._seen) > self.max_entries:
            self._seen.popitem(last=False)
        return True


@dataclass
class _RunningTask:
    message: InboundMessage
    future: Future
    typing_ticket: str = ""
    last_typing_at: float = 0.0


class WechatMessageEngine:
    def __init__(
        self,
        *,
        account: WechatAccount,
        account_store: AccountStore,
        client: ILinkClient,
        agent_session,
        renderer: Optional[WechatRenderer] = None,
        queue_max_size: int = 20,
        poll_timeout_seconds: float = 35,
        busy_poll_timeout_seconds: float = 3,
        typing_refresh_seconds: float = 5,
        private_chat_only: bool = True,
    ):
        account.validate()
        self.account = account
        self.account_store = account_store
        self.client = client
        self.agent_session = agent_session
        self.renderer = renderer or WechatRenderer()
        self.queue_max_size = queue_max_size
        self.poll_timeout_seconds = poll_timeout_seconds
        self.busy_poll_timeout_seconds = busy_poll_timeout_seconds
        self.typing_refresh_seconds = typing_refresh_seconds
        self.private_chat_only = private_chat_only

        self.queue: deque[InboundMessage] = deque()
        self.deduplicator = MessageDeduplicator()
        self.executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="weavemind-wechat-agent",
        )
        self.current: Optional[_RunningTask] = None
        self.paused = False
        self._stop_requested = False
        self._consecutive_failures = 0

    def run_forever(self, *, max_polls: Optional[int] = None) -> None:
        polls = 0
        self._best_effort(self.client.notify_start)
        try:
            while not self._stop_requested:
                self._complete_current_if_ready()
                self._start_next_if_possible()
                self._refresh_typing_if_needed()

                timeout = (
                    self.busy_poll_timeout_seconds
                    if self.current is not None
                    else self.poll_timeout_seconds
                )
                try:
                    result = self.client.get_updates(
                        self.account.get_updates_buf,
                        timeout_seconds=timeout,
                    )
                    self._consecutive_failures = 0
                except SessionExpiredError:
                    raise
                except Exception as exc:
                    self._consecutive_failures += 1
                    delay = min(30, 2 ** min(self._consecutive_failures, 5))
                    logger.warning(
                        "WeChat long-poll failed (%s); retrying in %ss",
                        exc,
                        delay,
                    )
                    time.sleep(delay)
                    continue

                polls += 1
                if result.get_updates_buf != self.account.get_updates_buf:
                    self.account.get_updates_buf = result.get_updates_buf
                    self.account_store.save(self.account)

                if result.timeout_ms and self.current is None:
                    self.poll_timeout_seconds = max(
                        3,
                        min(result.timeout_ms / 1000, 60),
                    )

                for raw_message in result.messages:
                    self._handle_raw_message(raw_message)

                if max_polls is not None and polls >= max_polls:
                    break
        finally:
            self.stop()

    def request_stop(self) -> None:
        self._stop_requested = True

    def stop(self) -> None:
        self._stop_requested = True
        if self.current:
            self.agent_session.cancel()
            self.current.future.cancel()
            self._cancel_typing(self.current)
        self.executor.shutdown(wait=False, cancel_futures=True)
        self._best_effort(self.client.notify_stop)

    def status_text(self) -> str:
        state = "运行中" if self.current else "空闲"
        if self.paused:
            state = "已暂停"
        return (
            f"WeaveMind 微信通道\n"
            f"状态：{state}\n"
            f"排队消息：{len(self.queue)}\n"
            f"工作区：{self.account.workspace}\n"
            f"安全模式：remote_safe（只读）"
        )

    def _handle_raw_message(self, raw_message: dict) -> None:
        message = parse_inbound_message(raw_message)
        if not message:
            return
        if not self.deduplicator.add_if_new(message.message_id):
            logger.debug("Ignoring duplicate WeChat message %s", message.message_id)
            return
        if message.sender_id != self.account.bound_user_id:
            logger.warning("Ignoring message from unbound WeChat user")
            return
        if self.private_chat_only and message.is_group:
            logger.info("Ignoring group message in private-chat-only mode")
            return

        command = parse_command(message.text)
        if command:
            self._handle_command(message, command)
            return

        if not message.text:
            self._send_text(
                message,
                "当前版本只支持文本和语音转文字，图片、文件和视频将在后续版本支持。",
            )
            return

        if len(self.queue) >= self.queue_max_size:
            self._send_text(message, "消息队列已满，请稍后再试。")
            return

        self.queue.append(message)

    def _handle_command(
        self,
        message: InboundMessage,
        command: WechatCommand,
    ) -> None:
        if command.name == "/help":
            self._send_text(
                message,
                "可用命令：\n"
                "/status 查看状态\n"
                "/clear 清空对话\n"
                "/compact 压缩上下文\n"
                "/pause 暂停普通消息消费\n"
                "/resume 恢复消费\n"
                "/stop 取消当前任务",
            )
        elif command.name == "/status":
            self._send_text(message, self.status_text())
        elif command.name == "/pause":
            self.paused = True
            self._send_text(message, "消息消费已暂停；新消息仍会进入队列。")
        elif command.name == "/resume":
            self.paused = False
            self._send_text(message, "消息消费已恢复。")
        elif command.name == "/stop":
            if not self.current:
                self._send_text(message, "当前没有正在运行的任务。")
                return
            cancelled = self.agent_session.cancel()
            cancelled = self.current.future.cancel() or cancelled
            self._send_text(
                message,
                "已请求取消当前任务。" if cancelled else "当前任务已接近结束。",
            )
        elif command.name == "/clear":
            if self.current:
                self._send_text(message, "任务运行期间不能清空会话，请先发送 /stop。")
                return
            self.agent_session.clear()
            self._send_text(message, "当前微信会话历史已清空。")
        elif command.name == "/compact":
            if self.current:
                self._send_text(message, "任务运行期间不能压缩会话，请稍后再试。")
                return
            changed = self.agent_session.compact()
            self._send_text(
                message,
                "上下文已压缩。" if changed else "当前上下文无需压缩。",
            )

    def _start_next_if_possible(self) -> None:
        if self.paused or self.current is not None or not self.queue:
            return
        message = self.queue.popleft()
        future = self.executor.submit(self.agent_session.run, message.text)
        task = _RunningTask(message=message, future=future)
        self.current = task
        self._start_typing(task)

    def _complete_current_if_ready(self) -> None:
        task = self.current
        if not task or not task.future.done():
            return
        self._cancel_typing(task)
        try:
            result = task.future.result()
            if result.cancelled:
                text = "任务已取消。"
            elif result.success:
                text = result.text
            else:
                text = f"任务执行失败：{result.error or '未知错误'}"
        except CancelledError:
            text = "任务已取消。"
        except Exception as exc:
            logger.exception("WeChat Agent worker failed")
            text = f"任务执行失败：{exc}"

        self._send_text(task.message, text)
        self.current = None

    def _start_typing(self, task: _RunningTask) -> None:
        try:
            task.typing_ticket = self.client.get_typing_ticket(
                user_id=task.message.sender_id,
                context_token=task.message.context_token,
            )
            if task.typing_ticket:
                self.client.send_typing(
                    user_id=task.message.sender_id,
                    typing_ticket=task.typing_ticket,
                    typing=True,
                )
                task.last_typing_at = time.monotonic()
        except Exception as exc:
            logger.debug("Failed to start WeChat typing indicator: %s", exc)

    def _refresh_typing_if_needed(self) -> None:
        task = self.current
        if not task or not task.typing_ticket:
            return
        if time.monotonic() - task.last_typing_at < self.typing_refresh_seconds:
            return
        try:
            self.client.send_typing(
                user_id=task.message.sender_id,
                typing_ticket=task.typing_ticket,
                typing=True,
            )
            task.last_typing_at = time.monotonic()
        except Exception as exc:
            logger.debug("Failed to refresh WeChat typing indicator: %s", exc)

    def _cancel_typing(self, task: _RunningTask) -> None:
        if not task.typing_ticket:
            return
        try:
            self.client.send_typing(
                user_id=task.message.sender_id,
                typing_ticket=task.typing_ticket,
                typing=False,
            )
        except Exception as exc:
            logger.debug("Failed to cancel WeChat typing indicator: %s", exc)

    def _send_text(self, message: InboundMessage, text: str) -> None:
        for chunk in self.renderer.render(text):
            self.client.send_message(
                to_user_id=message.sender_id,
                context_token=message.context_token,
                text=chunk,
            )

    @staticmethod
    def _best_effort(callback) -> None:
        try:
            callback()
        except Exception as exc:
            logger.debug("Best-effort WeChat operation failed: %s", exc)
