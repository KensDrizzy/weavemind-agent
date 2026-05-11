from tools.base import WeaveMindTool
import subprocess


class GrepTool(WeaveMindTool):
    name: str = "Grep"
    description: str = "Search file contents with regex. Args: pattern, path, flags (optional)"

    def _run(self, pattern: str, path: str = ".", flags: str = "-r") -> str:
        result = subprocess.run(
            ["grep", flags, "-n", "--include=*", pattern, path],
            capture_output=True, text=True
        )
        return result.stdout or "(no matches)"
