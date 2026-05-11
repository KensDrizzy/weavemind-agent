from tools.base import WeaveMindTool
import os


class EditTool(WeaveMindTool):
    name: str = "Edit"
    description: str = "Replace exact string in a file. Args: path, old_string, new_string"

    def _run(self, path: str, old_string: str, new_string: str) -> str:
        path = os.path.expanduser(path)
        with open(path) as f:
            content = f.read()
        if old_string not in content:
            return f"Error: old_string not found in {path}"
        with open(path, "w") as f:
            f.write(content.replace(old_string, new_string, 1))
        return f"Edited: {path}"
