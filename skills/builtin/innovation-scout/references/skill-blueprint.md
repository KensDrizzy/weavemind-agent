# Skill Blueprint

用于把创新方向转成 WeaveMind Skill。

## 必填结构

```markdown
---
name: short-hyphen-name
description: |
  说明这个 Skill 做什么，以及用户说什么时应该触发。触发信息必须写在 description。
---

# Skill Title

## 目标

说明这个 Skill 帮 Agent 稳定完成什么任务。

## 工作流

1. 第一步
2. 第二步
3. 第三步

## 工具策略

- 何时读代码、何时搜索、何时使用浏览器或 MCP。
- 失败时如何降级。

## 输出格式

给出 Agent 应交付的结构。

## 反模式

- 列出不要做的事。
```

## 设计准则

- `name` 使用小写字母、数字和连字符，长度不超过 64。
- `description` 负责触发，不要把触发条件藏在正文。
- `SKILL.md` 保留核心流程；长模板、评分表、站点经验、API 细节放 `references/`。
- 如果操作可重复且易出错，再放 `scripts/`；不要为了显得完整而创建空目录。
- 第一版至少准备 2 条真实用户提示，用于验证 Skill 是否会在合适场景触发。

## 验收提示示例

- “帮我看 GitHub 和网络上有什么 Agent skill 可以参考，然后给 WeaveMind 设计一个创新 skill。”
- “基于这个仓库现有能力，找 3 个差异化产品机会，选一个做成 SKILL.md。”
- “我想把微信入口做得更有特色，调研竞品后给我一个可落地方案。”
