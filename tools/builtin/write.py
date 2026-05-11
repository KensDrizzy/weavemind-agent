from tools.base import WeaveMindTool
import os


class WriteTool(WeaveMindTool):
    name: str = "Write"
    description: str = "Create or overwrite a file. Args: path, content"

    def _run(self, path: str, content: str) -> str:
        path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return f"Written: {path}"
