"""会话管理 — 完整对话的持久化与切换。

每次启动 CLI 开启新会话（uuid id）；每轮结束把 conversation 落盘到
`.weavemind/sessions/<id>.json`，/sessions 可列出并切换历史会话。

存储格式：{"id", "created_at", "updated_at", "title", "messages", "token_totals"}。
图片 payload（base64）保存前剥离为占位文本，避免会话文件膨胀。
"""

import json
import logging
import os
import time
import uuid
from typing import Optional

from langchain_core.messages import HumanMessage

import settings

logger = logging.getLogger(__name__)

_IMAGE_PART_TYPES = {"image_url", "image", "input_image"}


class SessionManager:
    def __init__(self, storage_dir: Optional[str] = None):
        self.storage_dir = storage_dir or settings.get(
            "session.storage_dir", ".weavemind/sessions"
        )
        os.makedirs(self.storage_dir, exist_ok=True)

    def create(self) -> str:
        return uuid.uuid4().hex

    def save(self, session_id: str, conversation: list, token_totals: Optional[dict] = None):
        """保存完整会话；已有文件保留 created_at。"""
        from langchain_core.messages import messages_to_dict

        path = self._path(session_id)
        created_at = time.time()
        if os.path.exists(path):
            try:
                with open(path) as f:
                    created_at = json.load(f).get("created_at", created_at)
            except (json.JSONDecodeError, OSError):
                pass

        payload = {
            "id": session_id,
            "created_at": created_at,
            "updated_at": time.time(),
            "title": self._make_title(conversation),
            "messages": _strip_image_payloads(messages_to_dict(list(conversation))),
            "token_totals": token_totals or {"input": 0, "output": 0, "total": 0},
        }
        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp_path, path)  # 原子写，避免中途崩溃留下半个文件

    def resume(self, session_id: str):
        """加载会话，返回 (messages, meta)；不存在或旧格式返回 (None, None)。"""
        from langchain_core.messages import messages_from_dict

        path = self._path(session_id)
        if not os.path.exists(path):
            return None, None
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None, None
        raw = data.get("messages")
        if raw is None:
            return None, None
        messages = messages_from_dict(raw)
        meta = {
            "id": data.get("id", session_id),
            "created_at": data.get("created_at", 0),
            "updated_at": data.get("updated_at", 0),
            "title": data.get("title", ""),
            "token_totals": data.get("token_totals") or {},
        }
        return messages, meta

    def list(self) -> list[dict]:
        """会话摘要（按更新时间倒序）；跳过无 messages 的旧格式元数据文件。"""
        items = []
        for fname in os.listdir(self.storage_dir):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.storage_dir, fname)) as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            if not data.get("messages"):
                continue
            items.append({
                "id": data.get("id", fname[:-5]),
                "title": data.get("title") or "(无标题)",
                "created_at": data.get("created_at", 0),
                "updated_at": data.get("updated_at", 0),
                "message_count": len(data["messages"]),
                "token_totals": data.get("token_totals") or {},
            })
        items.sort(key=lambda x: x["updated_at"], reverse=True)
        return items

    def resolve(self, arg: str) -> Optional[str]:
        """按 /sessions 列表序号（1 基）或 id 前缀定位会话 id。"""
        sessions = self.list()
        if arg.isdigit():
            idx = int(arg) - 1
            return sessions[idx]["id"] if 0 <= idx < len(sessions) else None
        matches = [s["id"] for s in sessions if s["id"].startswith(arg)]
        return matches[0] if len(matches) == 1 else None

    def _path(self, session_id: str) -> str:
        return os.path.join(self.storage_dir, f"{session_id}.json")

    @staticmethod
    def _make_title(conversation: list) -> str:
        from core.multimodal.content_part import content_to_text
        for m in conversation:
            if isinstance(m, HumanMessage):
                text = content_to_text(m.content).strip().replace("\n", " ")
                if text:
                    return text[:40]
        return "(无标题)"


def _strip_image_payloads(message_dicts: list[dict]) -> list[dict]:
    """把消息中的图片 content part 替换为占位文本。"""
    for d in message_dicts:
        content = d.get("data", {}).get("content")
        if not isinstance(content, list):
            continue
        d["data"]["content"] = [
            {"type": "text", "text": "[图片]"}
            if isinstance(part, dict) and part.get("type") in _IMAGE_PART_TYPES
            else part
            for part in content
        ]
    return message_dicts
