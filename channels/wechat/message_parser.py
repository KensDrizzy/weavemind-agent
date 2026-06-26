"""Convert raw iLink messages into channel-neutral inbound messages."""

from __future__ import annotations

from typing import Any, Optional

from channels.wechat.models import (
    InboundAttachment,
    InboundMessage,
)

MESSAGE_TYPE_USER = 1
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5


def parse_inbound_message(raw: dict[str, Any]) -> Optional[InboundMessage]:
    if int(raw.get("message_type") or 0) != MESSAGE_TYPE_USER:
        return None

    sender_id = str(raw.get("from_user_id") or "")
    recipient_id = str(raw.get("to_user_id") or "")
    context_token = str(raw.get("context_token") or "")
    message_id = str(
        raw.get("message_id")
        or raw.get("client_id")
        or raw.get("seq")
        or ""
    )

    if not sender_id or not context_token or not message_id:
        return None

    text_parts: list[str] = []
    attachments: list[InboundAttachment] = []

    for item in raw.get("item_list") or []:
        item_type = int(item.get("type") or 0)
        if item_type == ITEM_TEXT:
            text = str((item.get("text_item") or {}).get("text") or "").strip()
            if text:
                text_parts.append(text)
        elif item_type == ITEM_VOICE:
            voice = item.get("voice_item") or {}
            transcript = str(voice.get("text") or "").strip()
            if transcript:
                text_parts.append(transcript)
            attachments.append(InboundAttachment(kind="voice", raw=item))
        elif item_type == ITEM_IMAGE:
            attachments.append(InboundAttachment(kind="image", raw=item))
        elif item_type == ITEM_FILE:
            file_item = item.get("file_item") or {}
            attachments.append(
                InboundAttachment(
                    kind="file",
                    name=str(file_item.get("file_name") or "") or None,
                    raw=item,
                )
            )
        elif item_type == ITEM_VIDEO:
            attachments.append(InboundAttachment(kind="video", raw=item))

    return InboundMessage(
        message_id=message_id,
        sender_id=sender_id,
        recipient_id=recipient_id,
        context_token=context_token,
        text="\n".join(text_parts).strip(),
        created_at_ms=int(raw.get("create_time_ms") or 0),
        group_id=str(raw.get("group_id") or "") or None,
        attachments=tuple(attachments),
        raw=raw,
    )
