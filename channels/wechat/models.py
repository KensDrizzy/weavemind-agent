"""Models used by the WeChat channel."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class WechatAccount:
    bot_token: str
    bot_id: str
    bound_user_id: str
    base_url: str
    workspace: str
    get_updates_buf: str = ""
    schema_version: int = 1
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        self.updated_at = datetime.now(timezone.utc).isoformat()
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WechatAccount":
        return cls(
            bot_token=str(data.get("bot_token") or data.get("token") or ""),
            bot_id=str(data.get("bot_id") or data.get("account_id") or ""),
            bound_user_id=str(data.get("bound_user_id") or data.get("user_id") or ""),
            base_url=str(data.get("base_url") or "https://ilinkai.weixin.qq.com"),
            workspace=str(data.get("workspace") or ""),
            get_updates_buf=str(
                data.get("get_updates_buf") or data.get("sync_buf") or ""
            ),
            schema_version=int(data.get("schema_version", 1)),
            created_at=str(data.get("created_at") or datetime.now(timezone.utc).isoformat()),
            updated_at=str(data.get("updated_at") or datetime.now(timezone.utc).isoformat()),
        )

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("bot_token", self.bot_token),
                ("bot_id", self.bot_id),
                ("bound_user_id", self.bound_user_id),
                ("base_url", self.base_url),
                ("workspace", self.workspace),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"WeChat account is missing fields: {', '.join(missing)}")


@dataclass(frozen=True)
class InboundAttachment:
    kind: str
    name: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InboundMessage:
    message_id: str
    sender_id: str
    recipient_id: str
    context_token: str
    text: str
    created_at_ms: int = 0
    group_id: Optional[str] = None
    attachments: tuple[InboundAttachment, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def is_group(self) -> bool:
        return bool(self.group_id)


@dataclass(frozen=True)
class PollResult:
    messages: tuple[dict[str, Any], ...]
    get_updates_buf: str
    timeout_ms: Optional[int] = None
