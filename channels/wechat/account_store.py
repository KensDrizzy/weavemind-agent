"""Secure local persistence for WeChat iLink credentials."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from channels.wechat.models import WechatAccount


class AccountStore:
    def __init__(self, path: str | Path = "~/.weavemind/wechat/account.json"):
        self.path = Path(path).expanduser()

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> Optional[WechatAccount]:
        if not self.exists():
            return None
        data = json.loads(self.path.read_text(encoding="utf-8"))
        account = WechatAccount.from_dict(data)
        account.validate()
        return account

    def save(self, account: WechatAccount) -> None:
        account.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        payload = json.dumps(account.to_dict(), ensure_ascii=False, indent=2)

        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            dir=str(self.path.parent),
            text=True,
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def update_cursor(self, cursor: str) -> None:
        account = self.load()
        if not account:
            raise FileNotFoundError(str(self.path))
        account.get_updates_buf = cursor
        self.save(account)

    def delete(self) -> None:
        if self.path.exists():
            self.path.unlink()

    @staticmethod
    def redact(value: str) -> str:
        if not value:
            return "(empty)"
        if len(value) <= 8:
            return "***"
        return f"{value[:4]}...{value[-4:]}"
