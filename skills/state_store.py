"""SkillStateStore — 持久化 disabled 列表。"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class SkillStateStore:
    def __init__(self, file_path: Path):
        self.file_path = file_path

    def disabled(self) -> set[str]:
        if not self.file_path.exists():
            return set()
        try:
            data = json.loads(self.file_path.read_text(encoding="utf-8"))
            return set(data.get("disabled", []))
        except Exception:
            return set()

    def disable(self, name: str) -> None:
        d = self.disabled()
        d.add(name)
        self._write(d)

    def enable(self, name: str) -> None:
        d = self.disabled()
        d.discard(name)
        self._write(d)

    def _write(self, disabled: set[str]) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.file_path.write_text(json.dumps({"disabled": sorted(disabled)}, ensure_ascii=False, indent=2), encoding="utf-8")
