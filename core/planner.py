"""规划器 — 调用 LLM 将用户目标分解为 DAG 结构的执行计划。

使用结构化输出确保 LLM 返回符合 Plan 模型的 JSON。
"""

from langchain_core.messages import HumanMessage, SystemMessage
from core.llm_factory import create_llm
from core.plan_models import Plan, Task
import json
import logging

logger = logging.getLogger(__name__)

PLANNING_SYSTEM_PROMPT = """你是一个任务规划专家。你的职责是将用户的复杂目标分解为可执行的原子任务，并组织成 DAG（有向无环图）结构。

## 输出格式

你必须输出一个 JSON 对象，格式如下：

```json
{
  "goal": "用户的原始目标",
  "tasks": [
    {
      "id": "task_1",
      "description": "任务描述（清晰、具体、可执行）",
      "tool_name": "工具名称（可选，如 Read、Write、Edit、Bash、Glob、Grep、WebFetch、WebSearch）",
      "tool_args": {"参数key": "参数value"}（可选，如果指定了 tool_name 则应提供参数）,
      "dependencies": ["依赖的task_id列表"]
    }
  ]
}
```

## 规划原则

1. **原子性**：每个任务应该是最小可执行单元，一个任务只做一件事
2. **依赖明确**：只有真正有先后顺序的任务才设置依赖，无依赖的任务可以并行
3. **工具匹配**：根据任务性质选择合适的工具，不确定时可以不指定 tool_name
4. **ID 规范**：使用 task_1, task_2, task_3... 的递增编号
5. **描述清晰**：任务描述要让执行者明确知道要做什么，不要模糊

## 可用工具

- Read: 读取文件内容
- Write: 写入文件
- Edit: 编辑文件（字符串替换）
- Bash: 执行 shell 命令
- Glob: 搜索文件路径
- Grep: 搜索文件内容
- WebFetch: 获取网页内容
- WebSearch: 搜索互联网信息

## 示例

用户目标："分析项目结构并生成 README"

```json
{
  "goal": "分析项目结构并生成 README",
  "tasks": [
    {
      "id": "task_1",
      "description": "列出项目根目录下的所有文件和目录",
      "tool_name": "Bash",
      "tool_args": {"command": "ls -la"},
      "dependencies": []
    },
    {
      "id": "task_2",
      "description": "查找项目中所有的 Python 源文件",
      "tool_name": "Glob",
      "tool_args": {"pattern": "**/*.py"},
      "dependencies": []
    },
    {
      "id": "task_3",
      "description": "读取 main.py 了解项目入口",
      "tool_name": "Read",
        "tool_args": {"path": "main.py"},
      "dependencies": ["task_2"]
    },
    {
      "id": "task_4",
      "description": "根据收集的信息编写 README.md",
      "tool_name": "Write",
        "tool_args": {"path": "README.md", "content": "待执行时根据前序任务结果填充"},
      "dependencies": ["task_1", "task_3"]
    }
  ]
}
```

注意：task_1 和 task_2 无依赖可以并行执行；task_3 依赖 task_2；task_4 依赖 task_1 和 task_3。
"""


class Planner:
    """调用 LLM 生成结构化执行计划。"""

    def __init__(self, provider: str = None, model: str = None):
        self.llm = create_llm(provider, model)

    def create_plan(self, goal: str) -> Plan:
        """将用户目标分解为 DAG 执行计划。

        Args:
            goal: 用户的目标描述

        Returns:
            Plan: 结构化的执行计划
        """
        messages = [
            SystemMessage(content=PLANNING_SYSTEM_PROMPT),
            HumanMessage(content=f"请为以下目标制定执行计划：\n{goal}"),
        ]

        from core.llm_retry import call_with_retry
        response = call_with_retry(
            lambda: self.llm.invoke(messages),
            description="计划生成",
        )
        raw_content = response.content

        # 提取 JSON（LLM 可能包裹在 markdown code block 中）
        json_str = self._extract_json(raw_content)
        plan_data = json.loads(json_str)

        # 构建并验证 Plan 对象  Pydantic 逐个校验 Task
        tasks = [Task(**t) for t in plan_data.get("tasks", [])]
        plan = Plan(
            goal=plan_data.get("goal", goal),
            tasks=tasks,
        )

        # 验证 DAG 无循环依赖  DFS 检测循环依赖
        self._validate_dag(plan)

        logger.info(f"计划生成完成: {plan.id}, 共 {len(plan.tasks)} 个任务")
        return plan

    def _extract_json(self, content: str) -> str:
        """从 LLM 输出中提取 JSON 字符串。"""
        # 尝试提取 markdown code block 中的 JSON
        import re
        match = re.search(r"```(?:json)?\s*\n([\s\S]*?)\n```", content)
        if match:
            return match.group(1).strip()

        # 尝试直接解析整个内容
        stripped = content.strip()
        if stripped.startswith("{"):
            return stripped

        # 最后尝试找到第一个 { 到最后一个 } 之间的内容
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1:
            return content[start:end + 1]

        raise ValueError(f"无法从 LLM 输出中提取 JSON: {content[:200]}")

    def _validate_dag(self, plan: Plan):
        """验证 DAG 无循环依赖，且所有依赖引用有效。"""
        task_ids = {t.id for t in plan.tasks}

        for task in plan.tasks:
            for dep in task.dependencies:
                if dep not in task_ids:
                    raise ValueError(f"任务 {task.id} 引用了不存在的依赖 {dep}")

        # DFS 检测环
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {t.id: WHITE for t in plan.tasks}
        task_map = {t.id: t for t in plan.tasks}

        def dfs(tid):
            color[tid] = GRAY
            for dep in task_map[tid].dependencies:
                if color[dep] == GRAY:
                    raise ValueError(f"DAG 中存在循环依赖: {tid} -> {dep}")
                if color[dep] == WHITE:
                    dfs(dep)
            color[tid] = BLACK

        for tid in task_ids:
            if color[tid] == WHITE:
                dfs(tid)
