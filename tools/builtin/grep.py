from tools.base import WeaveMindTool
import subprocess


class GrepTool(WeaveMindTool):
    name: str = "Grep"
    description: str = (
        "Search file contents with regex. "
        "Args: pattern, path (optional), glob (optional include pattern like '*.py')"
    )

    def _run(self, pattern: str, path: str = ".", glob: str = "*", flags: str = "-r") -> str:
        include = glob or "*"
        result = subprocess.run(
            ["grep", flags, "-n", f"--include={include}", pattern, path],
            capture_output=True, text=True
        )
        if result.returncode > 1:
            return f"Error: grep 执行失败（退出码 {result.returncode}）：{result.stderr}"
        return result.stdout or "(no matches)"
