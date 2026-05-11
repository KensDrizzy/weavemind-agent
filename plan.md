# 自动识别任务复杂度并触发 Team 模式

## 目标

在普通模式下，Agent 自动评估用户任务的复杂度，复杂任务自动切换到 Team 模式（Supervisor→Planner→Worker→Reviewer），无需用户手动 `/team`。

## 方案

在 `AgentLoop` 的 `run()` 方法中，think 节点之后、route 节点之前，新增一个 `complexity_check` 节点：

1. 用 LLM 快速判断任务复杂度（单次调用，返回 simple/complex）
2. simple → 走原有 ReAct/Plan-Execute 路径
3. complex → 设置 `state["use_team"] = True`，route 节点据此跳转到 team 执行路径

## 修改文件

### 1. `core/agent_loop.py`

- 在 `AgentState` 中新增 `use_team: bool` 字段
- 新增 `complexity_check` 节点方法：
  - 构造简短 prompt，让 LLM 返回 `simple` 或 `complex`
  - 判断依据：是否涉及多步骤、多文件创建、项目级操作、需要审查等
  - 使用 haiku 模型降低延迟和成本
- 修改 `route` 节点：检查 `state["use_team"]`，若为 True 则返回 `"team"` 路由
- 修改 `_build_graph`：添加 `complexity_check` 节点和边
- 新增 `_run_team_in_graph` 方法：在图内调用 `MultiAgentOrchestrator`，将结果写回 state
- 在 `_build_graph` 中添加 `"team"` → `"_run_team_in_graph"` → `"END"` 的边

### 2. `cli/app.py`

- `_run_multi_agent` 保持不变（手动 /team 仍可用）
- `process_input` 中 `self.team_mode` 判断保持不变（手动覆盖优先）

### 3. `config.yaml`

- 新增 `team.auto_detect: true` 配置项，允许用户关闭自动识别

### 4. `tests/test_multiagent.py`

- 新增测试：`test_complexity_check_simple`、`test_complexity_check_complex`、`test_auto_team_routing`

## 数据流

```
用户输入 → think → complexity_check → route
  ├─ simple → [原有 ReAct/Plan-Execute 路径]
  ├─ complex → team → _run_team_in_graph → END
  └─ 无 tool_calls → END
```

## 复杂度判断 Prompt

```
判断以下任务的复杂度。只回复 simple 或 complex：
- simple：简单查询、单步操作、单文件修改、简单问答
- complex：多步骤任务、创建项目、多文件修改、需要规划和审查的任务

任务：{user_input}
```

## 成本控制

- 使用 haiku 模型做复杂度判断，单次调用约 50 tokens
- 可通过 `config.yaml` 中 `team.auto_detect: false` 关闭
