from tools.base import WeaveMindTool
import glob as _glob


class GlobTool(WeaveMindTool):
    name: str = "Glob"
    description: str = "Find files matching a pattern. Args: pattern, root (optional)"

    def _run(self, pattern: str, root: str = ".") -> str:
        matches = _glob.glob(f"{root}/{pattern}", recursive=True)
        return "\n".join(matches) if matches else "(no matches)"
