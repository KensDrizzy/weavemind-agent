# WeaveMind Memory 系统升级分析

> 基于 PaiCLI Memory 实现分析 + 2026 主流 Agent Memory 方案调研
> 生成时间：2026-05-08

---

## 一、PaiCLI Memory 实现分析

### 1.1 整体架构

PaiCLI 的 Memory 系统采用**门面模式（Facade Pattern）**，`MemoryManager` 作为唯一对外接口，底层管理五个组件：

```
MemoryManager（门面）
  ├── ConversationMemory   — 短期记忆（当前会话）
  ├── LongTermMemory       — 长期记忆（跨会话持久化）
  ├── ContextCompressor    — 上下文压缩（Map-Reduce 策略）
  ├── TokenBudget          — Token 预算管理
  └── MemoryRetriever      — 记忆检索（jieba 分词 + 关键词匹配）
```

Agent 只与 `MemoryManager` 交互，暴露两个核心操作：**存消息**、**取记忆**。

### 1.2 记忆基本单元 — MemoryEntry

```java
public class MemoryEntry {
    private final String id;
    private final String content;
    private final MemoryType type;      // 记忆类型
    private final Instant timestamp;    // 时间戳（用于时间衰减）
    private final Map<String, String> metadata;  // 附加信息
    private final int tokenCount;       // Token 占用
}

public enum MemoryType {
    CONVERSATION,  // 对话记忆
    FACT,          // 事实记忆（用户偏好、项目信息）
    SUMMARY,       // 摘要记忆
    TOOL_RESULT    // 工具执行结果
}
```

**设计要点：**
- `TOOL_RESULT` 单独分类，因为工具返回内容通常很长，压缩时可以更激进地砍
- Token 估算：中文约 1.5 字/token，英文约 4 字符/token
- 时间戳保留原始值，不做覆盖，确保时间衰减检索的正确性

### 1.3 短期记忆 — ConversationMemory

**核心机制：** LinkedHashMap + FIFO 淘汰

```java
public class ConversationMemory implements Memory {
    private final LinkedHashMap<String, MemoryEntry> entries;
    private final int maxTokens;
    private final AtomicInteger currentTokens;
    private final List<MemoryEntry> compressedSummaries;

    @Override
    public void store(MemoryEntry entry) {
        entries.put(entry.getId(), entry);
        currentTokens.addAndGet(entry.getTokenCount());
        // 超出预算时自动淘汰最旧的条目
        while (currentTokens.get() > maxTokens && entries.size() > 1) {
            evictOldest();
        }
    }
}
```

**关键设计：**
- 淘汰的消息不直接丢弃，而是放进 `compressedSummaries` 等待压缩
- `getUsageRatio()` 返回 token 使用率，超过 **80%** 自动触发 `ContextCompressor`
- 使用 LinkedHashMap 保证插入顺序，FIFO 策略类似操作系统页面置换

### 1.4 长期记忆 — LongTermMemory

**核心机制：** JSON 文件持久化 + jieba 分词检索

```java
public class LongTermMemory implements Memory {
    private static final String STORAGE_FILE = "long_term_memory.json";
    
    @Override
    public void store(MemoryEntry entry) {
        // 自动去重：内容完全相同则跳过
        Optional<...> existing = entries.entrySet().stream()
            .filter(e -> e.getValue().getContent().equals(entry.getContent()))
            .findFirst();
        if (existing.isPresent()) return;
        
        entries.put(entry.getId(), entry);
        saveToDisk();  // 即时持久化
    }
}
```

**三个设计要点：**
1. **自动去重** — 连续三次说"我喜欢用 Java"只存一条
2. **即时持久化** — 每次 store 都写磁盘，路径支持三种配置（默认/JVM参数/环境变量）
3. **启动时加载** — 保留原始时间戳，确保时间衰减正确

**检索实现：**
- 使用 jieba 分词器做中文优先的轻量匹配
- 过滤单字符和纯标点（"的""了""是"等无检索价值）
- 同时匹配 content 和 metadata

**局限性：** jieba + 关键词匹配 vs 向量检索，语义理解能力有限。"Java 框架"匹配不到"Spring Boot"。

### 1.5 上下文压缩 — ContextCompressor

**核心策略：Map-Reduce**

```
旧消息 → 分组（每 5 条一组）
         ↓
    Map 阶段：每组独立调 LLM 生成摘要
         ↓
    Reduce 阶段：合并多个分片摘要
         ↓
    清空旧记忆 → 注入摘要 → 保留最近 3 轮
```

**关键参数：**
- `retainRecentRounds = 3` — 最近 3 轮不参与压缩
- 分组大小 = 5 条/组
- 分组处理比一股脑扔几十条给 LLM 效果好得多

**附加功能 — 事实提取：**
对话结束时（/clear 或退出），自动调用 `extractFacts` 让 LLM 从对话中提炼关键信息，存入长期记忆。

### 1.6 集成到 Agent

```java
// 记忆注入到 system prompt（不是 user message）
private void updateSystemPromptWithMemory(String memoryContext) {
    if (memoryContext == null || memoryContext.isEmpty()) {
        conversationHistory.set(0, GLMClient.Message.system(SYSTEM_PROMPT));
    } else {
        String enrichedPrompt = SYSTEM_PROMPT + "\n" + memoryContext;
        conversationHistory.set(0, GLMClient.Message.system(enrichedPrompt));
    }
}
```

**为什么放 system prompt？**
- 放 user message 里，LLM 可能把记忆内容当成用户指令执行
- 放 system prompt 里，模型当背景信息参考，不会混淆

**自动事实提取触发点：**
- 用户输入 `/clear` 清空对话时
- 用户直接退出时
- PlanExecuteAgent 执行完整个计划后

---

## 二、WeaveMind 现状分析

### 2.1 当前实现

```python
# core/memory.py — 当前实现
class WeaveMindMemory:
    def load(self) -> str:
        paths = [
            os.path.join(self.project_root, settings.get("memory.claude_md", "CLAUDE.md")),
            os.path.join(self.project_root, settings.get("memory.project_file", ".weavemind/MEMORY.md")),
        ]
        return "\n\n".join(open(p).read() for p in paths if os.path.exists(p))
    
    def build_system_message(self) -> SystemMessage:
        memory = self.load()
        # + 固定的行为规范文本
        return SystemMessage(content)
```

### 2.2 差距分析

| 能力 | PaiCLI | WeaveMind 现状 | 差距 |
|------|--------|---------------|------|
| 短期记忆管理 | ✅ LinkedHashMap + FIFO | ❌ 无（依赖 LangChain 默认） | 严重 |
| 长期记忆持久化 | ✅ JSON 文件 + 去重 | ❌ 仅静态 CLAUDE.md | 严重 |
| 上下文压缩 | ✅ Map-Reduce | ⚠️ 已定义未集成 | 中等 |
| Token 预算管理 | ✅ 80% 阈值自动触发 | ❌ 无 | 严重 |
| 记忆检索 | ✅ jieba + 关键词 | ❌ 无 | 严重 |
| 事实自动提取 | ✅ LLM 提取 | ❌ 无 | 中等 |
| 记忆类型分类 | ✅ 4 种类型 | ❌ 无 | 中等 |

**核心问题：** WeaveMind 的 `ContextCompactor` 已经写好了（tiktoken 计数 + LLM 摘要），但**从未在主循环中调用**。`SessionManager.save()` 也从未被调用。

---

## 三、2026 主流 Agent Memory 方案调研

### 3.1 方案总览

| 框架 | 核心思路 | 适用场景 | 开源 |
|------|---------|---------|------|
| **Mem0** | 专用记忆层 + 向量/图检索 | 通用 Agent | ✅ |
| **Zep** | 对话记忆 + 实体/事实提取 | 对话型 Agent | ✅ |
| **Letta (MemGPT)** | OS 式分层 + 自管理 | 长对话 Agent | ✅ |
| **LangChain Memory** | 可组合记忆模块 | LangChain 生态 | ✅ |
| **LlamaIndex Memory** | 文档 + 对话融合 | 知识密集型 | ✅ |
| **Cognee** | 知识图谱 + 向量检索 | 关系推理 | ✅ |
| **OpenAI Memory** | 全局事实 + 聊天 RAG | 消费级产品 | ❌ |
| **Claude Memory** | 项目级 + CLAUDE.md | 开发者工具 | ❌ |

### 3.2 Letta (MemGPT) — 最值得参考的架构

**核心理念：** 把 LLM 上下文窗口当作 RAM，用 OS 式虚拟内存管理。

**三层记忆架构：**

```
┌─────────────────────────────────────────────┐
│  LLM Context Window（有限，类似 RAM）         │
│  ┌─────────────────────────────────────┐    │
│  │ System Prompt + Core Memory Blocks  │    │  ← 始终在上下文中
│  │  - Persona Block（Agent 人设）       │    │
│  │  - Human Block（用户信息）           │    │
│  └─────────────────────────────────────┘    │
│  ┌─────────────────────────────────────┐    │
│  │ Recent Messages（滑动窗口）          │    │  ← 最近的对话
│  └─────────────────────────────────────┘    │
├─────────────────────────────────────────────┤
│  外部存储（类似磁盘）                         │
│  ┌─────────────────────────────────────┐    │
│  │ Recall Memory — 完整对话历史         │    │  ← 可搜索
│  │ （关键词/日期检索）                   │    │
│  └─────────────────────────────────────┘    │
│  ┌─────────────────────────────────────┐    │
│  │ Archival Memory — 长期知识           │    │  ← 向量检索
│  │ （向量数据库/知识图谱）               │    │
│  └─────────────────────────────────────┘    │
└─────────────────────────────────────────────┘
```

**关键机制：**

1. **自编辑记忆（Self-Editing Memory）**
   - Agent 通过 function call 主动修改自己的 Core Memory
   - `core_memory_replace` / `core_memory_append` 工具
   - 不是被动存储，而是 Agent 主动管理

2. **心跳机制（Heartbeat）**
   - 一次用户消息可以触发多轮内部处理
   - `request_heartbeat=true` → 继续处理，不等用户输入
   - 支持单轮内完成多步记忆操作

3. **内部独白（Inner Monologue）**
   - 每次响应前的私有推理步骤
   - 用户不可见，用于记忆管理决策
   - "该存什么？该删什么？该检索什么？"

4. **记忆压力信号（Memory Pressure）**
   - 上下文接近阈值时插入内部警告
   - Agent 收到信号后主动总结、归档、丢弃低优先级信息

5. **Sleep-Time Compute（异步记忆管理）**
   - 独立的 sleep-time agent 在空闲时整理记忆
   - 非阻塞操作，不增加响应延迟
   - 主动优化记忆质量，而非懒更新

### 3.3 Mem0 — 生产级记忆层

**核心架构：** 选择性记忆管道（Selective Memory Pipeline）

**LOCOMO 基准测试结果（ECAI 2025）：**

| 方案 | 准确率 | P95 延迟 | Token 消耗 |
|------|--------|---------|-----------|
| Full-context | 72.9% | 17.12s | ~26,000/会话 |
| Mem0g（图增强） | 68.4% | 2.59s | ~1,800/会话 |
| Mem0 | 66.9% | 1.44s | ~1,800/会话 |
| RAG | 61.0% | - | - |
| OpenAI Memory | 52.9% | - | - |

**关键洞察：**
- Full-context 准确率最高但不可用（17s 延迟 + 14x token 成本）
- Mem0 用 6% 准确率换 91% 延迟降低 + 90% token 节省
- 图增强版本（Mem0g）进一步缩小准确率差距

**多层记忆作用域：**
- User-level — 跨所有会话的用户偏好
- Session-level — 单次会话上下文
- Agent-level — Agent 自身的行为模式

### 3.4 OpenAI Memory — 产品级方案

**双层架构：**
1. **Saved Memories** — 结构化事实（key-value），自动/手动提取
2. **Chat History Reference** — 全历史 RAG 检索

**写回模式：**
- 显式命令："记住我喜欢 Java 17"
- 自动提取：后台分类器扫描对话，识别高频/重要信息

### 3.5 Claude Memory — 开发者友好方案

**项目级隔离：**
- 每个项目独立的 memory summary
- `CLAUDE.md` 文件模式（Git 版本控制）
- 按需检索，不主动全局索引

**WeaveMind 已经采用了这种模式**（CLAUDE.md + .weavemind/MEMORY.md），但缺少动态管理能力。

---

## 四、WeaveMind Memory 升级方案

### 4.1 设计原则

1. **渐进式升级** — 不推翻现有架构，逐步增强
2. **Python 原生** — 不引入 Java 依赖，用 Python 生态替代
3. **可插拔** — 记忆组件独立，方便替换和扩展
4. **生产可用** — 参考 Mem0 的性能约束（延迟 < 2s，token < 2000/会话）

### 4.2 目标架构

```
MemoryManager（门面，对外统一接口）
  │
  ├── ConversationMemory（短期记忆）
  │     - collections.OrderedDict 实现 FIFO
  │     - Token 预算管理（tiktoken 计数）
  │     - 80% 阈值自动触发压缩
  │     - 淘汰消息进入压缩队列
  │
  ├── LongTermMemory（长期记忆）
  │     - JSON 文件持久化（.weavemind/memory/long_term.json）
  │     - 内容去重（hash 比对）
  │     - 启动时自动加载
  │     - 分词检索（jieba 或 jionlp）
  │
  ├── ContextCompressor（上下文压缩）
  │     - Map-Reduce 策略（已有基础，需集成）
  │     - 保留最近 N 轮不压缩
  │     - 事实自动提取（LLM extractFacts）
  │
  ├── MemoryRetriever（记忆检索）
  │     - 关键词匹配 + 时间衰减 + 来源加权
  │     - 未来可扩展为向量检索
  │
  └── CoreMemory（核心记忆 — 新增）
        - 始终在 system prompt 中的记忆块
        - Agent 可自主编辑（类似 Letta Core Memory）
        - 用户信息块 + 项目信息块 + Agent 人设块
```

### 4.3 实现优先级

#### P0 — 必须立即做

**1. 集成 ContextCompactor 到主循环**

```python
# core/agent_loop.py — 在 think 节点后添加压缩检查
def _check_compaction(self, state: AgentState) -> AgentState:
    if self.compactor.should_compact(state["messages"]):
        state["messages"] = self.compactor.compact(state["messages"])
    return state
```

**2. 激活 SessionManager.save()**

```python
# cli/app.py — 在每次 LLM 响应后保存会话
def _on_response(self, response):
    self.session_manager.save(self.session_id, {
        "messages": self.agent_loop.get_messages(),
        "memory": self.memory.get_state()
    })
```

**3. 长期记忆持久化**

```python
# core/memory.py — 新增 LongTermMemory
class LongTermMemory:
    def __init__(self, storage_path: str = ".weavemind/memory/long_term.json"):
        self.storage_path = storage_path
        self.entries: dict[str, MemoryEntry] = {}
        self._load_from_disk()
    
    def store(self, entry: MemoryEntry):
        # 去重
        content_hash = hashlib.md5(entry.content.encode()).hexdigest()
        if content_hash in self.entries:
            return
        self.entries[content_hash] = entry
        self._save_to_disk()
    
    def search(self, query: str, limit: int = 5) -> list[MemoryEntry]:
        # 分词 + 关键词匹配
        tokens = self._tokenize(query)
        scored = []
        for entry in self.entries.values():
            score = self._compute_score(entry, tokens)
            if score > 0:
                scored.append((score, entry))
        scored.sort(key=lambda x: -x[0])
        return [e for _, e in scored[:limit]]
```

#### P1 — 近期做

**4. Core Memory 块（借鉴 Letta）**

```python
# core/memory.py — 新增 CoreMemory
class CoreMemory:
    """始终在 system prompt 中的记忆块，Agent 可自主编辑"""
    
    def __init__(self):
        self.blocks = {
            "user": "",      # 用户信息
            "project": "",   # 项目信息
            "persona": "",   # Agent 人设
        }
    
    def edit(self, block: str, old_text: str, new_text: str):
        """Agent 通过 tool call 编辑记忆块"""
        self.blocks[block] = self.blocks[block].replace(old_text, new_text)
    
    def append(self, block: str, text: str):
        self.blocks[block] += "\n" + text
    
    def to_system_prompt(self) -> str:
        parts = []
        for name, content in self.blocks.items():
            if content:
                parts.append(f"[{name.upper()}]\n{content}")
        return "\n\n".join(parts)
```

**5. 新增记忆管理工具**

```python
# tools/builtin/memory_tools.py
class MemoryEditTool(WeaveMindTool):
    """Agent 自主编辑长期记忆"""
    name = "MemoryEdit"
    description = "编辑长期记忆中的事实"
    
    def _run(self, action: str, content: str, block: str = "user"):
        if action == "add":
            self.memory.long_term.store(MemoryEntry(content, type="FACT"))
        elif action == "search":
            return self.memory.long_term.search(content)
        elif action == "edit_core":
            self.memory.core_memory.edit(block, content)
```

**6. 事实自动提取**

```python
# core/memory.py — 对话结束时自动提取
def extract_facts(self, messages: list) -> list[str]:
    prompt = """从以下对话中提取关键事实（用户偏好、项目信息、重要决策）。
    每条事实一行，格式：[类别] 事实内容
    只提取跨会话仍有价值的信息。"""
    
    response = self.llm.invoke([
        SystemMessage(content=prompt),
        *messages
    ])
    
    facts = self._parse_facts(response.content)
    for fact in facts:
        self.long_term.store(MemoryEntry(fact, type="FACT"))
    return facts
```

#### P2 — 未来做

**7. 向量检索升级**

```python
# 替换 jieba 关键词匹配为向量检索
# 使用 Chroma（已定义但未接入 rag/ 模块）
class VectorMemoryRetriever:
    def __init__(self, collection_name="memory"):
        self.chroma = chromadb.PersistentClient(path=".weavemind/chroma")
        self.collection = self.chroma.get_or_create_collection(collection_name)
    
    def search(self, query: str, limit: int = 5) -> list[MemoryEntry]:
        results = self.collection.query(query_texts=[query], n_results=limit)
        return [self._to_entry(r) for r in results["documents"][0]]
```

**8. Sleep-Time Agent（借鉴 Letta）**

```python
# agents/sleep_time.py
class SleepTimeAgent:
    """空闲时异步整理记忆的 Agent"""
    
    async def run(self):
        while True:
            await asyncio.sleep(300)  # 每 5 分钟检查一次
            if self._should_optimize():
                self._consolidate_memories()
                self._update_core_memory()
                self._prune_stale_facts()
```

### 4.4 需要引入的依赖

```txt
# requirements.txt 新增
jieba>=0.42.1          # 中文分词（长期记忆检索）
chromadb>=0.4.0        # 向量数据库（P2 阶段）
tiktoken>=0.5.0        # 已有，用于 token 计数
```

### 4.5 配置扩展

```yaml
# config.yaml 新增
memory:
  claude_md: "CLAUDE.md"
  project_file: ".weavemind/MEMORY.md"
  long_term_file: ".weavemind/memory/long_term.json"
  core_memory_file: ".weavemind/memory/core.json"
  
  short_term:
    max_tokens: 80000
    compaction_threshold: 0.8  # 80% 触发压缩
    retain_recent_rounds: 3
  
  long_term:
    dedup: true
    search_limit: 5
    time_decay_hours: 168  # 7 天半衰期
  
  compression:
    chunk_size: 5  # 每组 5 条消息
    model: null    # null = 使用主模型
```

---

## 五、关键差异总结

### PaiCLI vs 主流方案

| 维度 | PaiCLI | Letta/MemGPT | Mem0 | WeaveMind 目标 |
|------|--------|-------------|------|---------------|
| 记忆分层 | 2 层（短/长） | 3 层（核心/召回/归档） | 3 层（用户/会话/Agent） | 3 层（核心/短/长） |
| Agent 自主管理 | ❌ 被动 | ✅ function call 自编辑 | ✅ 自动提取 | ✅ 工具调用编辑 |
| 检索方式 | jieba 关键词 | 向量 + 关键词 | 向量 + 图 | 关键词 → 向量（渐进） |
| 上下文压缩 | Map-Reduce | 递归摘要 | 选择性管道 | Map-Reduce |
| 异步记忆整理 | ❌ | ✅ Sleep-Time | ❌ | P2 阶段 |
| 持久化 | JSON 文件 | 数据库 | 云服务 | JSON → Chroma |

### WeaveMind 的独特优势

1. **已有 CLAUDE.md 模式** — 与 Claude 的项目级记忆理念一致
2. **ContextCompactor 已定义** — 只需集成，不需要重写
3. **LangGraph 架构** — 天然支持状态管理和节点间数据流
4. **SubAgent 支持** — 可以实现 Sleep-Time Agent 模式

### WeaveMind 的核心短板

1. **记忆完全静态** — 启动时加载一次，运行中不更新
2. **无跨会话持久化** — SessionManager.save() 从未调用
3. **无 Agent 自主记忆管理** — 缺少记忆编辑工具
4. **无自动事实提取** — 对话结束时不提取关键信息

---

## 六、实施路线图

```
Phase 1（1-2 天）— 基础记忆能力
  ├── 集成 ContextCompactor 到 AgentLoop
  ├── 激活 SessionManager.save()
  └── 实现 LongTermMemory（JSON 持久化 + 去重）

Phase 2（3-5 天）— 记忆管理工具
  ├── 实现 CoreMemory（用户/项目/人设块）
  ├── 新增 MemoryEdit / MemorySearch 工具
  ├── 实现事实自动提取（extractFacts）
  └── 记忆注入 system prompt

Phase 3（1-2 周）— 高级检索
  ├── 接入 Chroma 向量数据库
  ├── 实现语义检索替代关键词匹配
  └── 时间衰减 + 来源加权排序

Phase 4（未来）— 自主记忆
  ├── Sleep-Time Agent 异步整理
  ├── 记忆质量评估和自动优化
  └── 多 Agent 共享记忆
```

---

## 七、参考资源

- PaiCLI Memory 教程: https://paicoding.com/column/17/3
- Letta Agent Memory: https://www.letta.com/blog/agent-memory
- Mem0 LOCOMO Benchmark: https://arxiv.org/abs/2504.19413
- MemGPT Paper: https://research.memgpt.ai/
- Agent Memory Techniques (GitHub): https://github.com/NirDiamant/Agent_Memory_Techniques
- Memory Engineering 2026: https://medium.com/@mjgmario/memory-engineering-for-ai-agents
- Serokell Memory Patterns: https://serokell.io/blog/design-patterns-for-long-term-memory-in-llm-powered-architectures
