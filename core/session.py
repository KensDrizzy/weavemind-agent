import json
import uuid
import os
import settings
from typing import Optional  


class SessionManager:
    def __init__(self):
        self.storage_dir = settings.get("session.storage_dir", ".weavemind/sessions")
        os.makedirs(self.storage_dir, exist_ok=True)

    def create(self) -> str:
        return str(uuid.uuid4())

    def save(self, session_id: str, state: dict):
        path = os.path.join(self.storage_dir, f"{session_id}.json")
        with open(path, "w") as f:
            json.dump(state, f, default=str)

    def resume(self, session_id: str) -> Optional[dict]:
        path = os.path.join(self.storage_dir, f"{session_id}.json")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)

    def list(self) -> list[str]:
        return [f[:-5] for f in os.listdir(self.storage_dir) if f.endswith(".json")]
