"""Remote-safe tool scoping for the WeChat channel."""

from __future__ import annotations

import glob as globlib
import os
import re
from pathlib import Path

from pydantic import Field

from tools.base import WeaveMindTool
from tools.builtin.glob import GlobTool
from tools.builtin.read import ReadTool


DEFAULT_SAFE_TOOLS = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "SearchCode",
        "WebSearch",
        "WebFetch",
        "MemorySearch",
        "load_skill",
    }
)


class WorkspaceViolationError(PermissionError):
    pass


class WorkspaceGuard:
    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).expanduser().resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"workspace does not exist: {self.workspace}")

    def resolve(self, value: str | Path) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        resolved = candidate.resolve(strict=False)
        try:
            common = os.path.commonpath([str(self.workspace), str(resolved)])
        except ValueError as exc:
            raise WorkspaceViolationError(str(value)) from exc
        if common != str(self.workspace):
            raise WorkspaceViolationError(
                f"path escapes bound workspace: {value}"
            )
        return resolved


class ScopedReadTool(ReadTool):
    workspace: str = Field(exclude=True)

    def _run(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        resolved = WorkspaceGuard(self.workspace).resolve(path)
        return super()._run(str(resolved), offset=offset, limit=limit)


class ScopedGlobTool(GlobTool):
    workspace: str = Field(exclude=True)

    def _run(self, pattern: str, root: str = ".") -> str:
        guard = WorkspaceGuard(self.workspace)
        resolved_root = guard.resolve(root)
        matches = globlib.glob(str(resolved_root / pattern), recursive=True)
        safe_matches = []
        for match in matches:
            try:
                resolved = guard.resolve(match)
            except WorkspaceViolationError:
                continue
            safe_matches.append(str(resolved))
        return "\n".join(safe_matches) if safe_matches else "(no matches)"


class ScopedGrepTool(WeaveMindTool):
    name: str = "Grep"
    description: str = (
        "Search file contents with a Python regular expression inside the bound "
        "workspace. Args: pattern, path (optional)"
    )
    workspace: str = Field(exclude=True)
    max_results: int = Field(default=500, exclude=True)
    max_file_bytes: int = Field(default=2 * 1024 * 1024, exclude=True)

    def _run(self, pattern: str, path: str = ".", flags: str = "-r") -> str:
        del flags  # retained for compatibility with the terminal Grep schema
        guard = WorkspaceGuard(self.workspace)
        target = guard.resolve(path)
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return f"Error: invalid regex: {exc}"

        files = [target] if target.is_file() else self._iter_files(target)
        results: list[str] = []
        for file_path in files:
            if len(results) >= self.max_results:
                break
            try:
                safe_file = guard.resolve(file_path)
                if safe_file.stat().st_size > self.max_file_bytes:
                    continue
                with safe_file.open("r", encoding="utf-8", errors="ignore") as handle:
                    for line_no, line in enumerate(handle, 1):
                        if regex.search(line):
                            results.append(f"{safe_file}:{line_no}:{line.rstrip()}")
                            if len(results) >= self.max_results:
                                break
            except (OSError, UnicodeError):
                continue
        return "\n".join(results) if results else "(no matches)"

    @staticmethod
    def _iter_files(root: Path):
        excluded = {".git", ".weavemind", "__pycache__", "node_modules", ".venv"}
        for current, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if name not in excluded]
            for name in files:
                yield Path(current) / name


class FilteredToolRegistry:
    """Expose only explicitly allowed tools from an existing registry."""

    def __init__(self, base_registry, allowed: set[str] | frozenset[str]):
        self.base_registry = base_registry
        self.allowed = frozenset(allowed)

    def get(self, name: str):
        if name not in self.allowed:
            return None
        return self.base_registry.get(name)

    def get_all(self) -> list:
        return [
            tool
            for tool in self.base_registry.get_all()
            if getattr(tool, "name", "") in self.allowed
        ]

    def get_langchain_tools(self) -> list:
        return self.get_all()
