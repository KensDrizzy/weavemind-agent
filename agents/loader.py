"""Agent 定义加载器 — 从 .weavemind/agents/*.md 加载 YAML frontmatter。

支持字段：name, description, model, tools, role, permissionMode, system_prompt
"""

import yaml
import os
from pathlib import Path


def load_agent_def(path: str) -> dict:
    """从 .md 文件加载 Agent 定义（YAML frontmatter + 正文）。

    Args:
        path: Agent 定义文件路径

    Returns:
        包含 name, model, tools, role, system_prompt 等字段的字典
    """
    text = Path(path).read_text()
    if not text.startswith("---"):
        return {"name": Path(path).stem, "system_prompt": text, "tools": []}

    parts = text.split("---", 2)
    meta = yaml.safe_load(parts[1]) if len(parts) > 1 else {}
    meta["system_prompt"] = parts[2].strip() if len(parts) > 2 else ""
    return meta


def load_agents_from_dir(directory: str) -> list[dict]:
    """从目录加载所有 Agent 定义。

    Args:
        directory: Agent 定义目录路径

    Returns:
        Agent 定义字典列表
    """
    if not os.path.isdir(directory):
        return []
    return [load_agent_def(str(p)) for p in Path(directory).glob("*.md")]


def load_agents_by_role(directory: str) -> dict[str, list[dict]]:
    """按 role 分组加载 Agent 定义。

    Args:
        directory: Agent 定义目录路径

    Returns:
        {role: [agent_def, ...]} 字典，无 role 的归入 "default" 组
    """
    agents = load_agents_from_dir(directory)
    by_role: dict[str, list[dict]] = {}
    for agent in agents:
        role = agent.get("role", "default")
        by_role.setdefault(role, []).append(agent)
    return by_role
