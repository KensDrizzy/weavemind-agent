from tools.base import WeaveMindTool
import subprocess


class BashTool(WeaveMindTool):
    name: str = "Bash"
    description: str = (
        "Execute a shell command on the system. "
        "Use for: creating directories (mkdir), running scripts, git operations, "
        "installing packages, file management, and any system task not covered by other tools. "
        "Args: command (required), timeout (optional, seconds)"
    )

    def _run(self, command: str, timeout: int = 120) -> str:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout
        )
        out = result.stdout
        if result.returncode != 0:
            out += f"\n[stderr]\n{result.stderr}"
        return out or "(no output)"
