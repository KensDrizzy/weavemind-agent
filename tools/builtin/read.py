from tools.base import WeaveMindTool
from pydantic import Field
import os


class ReadTool(WeaveMindTool):
    name: str = "Read"
    description: str = "Read a file or list a directory. Args: path, offset (optional), limit (optional)"

    def _run(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        path = os.path.expanduser(path)
        if os.path.isdir(path):
            return "\n".join(os.listdir(path))
        if not os.path.exists(path):
            return f"Error: {path} not found"
        with open(path) as f:
            lines = f.readlines()
        lines = lines[offset:offset + limit]
        return "".join(f"{offset+i+1}\t{l}" for i, l in enumerate(lines))
