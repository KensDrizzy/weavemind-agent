# WeaveMind 长期记忆选择性加载改进方案

## 1. 背景与目标

WeaveMind 当前已经具备基础记忆系统：

- `CLAUDE.md`：项目级说明，每轮加载。
- `.weavemind/MEMORY.md`：项目补充记忆，每轮加载。
- `CoreMemory`：用户、项目、persona 三类核心记忆块，每轮加载。
- `LongTermMemory`：长期事实，JSON 持久化，支持去重和基于 query 的模糊检索。
- `ContextCompactor`：上下文过长时摘要旧对话，并抽取关键事实写入长期记忆。

这套机制已经能满足“小规模事实记忆”的需求，但随着长期记忆数量增长，会遇到几个问题：

1. **分层不够清晰**：核心记忆、项目记忆、历史事实、归档内容没有统一层级模型。
2. **召回轨道单一**：长期记忆主要依赖子串匹配、字符 bigram 和时间衰减，缺少路径/文件触发、tag 命中、向量召回和 rerank。
3. **上下文装配缺少预算控制**：`CLAUDE.md`、`MEMORY.md`、CoreMemory 和相关事实直接拼接，没有分层 token 上限。
4. **位置策略粗糙**：当前 `memory_context` 整体放在 system prompt 最前面，召回片段没有被放到更贴近当前任务的位置。
5. **没有渐进式披露**：`MemorySearch` 直接返回完整内容，缺少 “摘要索引先入上下文，需要细节再 read_memory(id)” 的机制。
6. **写入端缺少结构化治理**：长期记忆只有 `content/type/timestamp/metadata`，tag、summary、source、supersedes、layer 等字段不足，容易产生近重复和冲突。

本方案目标是把 WeaveMind 的 memory 系统升级成：

```text
分层存储 + 多轨召回 + 摘要索引 + 按需读取全文 + token 预算控制 + prompt 位置策略
```

优先保持当前实现的简单性和可维护性，不一次性引入过重架构。

## 2. 当前实现简析

当前核心代码路径：

- `core/memory.py`
  - `MemoryEntry`
  - `LongTermMemory`
  - `CoreMemory`
  - `MemoryManager.build_system_message()`
- `core/prompt_assembler.py`
  - `PromptAssembler.assemble()`
- `core/agent_loop.py`
  - `_think()` 每轮从最新用户消息截取前 100 字作为 query，构建 system message。
- `tools/builtin/memory_tools.py`
  - `MemoryAdd`
  - `MemorySearch`
  - `CoreMemoryEdit`
- `core/compaction.py`
  - 上下文压缩和事实抽取。

当前长期记忆结构：

```python
@dataclass
class MemoryEntry:
    id: str
    content: str
    type: Literal["conversation", "fact", "summary"]
    timestamp: float
    token_count: int
    metadata: dict = field(default_factory=dict)
```

当前检索逻辑：

```python
def search(self, query: str, limit: int = 5) -> list[MemoryEntry]:
    query_lower = query.lower().strip()
    scored = []

    for entry in self._entries.values():
        content_lower = entry.content.lower()
        score = 0.0

        if query_lower in content_lower:
            score += 2.0

        score += self._bigram_similarity(query_lower, content_lower)

        age_hours = (time.time() - entry.timestamp) / 3600
        decay = 0.5 ** (age_hours / 168)
        score *= 0.3 + 0.7 * decay

        if score > 0.1:
            scored.append((score, entry))

    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:limit]]
```

当前 prompt 装配：

```text
memory_context
base.md
personality.md
mode.md
skill_index
context.md
handoff.md
```

其中 `memory_context` 内部顺序为：

```text
CLAUDE.md
.weavemind/MEMORY.md
CoreMemory
相关长期记忆全文
```

这说明 WeaveMind 已经具备可升级基础，但还没有把长期记忆当成一个有索引、有预算、有召回策略的独立系统来管理。

## 3. 目标架构

建议将长期记忆分成四层：

| 层级 | 名称 | 内容 | 加载策略 |
| --- | --- | --- | --- |
| L0 | Always-on | 用户身份、稳定偏好、强约束、Agent 行为规范 | 每轮加载，严格限额 |
| L1 | Project/Scope | 项目、目录、仓库、模块相关规则 | 进入 scope 或路径命中时加载 |
| L2 | Episodic | 历史决策、会话摘要、长篇记录、PRD 摘要 | 按 query、tag、路径、向量召回 |
| L3 | Archive | 过期、低频、历史版本内容 | 只有显式引用或强命中时加载 |

对应到 WeaveMind 当前组件：

| 当前组件 | 建议归属 | 改造方式 |
| --- | --- | --- |
| `CoreMemory.user` | L0 | 限制 token，放 system 顶部 |
| `CoreMemory.persona` | L0 | 限制 token，放 system 顶部 |
| `CoreMemory.project` | L0/L1 | 关键项目事实放 L0，详细项目规则迁入 L1 |
| `CLAUDE.md` | L1 | 先保持每轮加载，后续支持 scope 化 |
| `.weavemind/MEMORY.md` | L1 | 先保持每轮加载，后续拆成索引化 project memory |
| `LongTermMemory` | L2/L3 | 扩展 schema，支持摘要索引和按需读取 |
| `ContextCompactor` 摘要 | L2 | 写入时生成 summary/tag/source |

## 4. 记忆数据结构设计

### 4.1 新版 MemoryEntry

建议兼容旧字段，同时新增结构化字段：

```python
from dataclasses import dataclass, field
from typing import Literal, Optional

MemoryLayer = Literal["L0", "L1", "L2", "L3"]
MemoryKind = Literal["fact", "summary", "decision", "preference", "project", "archive"]

@dataclass
class MemoryEntry:
    id: str
    content: str
    type: MemoryKind
    timestamp: float
    token_count: int
    metadata: dict = field(default_factory=dict)

    # 新增字段
    layer: MemoryLayer = "L2"
    title: str = ""
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    source: str = ""
    scope: str = ""
    path: str = ""
    updated_at: Optional[float] = None
    supersedes: list[str] = field(default_factory=list)
    archived: bool = False
```

### 4.2 JSON 示例

```json
{
  "id": "mem_2026_05_memory_layout",
  "layer": "L2",
  "type": "decision",
  "title": "长期记忆上下文装配顺序",
  "summary": "L0 放 system 顶部，本轮召回片段放 system 底部，避免被历史对话稀释。",
  "tags": ["memory", "prompt", "context-assembly"],
  "scope": "WeaveMindAgent",
  "path": "core/memory.py",
  "content": "长期记忆装配采用 L0 顶部 + L1 scope + L2 召回底部的顺序。确定性命中优先于向量召回，记忆总预算建议控制在 3K token 内。",
  "timestamp": 1778576520.0,
  "updated_at": 1778576520.0,
  "token_count": 52,
  "supersedes": [],
  "archived": false,
  "metadata": {
    "source": "compaction",
    "session_id": "..."
  }
}
```

### 4.3 兼容旧数据

读取旧版 JSON 时，自动补齐字段：

```python
@classmethod
def from_dict(cls, d: dict) -> "MemoryEntry":
    d = dict(d)
    d.setdefault("layer", "L2")
    d.setdefault("title", "")
    d.setdefault("summary", d.get("content", "")[:80])
    d.setdefault("tags", [])
    d.setdefault("source", "")
    d.setdefault("scope", "")
    d.setdefault("path", "")
    d.setdefault("updated_at", None)
    d.setdefault("supersedes", [])
    d.setdefault("archived", False)
    return cls(**d)
```

## 5. 召回策略设计

### 5.1 多轨召回

建议拆成三条轨道：

```text
1. 确定性路径轨
   - 当前 cwd
   - 打开的文件
   - 工具调用目标文件
   - repo/module/scope

2. 确定性关键词轨
   - title/tag/path/scope 精确或半精确命中
   - 项目名、模块名、技术栈名

3. 概率语义轨
   - 当前先沿用 bigram
   - 后续接入 embedding + Top-K + reranker
```

### 5.2 召回结果分组

召回结果不要直接混成一个列表，应该保留来源和权重：

```python
@dataclass
class MemoryHit:
    entry: MemoryEntry
    score: float
    source: Literal["path", "tag", "keyword", "semantic"]
    reason: str = ""
```

### 5.3 初版 scorer

```python
def score_memory(entry: MemoryEntry, query: str, context: dict) -> float:
    query_lower = query.lower()
    score = 0.0

    if entry.archived:
        score -= 0.5

    if query_lower and query_lower in entry.content.lower():
        score += 2.0

    if query_lower and query_lower in entry.summary.lower():
        score += 1.5

    for tag in entry.tags:
        if tag.lower() in query_lower:
            score += 1.2

    current_path = context.get("path", "")
    if current_path and entry.path and current_path.startswith(entry.path):
        score += 1.5

    if entry.scope and entry.scope == context.get("scope"):
        score += 1.0

    score += LongTermMemory._bigram_similarity(query_lower, entry.content.lower())

    age_hours = (time.time() - entry.timestamp) / 3600
    decay = 0.5 ** (age_hours / 720)  # 30 天半衰期，避免长期决策过快衰减
    score *= 0.5 + 0.5 * decay

    return score
```

### 5.4 后续接入向量召回

WeaveMind 已有代码 RAG 基础设施：

- `rag/pipeline.py`
- Chroma vector store
- embedding 配置
- keyword index

长期记忆可以先不独立引入 Milvus，优先复用 Chroma 或增加一个轻量 collection：

```text
.weavemind/chroma/code         # 代码 RAG
.weavemind/chroma/memory       # memory RAG
```

检索流程：

```text
deterministic_hits = path_hits + tag_hits + keyword_hits
semantic_hits = memory_vector_store.search(query, k=top_k * 3)
merged = dedupe_by_id(deterministic_hits + semantic_hits)
reranked = rerank(merged, query, context)
selected = apply_budget(reranked)
```

## 6. 渐进式披露机制

### 6.1 核心思想

首轮不要把长期记忆全文都塞进 system prompt，只注入索引：

```text
## 相关记忆索引
以下是本轮可能相关的长期记忆。若摘要不足以回答，请调用 ReadMemory(id) 拉取完整内容。

- id: mem_2026_05_memory_layout
  title: 长期记忆上下文装配顺序
  summary: L0 放 system 顶部，本轮召回片段放 system 底部...
  tags: memory, prompt, context-assembly
```

模型需要细节时再调用：

```text
ReadMemory(id="mem_2026_05_memory_layout")
```

### 6.2 新增工具

建议新增 `ReadMemoryTool`：

```python
class ReadMemoryInput(BaseModel):
    id: str = Field(description="要读取的长期记忆 id")


class ReadMemoryTool(WeaveMindTool):
    name: str = "ReadMemory"
    description: str = (
        "按 id 读取长期记忆全文。"
        "当 system prompt 中的记忆索引摘要不足以回答问题时使用。"
        "同一轮中已读取的 id 不要重复读取。"
    )
    args_schema: Type[BaseModel] = ReadMemoryInput

    def __init__(self, memory_manager=None):
        super().__init__()
        self._memory_manager = memory_manager
        self._read_ids: set[str] = set()

    def _run(self, id: str) -> str:
        if not self._memory_manager:
            return "错误：记忆管理器未初始化"
        if id in self._read_ids:
            return f"记忆 {id} 本轮已读取，避免重复加载。"
        entry = self._memory_manager.read_memory(id)
        if not entry:
            return f"未找到记忆: {id}"
        self._read_ids.add(id)
        return (
            f"# {entry.title or entry.id}\n"
            f"tags: {', '.join(entry.tags)}\n\n"
            f"{entry.content}"
        )
```

### 6.3 MemoryManager 新接口

```python
class MemoryManager:
    def read_memory(self, memory_id: str) -> Optional[MemoryEntry]:
        return self.long_term.get_by_id(memory_id)

    def search_memory_index(
        self,
        query: str,
        context: Optional[dict] = None,
        limit: int = 8,
    ) -> list[MemoryEntry]:
        return self.long_term.search_index(query, context=context or {}, limit=limit)
```

### 6.4 LongTermMemory 新接口

```python
class LongTermMemory:
    def get_by_id(self, memory_id: str) -> Optional[MemoryEntry]:
        for entry in self._entries.values():
            if entry.id == memory_id:
                return entry
        return None

    def search_index(
        self,
        query: str,
        context: dict,
        limit: int = 8,
    ) -> list[MemoryEntry]:
        hits = []
        for entry in self._entries.values():
            score = score_memory(entry, query, context)
            if score > 0.1:
                hits.append((score, entry))
        hits.sort(key=lambda x: -x[0])
        return [entry for _, entry in hits[:limit]]
```

## 7. 上下文预算与位置策略

### 7.1 推荐预算

第一阶段建议使用保守预算：

| 区域 | 上限 |
| --- | --- |
| L0 CoreMemory | 500 token |
| L1 Project/Scope | 1000 token |
| L2 deterministic recall | 1000 token |
| L2 semantic recall index | 800 token |
| memory 总预算 | 3000 token |

配置示例：

```yaml
memory:
  project_file: .weavemind/MEMORY.md
  claude_md: CLAUDE.md
  long_term_file: .weavemind/memory/long_term.json
  core_file: .weavemind/memory/core.json
  selective_loading:
    enabled: true
    max_total_tokens: 3000
    l0_max_tokens: 500
    l1_max_tokens: 1000
    recall_index_max_tokens: 1500
    deterministic_limit: 5
    semantic_limit: 8
    inject_full_text: false
```

### 7.2 Prompt 位置策略

推荐 system prompt 结构：

```text
[System 顶部]
base identity / role
L0 CoreMemory

[System 中部]
工具说明
Skill 索引
模式提示词

[System 底部]
L1 Project/Scope memory
本轮相关记忆索引
ReadMemory 使用说明

[历史对话]
滑窗 / 压缩摘要

[最新 User Message]
```

当前 `PromptAssembler` 是把整个 `memory_context` 放最前面，建议改成多个插槽：

```python
def assemble(
    self,
    mode: PromptMode = PromptMode.AGENT,
    l0_memory: Optional[str] = None,
    l1_memory: Optional[str] = None,
    recall_memory_index: Optional[str] = None,
    skill_index: Optional[str] = None,
    variables: Optional[dict] = None,
) -> str:
    parts = []

    parts.append(self.repository.load_required("base.md"))

    if l0_memory:
        parts.append(l0_memory)

    personality = self.repository.load("personality.md")
    if personality:
        parts.append(personality)

    mode_content = self.repository.load_required(mode.value)
    parts.append(mode_content)

    if skill_index:
        parts.append(skill_index)

    ctx = self.repository.load("context.md")
    if ctx:
        parts.append(ctx)

    # 靠近 system prompt 底部，降低被历史对话稀释的概率
    if l1_memory:
        parts.append(l1_memory)

    if recall_memory_index:
        parts.append(recall_memory_index)

    handoff = self.repository.load("handoff.md")
    if handoff:
        parts.append(handoff)

    return "\n\n".join(p for p in parts if p and p.strip())
```

注意：如果 `handoff.md` 是极高优先级约束，也可以继续放最后；如果召回记忆对本轮回答更关键，则可把 `recall_memory_index` 放在 `handoff.md` 前。

## 8. MemoryManager 装配设计

### 8.1 新增 MemoryContext

```python
@dataclass
class MemoryContext:
    l0: str = ""
    l1: str = ""
    recall_index: str = ""
    debug: dict = field(default_factory=dict)
```

### 8.2 build_memory_context()

```python
class MemoryManager:
    def build_memory_context(self, query: str = "", context: dict | None = None) -> MemoryContext:
        context = context or {}
        budget = MemoryBudget.from_settings()

        l0 = self._build_l0_core_memory(max_tokens=budget.l0_max_tokens)
        l1 = self._build_l1_project_memory(context=context, max_tokens=budget.l1_max_tokens)

        recall_entries = []
        if query:
            recall_entries = self.long_term.search_index(
                query,
                context=context,
                limit=settings.get("memory.selective_loading.semantic_limit", 8),
            )

        recall_index = self._format_memory_index(
            recall_entries,
            max_tokens=budget.recall_index_max_tokens,
        )

        return MemoryContext(
            l0=l0,
            l1=l1,
            recall_index=recall_index,
            debug={
                "recall_ids": [e.id for e in recall_entries],
                "l0_tokens": self._estimate_tokens(l0),
                "l1_tokens": self._estimate_tokens(l1),
                "recall_tokens": self._estimate_tokens(recall_index),
            },
        )
```

### 8.3 格式化记忆索引

```python
def _format_memory_index(self, entries: list[MemoryEntry], max_tokens: int) -> str:
    if not entries:
        return ""

    lines = [
        "## 相关长期记忆索引",
        "",
        "以下是本轮可能相关的长期记忆。若摘要不足以回答，请调用 ReadMemory(id) 获取全文。",
        "",
    ]

    used = self._estimate_tokens("\n".join(lines))
    for entry in entries:
        item = (
            f"- id: {entry.id}\n"
            f"  title: {entry.title or entry.content[:30]}\n"
            f"  summary: {entry.summary or entry.content[:80]}\n"
            f"  tags: {', '.join(entry.tags)}\n"
        )
        item_tokens = self._estimate_tokens(item)
        if used + item_tokens > max_tokens:
            break
        lines.append(item)
        used += item_tokens

    return "\n".join(lines)
```

## 9. 写入端质量治理

### 9.1 MemoryAdd 输入升级

当前 `MemoryAdd` 只收 `content`。建议升级为：

```python
class MemoryAddInput(BaseModel):
    content: str = Field(description="要保存的完整记忆内容")
    title: str = Field(default="", description="短标题，建议 20 字以内")
    summary: str = Field(default="", description="一句话摘要，建议 50 字以内")
    tags: list[str] = Field(default_factory=list, description="标签，如 memory、rag、project")
    layer: Literal["L1", "L2", "L3"] = Field(default="L2", description="记忆层级")
    scope: str = Field(default="", description="适用项目、目录或模块")
    path: str = Field(default="", description="相关文件或目录路径")
```

为了兼容模型调用，也可以保留 `content` 必填，其余字段自动生成或为空。

### 9.2 自动 summary/tag

第一阶段不要强依赖 LLM 自动生成，可以使用简单规则：

```python
def normalize_memory_input(content: str, metadata: dict | None = None) -> dict:
    content = content.strip()
    metadata = metadata or {}
    return {
        "title": metadata.get("title") or content[:30],
        "summary": metadata.get("summary") or content[:80],
        "tags": metadata.get("tags") or [],
        "scope": metadata.get("scope", ""),
        "path": metadata.get("path", ""),
    }
```

第二阶段再让 `ContextCompactor._extract_facts()` 输出结构化 JSON：

```text
请从对话中提取跨会话仍有价值的记忆，输出 JSON 数组。
每个元素包含：title, summary, content, tags, layer, scope, path。
如果没有，输出 []。
```

### 9.3 冲突与去重

建议增加两级去重：

1. **硬去重**：content hash 完全一致。
2. **软去重**：同 `scope + tags` 下 summary/content 相似度高于阈值。

对于软去重，不一定直接覆盖，可以记录版本关系：

```python
entry.supersedes.append(old_entry.id)
old_entry.archived = True
old_entry.layer = "L3"
```

## 10. 分阶段实施计划

### Phase 1：轻量选择性加载

目标：不引入 embedding，先把分层、预算、位置和索引披露做起来。

任务：

1. 扩展 `MemoryEntry` schema，兼容旧数据。
2. 新增 `MemoryBudget`、`MemoryContext`。
3. 将 `MemoryManager.build_system_message()` 拆成：
   - `build_memory_context()`
   - `_build_l0_core_memory()`
   - `_build_l1_project_memory()`
   - `_format_memory_index()`
4. `PromptAssembler.assemble()` 增加 `l0_memory/l1_memory/recall_memory_index` 插槽。
5. 新增 `ReadMemoryTool`。
6. `MemorySearchTool` 改成优先返回索引摘要，而不是全文。
7. 增加配置项：
   - `memory.selective_loading.enabled`
   - `memory.selective_loading.max_total_tokens`
   - `memory.selective_loading.l0_max_tokens`
   - `memory.selective_loading.l1_max_tokens`
   - `memory.selective_loading.recall_index_max_tokens`

验收标准：

- CoreMemory 被单独格式化为 L0。
- 本轮相关长期记忆以 `id/title/summary/tags` 形式进入 system prompt。
- 模型可通过 `ReadMemory(id)` 拉取全文。
- 单轮 memory 注入总量可控。
- 旧版 `.weavemind/memory/long_term.json` 不需要手动迁移即可读取。

### Phase 2：确定性召回增强

目标：让记忆选择更贴近当前文件、目录、项目和任务意图。

任务：

1. 新增 `MemoryHit`。
2. `LongTermMemory.search_index()` 支持 context：
   - `cwd`
   - `file_path`
   - `scope`
   - `tool_name`
3. `agent_loop._think()` 不再只传最新用户消息前 100 字，改为传入：
   - 完整用户消息截断后的 query
   - 当前 cwd
   - 最近工具调用涉及的 path
   - 当前项目名
4. `MemoryAdd` 支持 `tags/scope/path/title/summary`。
5. `/memory` 命令展示 layer、tag、summary 和 archived 状态。

验收标准：

- 询问某个文件/目录相关问题时，相关 path/scope 记忆优先出现。
- 同主题重复事实明显减少。
- `/memory` 能看出记忆分层和索引状态。

### Phase 3：向量召回与 rerank

目标：当长期记忆超过几百条后，仍能保持高召回质量。

任务：

1. 新增 memory vector collection。
2. 写入长期记忆时同步写入 embedding。
3. 检索时合并：
   - tag/path/keyword hits
   - vector hits
4. 增加轻量 rerank：
   - tag/path/scope 加权
   - recency 加权
   - semantic score 加权
5. 对 `ReadMemory` 增加同轮缓存，避免重复拉取。

验收标准：

- query 与记忆没有明显关键词重合时，也能召回正确摘要。
- 记忆库达到 1000 条时，首轮 prompt 仍只注入索引摘要。
- 召回耗时可接受。

### Phase 4：归档和记忆维护

目标：长期运行后记忆库保持干净。

任务：

1. 增加 `ArchiveMemoryTool` 或 `/memory archive`。
2. 增加自动归档策略：
   - 被 supersede 的旧记忆进入 L3。
   - 长期未命中且低价值内容进入 L3。
3. 增加 memory maintenance 命令：
   - `/memory list`
   - `/memory show <id>`
   - `/memory archive <id>`
   - `/memory merge <id1> <id2>`
   - `/memory prune`

验收标准：

- 重复旧决策不会持续污染召回。
- 用户可以追溯和维护长期记忆。

## 11. 测试计划

### 11.1 单元测试

建议新增或扩展 `tests/test_memory.py`：

```python
def test_memory_entry_from_old_dict_backfills_new_fields(tmp_path):
    data = {
        "id": "abc",
        "content": "用户偏好中文回复",
        "type": "fact",
        "timestamp": 1.0,
        "token_count": 10,
        "metadata": {},
    }

    entry = MemoryEntry.from_dict(data)

    assert entry.layer == "L2"
    assert entry.summary
    assert entry.tags == []
    assert entry.archived is False
```

```python
def test_search_index_prefers_tag_match(tmp_path):
    memory = LongTermMemory(str(tmp_path / "long_term.json"))
    memory.store(
        "长期记忆装配采用 L0 顶部和召回索引底部策略",
        metadata={
            "title": "Prompt 位置策略",
            "summary": "L0 顶部，召回索引靠近底部",
            "tags": ["memory", "prompt"],
        },
    )

    results = memory.search_index("memory prompt 怎么装配", context={}, limit=3)

    assert results
    assert "L0" in results[0].content
```

```python
def test_format_memory_index_respects_budget(tmp_path):
    manager = MemoryManager(project_root=str(tmp_path))
    entries = [
        MemoryEntry(
            id=f"mem_{i}",
            content="x" * 200,
            type="fact",
            timestamp=1.0,
            token_count=100,
            title=f"title {i}",
            summary="summary " * 20,
        )
        for i in range(10)
    ]

    text = manager._format_memory_index(entries, max_tokens=100)

    assert "相关长期记忆索引" in text
    assert len(text) < 1000
```

### 11.2 集成测试

1. 启动 `MemoryManager`，写入 20 条长期记忆。
2. 构建 system message。
3. 断言：
   - system message 包含 L0。
   - system message 包含相关记忆索引。
   - system message 不包含所有长期记忆全文。
   - `ReadMemory(id)` 能返回全文。

### 11.3 回归测试

必须确认：

- 旧版 `long_term.json` 可以正常读取。
- 旧版 `MemoryAdd(content)` 调用不破坏。
- `CoreMemoryEdit` 行为保持不变。
- 没开启 `memory.selective_loading.enabled` 时可以回退到旧逻辑。

## 12. 推荐最小改动路径

如果只做一版 MVP，建议范围控制为：

1. `core/memory.py`
   - 扩展 `MemoryEntry`
   - 新增 `get_by_id()`
   - 新增 `search_index()`
   - 新增 `_format_memory_index()`
2. `tools/builtin/memory_tools.py`
   - 新增 `ReadMemoryTool`
   - `MemorySearchTool` 返回摘要索引
3. `tools/registry.py`
   - 注册 `ReadMemoryTool`
4. `core/prompt_assembler.py`
   - 增加分层 memory 插槽
5. `config.yaml.example`
   - 增加 selective loading 配置
6. `tests/test_memory.py`
   - 增加兼容、索引、读取测试

这版不碰 embedding，不碰 RAG pipeline，不大改 compaction。这样风险最小，能先把长期记忆从“全文事实检索”升级到“摘要索引 + 按需读取”。

## 13. 预期收益

升级后，WeaveMind 的 memory 会从当前的：

```text
CLAUDE.md + MEMORY.md + CoreMemory + Top5 长期事实全文
```

变成：

```text
L0 常驻核心记忆
+ L1 项目/Scope 记忆
+ L2/L3 长期记忆索引
+ ReadMemory(id) 按需拉全文
+ token 预算和位置策略
```

主要收益：

- 长期记忆增长后，首轮 prompt 不会线性膨胀。
- 相关记忆以索引方式出现，模型能知道“有什么”，但只为需要的细节付费。
- L0 强约束更稳定，不容易被历史对话和工具说明稀释。
- 记忆写入更结构化，后续可以自然演进到 embedding、rerank 和自动归档。
- 和 WeaveMind 当前代码兼容，改造路径可分阶段完成。

