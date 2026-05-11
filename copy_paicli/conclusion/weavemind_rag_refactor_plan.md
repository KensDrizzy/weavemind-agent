# WeaveMind RAG 改造计划

## 一、现状分析

### 1.1 WeaveMind 当前架构

**Memory 系统（已完善）**
```
MemoryManager（门面）
├── CoreMemory — 3个固定块（user/project/persona），始终在 system prompt
├── LongTermMemory — JSON 持久化，bigram 相似度检索
└── ContextCompactor — Map-Reduce 摘要，超阈值自动压缩
```

**RAG 系统（stub）**
```
RAGPipeline（未接入）
├── RecursiveCharacterTextSplitter — 硬切，无代码感知
├── OpenAIEmbeddings — 硬编码，与当前 LLM 配置脱节
└── Chroma — 已有但无效
```

**关键问题**
1. RAG 完全未接入主流程 — 无工具注册、无 CLI 命令
2. 分块策略破坏代码结构 — 按字符切分会切碎方法/类
3. Embedding 硬编码 OpenAI — 与 MiMo/deepseek 配置冲突
4. Multi-Agent 模式无 RAG — Worker 无法获取代码上下文
5. Memory 与 RAG 割裂 — 两者都存储信息但互不感知

### 1.2 对比 PaiCLI

| 维度 | PaiCLI | WeaveMind 当前 |
|------|--------|----------------|
| 索引触发 | `/index` 命令 | ❌ 无 |
| 检索触发 | `search_code` 工具 | ❌ 无 |
| 代码分块 | AST 解析（类/方法级） | ❌ 按字符切分 |
| 混合检索 | 语义+关键词+类型加权 | ❌ 纯向量 |
| 代码关系 | 提取 extends/imports/calls | ❌ 无 |
| Embedding | Ollama 本地 + 智谱/千问 | ❌ 硬编码 OpenAI |

---

## 二、主流 Agent RAG 架构分析

### 2.1 2024-2025 关键模式

**1. Tool-Based RAG（Agent 主导）**
- Agent 自主决定何时检索、检索什么
- 通过 `SearchCode` 工具显式调用
- 优势：精准、节省 token、可解释
- 劣势：依赖 LLM 判断（弱模型可能误判）

**2. Memory-Augmented RAG（记忆增强）**
- 双层检索：文档索引 + 交互历史索引
- 新 query → 同时召回代码片段 + 过往相关记忆
- 代表：Mem0、Zep、LangGraph Store
- 关键：记忆去重、时间衰减、跨会话持久化

**3. Agentic RAG（自适应）**
- **Corrective RAG（CRAG）**：Agent 评估检索结果，不相关则改写 query 重试
- **Self-RAG**：LLM 生成 `[Retrieve]` 等特殊 token 自主决定是否检索
- **Adaptive RAG**：按 query 复杂度选择策略（无检索/单次/多轮）
- **GraphRAG**：知识图谱 + 向量检索，适合全局推理

**4. Code-Aware RAG（开发者场景）**
- **AST-Based Chunking**：方法/类/函数级分块，保留结构
- **Repo Map**：文件树+签名摘要，快速定位
- **Dependency-Aware**：跟踪 imports/calls 扩展上下文
- **Hybrid Retrieval**：向量 + BM25 + 标识符匹配

---

## 三、WeaveMind RAG 架构目标

### 3.1 设计原则

1. **渐进集成** — 每个阶段独立可用，不阻塞其他开发
2. **轻量优先** — CLI 工具不适合 Milvus/Weaviate 重量级方案
3. **代码感知** — AST 分块 + 混合检索是核心
4. **记忆融合** — LongTermMemory 与 RAG 协同，而非割裂

### 3.2 目标架构

```
┌─────────────────────────────────────────────────────────────────┐
│                    MemoryManager（统一门面）                      │
├──────────────┬──────────────┬──────────────────┬────────────────┤
│ CoreMemory   │ CodeRAG      │ LongTermMemory   │ SessionMemory  │
│ (系统提示)    │ (代码库)      │ (跨会话事实)      │ (当前对话)     │
├──────────────┼──────────────┼──────────────────┼────────────────┤
│ user block   │ AST chunks   │ project facts    │ recent msgs    │
│ project block│ embeddings   │ user preferences │ summary        │
│ persona block│ BM25 index   │ decisions        │                │
└──────────────┴──────┬───────┴──────────────────┘                │
                     │                                             │
                     ▼                                             │
┌──────────────────────────────────────────────────────────────────┐
│  Agent Loop                                                       │
│   ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐            │
│   │  Think  │→│ Retrieve│→│ Generate│→│  Act    │            │
│   └─────────┘  └────┬────┘  └─────────┘  └────┬────┘            │
│                     │                         │                 │
│                     ▼                         ▼                 │
│              SearchCodeTool              ToolExecution          │
│              - semantic_search           - Read/Write/Edit       │
│              - keyword_search            - Bash                  │
│              - hybrid_rerank                                      │
└──────────────────────────────────────────────────────────────────┘
```

---

## 四、分阶段实施计划

### Phase 1：基础 RAG 接入（1 周）

**目标**：让 RAG 模块接入 CLI，能跑通基本流程。

**核心任务**

| 任务 | 说明 | 代码位置 |
|------|------|----------|
| 重构 RAGPipeline | 支持可配置 Embedding Provider | `rag/pipeline.py` |
| 新增 SearchCodeTool | Agent 可调用的代码检索工具 | `tools/builtin/search_code.py` |
| 新增 IndexWorkspaceTool | 批量索引工作区文件 | `tools/builtin/index_workspace.py` |
| 注册工具 | 在 ToolRegistry 中注册 | `tools/registry.py` |
| CLI 命令 | `/index` `/search` 命令 | `cli/commands.py` |
| 配置扩展 | embedding_provider, enabled 开关 | `config.yaml` |

**Embedding Provider 配置**
```yaml
rag:
  enabled: true
  provider: chroma  # chroma | sqlite | faiss
  embedding_provider: openai  # openai | ollama | local
  embedding_model: text-embedding-3-small
  chroma_dir: .weavemind/chroma
  chunk_size: 500
  chunk_overlap: 100
```

**SearchCodeTool 设计**
```python
class SearchCodeInput(BaseModel):
    query: str = Field(description="检索需求，如'查找用户认证逻辑'")
    top_k: int = Field(default=5, description="返回结果数量")
    file_filter: Optional[str] = Field(default=None, description="文件通配符过滤，如'*.py'")

class SearchCodeTool(WeaveMindTool):
    """语义检索代码库。根据自然语言描述查找相关代码块。"""
    name = "SearchCode"
    description = "检索代码库中与需求相关的代码片段，用于理解代码结构或查找实现参考"
```

**接入点修改**
```python
# cli/app.py
class WeaveMindCLI:
    def __init__(self):
        # ... 现有初始化 ...
        # 初始化 RAG Pipeline（如果启用）
        if settings.get("rag.enabled", False):
            self.rag_pipeline = RAGPipeline()
        else:
            self.rag_pipeline = None
```

**预期产出**
- 用户可运行 `/index` 索引当前工作区
- Agent 可调用 `SearchCode` 工具检索代码
- 支持 OpenAI/Ollama 两种 embedding provider

---

### Phase 2：AST 代码分块（1-2 周）

**目标**：按代码结构分块，提升检索精度。

**核心任务**

| 任务 | 说明 | 代码位置 |
|------|------|----------|
| ASTChunker | 代码分块器，支持 Python/Java/JS | `rag/chunkers/ast_chunker.py` |
| CodeChunk 数据模型 | 结构化代码块定义 | `rag/models.py` |
| 语言检测 | 按文件扩展名选择解析器 | `rag/chunkers/` |
| 回退策略 | AST 失败时回退到字符切分 | `rag/chunkers/base.py` |

**CodeChunk 数据模型**
```python
from pydantic import BaseModel
from typing import Optional, Literal

class CodeChunk(BaseModel):
    file_path: str
    chunk_type: Literal["file", "class", "method", "function", "import"]
    name: str  # 类名/方法名/函数名
    content: str
    start_line: int
    end_line: int
    parent_name: Optional[str] = None  # 所属类/模块
    signature: Optional[str] = None  # 方法签名
    docstring: Optional[str] = None  # 文档字符串
    embedding: Optional[list[float]] = None
    
    def to_metadata(self) -> dict:
        """转换为向量存储 metadata"""
        return {
            "file_path": self.file_path,
            "chunk_type": self.chunk_type,
            "name": self.name,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "parent_name": self.parent_name,
        }
```

**Python AST Chunker 实现**
```python
import ast
from pathlib import Path

class PythonASTChunker:
    """基于 Python AST 的代码分块器。"""
    
    def chunk(self, file_path: str, content: str) -> list[CodeChunk]:
        """解析 Python 文件，返回类级和方法级代码块。"""
        try:
            tree = ast.parse(content)
        except SyntaxError:
            # AST 解析失败，回退到行级分块
            return self._fallback_chunk(file_path, content)
        
        chunks = []
        lines = content.split('\n')
        
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                chunk = self._create_class_chunk(node, lines, file_path)
                chunks.append(chunk)
                
                # 提取类中的方法
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        method_chunk = self._create_method_chunk(
                            item, lines, file_path, parent_name=node.name
                        )
                        chunks.append(method_chunk)
            
            elif isinstance(node, ast.FunctionDef):
                # 模块级函数
                chunk = self._create_function_chunk(node, lines, file_path)
                chunks.append(chunk)
        
        return chunks
```

**技术选型**
- Python：`ast` 标准库（内置、无依赖）
- Java：未安装 — 暂不实现或使用 tree-sitter
- JS/TS：tree-sitter
- 通用回退：`RecursiveCharacterTextSplitter`

**预期产出**
- 检索结果精确到方法/类级别
- 不再出现"方法被切一半"的情况
- 支持 Python 文件（Java/JS 后续扩展）

---

### Phase 3：混合检索（1 周）

**目标**：语义 + 关键词 + 类型加权，提升代码标识符匹配。

**核心任务**

| 任务 | 说明 | 代码位置 |
|------|------|----------|
| HybridRetriever | 混合检索器 | `rag/retriever.py` |
| KeywordIndex | SQLite FTS/BM25 索引 | `rag/keyword_index.py` |
| 评分融合 | 多层加权算法 | `rag/retriever.py` |
| 同文件去重 | 每个文件最多保留 N 条 | `rag/retriever.py` |

**评分公式（参考 PaiCLI）**
```python
def calculate_score(chunk, query_embedding, query_tokens) -> float:
    """
    final_score = semantic_score * 0.5 
                + keyword_boost * 0.3 
                + type_boost * 0.1
                + dual_hit_bonus * 0.1
    """
    # 1. 语义相似度 (0.5)
    semantic_score = cosine_similarity(query_embedding, chunk.embedding)
    
    # 2. 关键词加权 (0.3)
    keyword_score = 0.0
    content_lower = chunk.content.lower()
    for token in query_tokens:
        if token in chunk.name.lower():  # 类名/方法名命中
            keyword_score += 0.15
        elif token in chunk.file_path.lower():  # 文件路径命中
            keyword_score += 0.05
        elif token in content_lower:  # 内容命中
            keyword_score += 0.05
    keyword_score = min(keyword_score, 0.3)
    
    # 3. 类型加分 (0.1)
    type_boost = {
        "method": 0.08,
        "function": 0.08,
        "class": 0.05,
        "file": 0.0,
    }.get(chunk.chunk_type, 0.0)
    
    # 4. 双重命中奖励 (0.1)
    dual_hit_bonus = 0.1 if (semantic_score > 0.7 and keyword_score > 0.1) else 0.0
    
    return (semantic_score * 0.5 + 
            keyword_score * 0.3 + 
            type_boost * 0.1 + 
            dual_hit_bonus * 0.1)
```

**HybridRetriever 接口**
```python
class HybridRetriever:
    """混合检索器：向量检索 + 关键词检索 + 重排序。"""
    
    def __init__(self, vector_store, keyword_index):
        self.vector_store = vector_store
        self.keyword_index = keyword_index
    
    def retrieve(self, query: str, top_k: int = 10) -> list[RetrievalResult]:
        # 1. 向量检索召回候选集 (top_k * 3)
        candidates = self.vector_store.similarity_search(query, k=top_k * 3)
        
        # 2. 关键词检索召回候选集
        keyword_results = self.keyword_index.search(query, k=top_k * 2)
        
        # 3. 合并去重
        all_chunks = merge_candidates(candidates, keyword_results)
        
        # 4. 计算混合分数
        query_embedding = self.embeddings.embed_query(query)
        query_tokens = tokenize(query)
        
        scored = []
        for chunk in all_chunks:
            score = calculate_score(chunk, query_embedding, query_tokens)
            scored.append((score, chunk))
        
        # 5. 按分数排序 + 同文件去重
        scored.sort(key=lambda x: -x[0])
        results = deduplicate_by_file(scored, max_per_file=2)
        
        return results[:top_k]
```

**预期产出**
- 检索精度提升（精确匹配类名/方法名时召回率高）
- 支持自然语言描述 + 代码标识符混合查询
- 同文件最多保留 2 条，避免大文件霸榜

---

### Phase 4：Memory 与 RAG 融合（1 周）

**目标**：让 LongTermMemory 和 CodeRAG 协同工作，Agent 感知统一。

**核心任务**

| 任务 | 说明 | 代码位置 |
|------|------|----------|
| UnifiedRetriever | 统一检索门面 | `core/unified_retriever.py` |
| MemoryManager 扩展 | 协调长期记忆 + RAG | `core/memory.py` |
| 上下文组装 | 智能融合两种来源 | `core/memory.py` |
| 去重与冲突解决 | 避免重复信息 | `core/unified_retriever.py` |

**UnifiedRetriever 设计**
```python
class UnifiedRetriever:
    """
    统一检索器 — 同时检索长期记忆和代码库。
    
    返回信息优先级：
    1. CoreMemory — 始终注入
    2. SessionMemory — 当前对话上下文
    3. LongTermMemory — 相关历史事实
    4. CodeRAG — 相关代码片段
    """
    
    def __init__(self, long_term_memory: LongTermMemory, 
                 code_rag: CodeRAGPipeline):
        self.ltm = long_term_memory
        self.code_rag = code_rag
    
    def retrieve_for_query(self, query: str, context: dict = None) -> RetrievalContext:
        """
        为当前 query 检索所有相关上下文。
        
        Returns:
            RetrievalContext 包含：
            - facts: List[str] — 来自长期记忆的事实
            - code_snippets: List[CodeSnippet] — 来自 code RAG 的代码片段
            - relationships: List[CodeRelation] — 代码关系（可选）
        """
        # 并行检索
        facts = self.ltm.search(query, limit=5)
        code_snippets = self.code_rag.search(query, limit=5)
        
        # 去重：如果某 fact 已包含于 code snippet，降低权重
        facts = self._deduplicate(facts, code_snippets)
        
        return RetrievalContext(facts=facts, code_snippets=code_snippets)
```

**MemoryManager 改造**
```python
def build_system_message(self, query: str = "") -> SystemMessage:
    """构建完整的 system prompt，整合 CoreMemory + LTM + CodeRAG。"""
    parts = []
    
    # 1. CLAUDE.md
    # 2. MEMORY.md
    # 3. CoreMemory blocks
    
    # 4. 检索上下文（如果 query 不为空）
    if query and self.unified_retriever:
        context = self.unified_retriever.retrieve_for_query(query)
        
        if context.facts:
            parts.append(f"## 相关记忆\\n" + "\\n".join(f"- {f}" for f in context.facts))
        
        if context.code_snippets:
            snippets_text = format_code_snippets(context.code_snippets)
            parts.append(f"## 相关代码参考\\n{snippets_text}")
    
    # 5. 行为规范
    
    return SystemMessage(content="\\n\\n".join(parts))
```

**预期产出**
- Agent 自动感知代码库，无需显式调用 SearchCode
- 长期记忆和代码片段智能融合
- 查询相关时自动注入代码参考

---

### Phase 5：Multi-Agent RAG 集成（1-2 周）

**目标**：让 Multi-Agent 模式下的每个角色都能利用 RAG。

**核心任务**

| 任务 | 说明 | 代码位置 |
|------|------|----------|
| SharedRAGContext | 多 Agent 共享的 RAG 上下文 | `agents/agent_state.py` |
| Planner 代码感知 | 制定计划时参考代码结构 | `agents/orchestrator.py` |
| Worker RAG 工具 | Worker 可调用 SearchCode | `agents/worker.py` |
| Reviewer 代码审查 | 审查时检索相关代码规范 | `agents/reviewer.py` |
| 索引缓存 | 预索引共享代码库 | `agents/orchestrator.py` |

**SharedRAGContext 设计**
```python
class MultiAgentState(TypedDict):
    """Multi-Agent 共享状态 — 扩展以支持 RAG。"""
    messages: Annotated[list[AnyMessage], add_messages]
    next: str
    current_task: Optional[str]
    step_results: dict[str, str]
    review_status: Optional[str]
    retry_count: int
    # 新增 RAG 字段
    code_context: dict  # 预检索的代码上下文
    indexed_files: list[str]  # 已索引的文件列表
    global_code_graph: Optional[CodeGraph]  # 代码关系图（可选）
```

**Planner 改造**
```python
def _make_planner_node(self):
    """创建 Planner 节点 — 规划时考虑代码上下文。"""
    
    def planner_node(state: MultiAgentState) -> Command[Literal["supervisor"]]:
        # 检索与任务相关的代码结构
        task = state["messages"][-1].content
        code_context = self.code_rag.get_code_outline(task) if self.code_rag else None
        
        system_prompt = PLANNER_SYSTEM_PROMPT
        if code_context:
            system_prompt += f"\\n\\n当前代码库结构参考：\\n{code_context}"
        
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=task),
        ]
        response = self.llm.invoke(messages)
        
        return Command(
            update={
                "messages": [HumanMessage(content=response.content, name="planner")],
                "current_task": response.content,
                "code_context": code_context,  # 传递给 Worker
            },
            goto="supervisor",
        )
    
    return planner_node
```

**Worker 改造**
```python
def create_worker_node(..., code_rag: Optional[CodeRAGPipeline] = None):
    """创建 Worker 节点 — 支持代码检索工具。"""
    
    # Worker 的工具集扩展 SearchCode
    tools = list(base_tools)
    if code_rag:
        tools.append(SearchCodeTool(code_rag))
    
    agent = create_react_agent(llm, tools)
    
    def worker_node(state: MultiAgentState) -> Command[Literal["supervisor"]]:
        # 注入代码上下文到 system message
        system_msg = build_worker_system_prompt(state.get("code_context"))
        messages = [system_msg] + state["messages"]
        
        result = agent.invoke({"messages": messages})
        # ...
```

**预期产出**
- Planner 制定计划时参考代码结构
- Worker 可自主检索代码库
- Reviewer 审查时检索编码规范
- Multi-Agent 协同效率提升

---

### Phase 6：Agentic RAG（长期演进）

**目标**：Agent 主导检索决策，实现自适应 RAG。

**功能规划**

| 功能 | 说明 | 触发条件 |
|------|------|----------|
| Query 改写 | Agent 分析需求，生成更精确的检索 query | 所有检索请求 |
| 结果验证 | Agent 评估检索结果相关性 | 首次检索后 |
| 重试检索 | 结果不满意时改写 query 重试 | 相关性低于阈值 |
| 多源融合 | 同时检索代码 + Web + 文档 | 需要外部参考时 |
| 策略路由 | 根据 query 复杂度选择检索策略 | 初始阶段 |

**Adaptive RAG 路由逻辑**
```python
def route_retrieval_strategy(query: str, state: AgentState) -> str:
    """
    根据 query 特征选择检索策略：
    - simple: 无需检索（直接生成）
    - single: 单次代码检索
    - multi: 多轮检索 + 结果验证
    - hybrid: 代码 + Web 搜索
    """
    # 简单 query 模式匹配
    if is_simple_query(query):  # "你好"/"谢谢"等
        return "simple"
    
    # 需要外部知识的疑问
    if contains_external_reference(query):  # "最新的 Python 版本"
        return "hybrid"
    
    # 复杂分析类 query
    if requires_multi_file_reasoning(query):
        return "multi"
    
    return "single"
```

**Corrective RAG 循环**
```python
def corrective_retrieve(query: str, max_iterations: int = 3) -> list[CodeSnippet]:
    """纠正式检索 — 不满意则重试。"""
    for i in range(max_iterations):
        snippets = code_rag.search(query, top_k=5)
        
        # Agent 评估结果相关性
        evaluation = evaluate_retrieval(query, snippets)
        
        if evaluation.is_relevant:
            return snippets
        
        if evaluation.needs_reformulation:
            query = reformulate_query(query, evaluation.feedback)
            continue
        
        if evaluation.needs_web_search:
            snippets.extend(web_search(query))
            return snippets
    
    return snippets  # 用尽重试次数，返回最好的结果
```

---

## 五、配置与存储方案

### 5.1 完整配置示例

```yaml
rag:
  enabled: true
  
  # 向量存储配置
  vector_store:
    provider: chroma  # chroma | sqlite | faiss
    chroma_dir: .weavemind/chroma
  
  # Embedding 配置
  embedding:
    provider: openai  # openai | ollama | local
    model: text-embedding-3-small
    api_key_env: OPENAI_API_KEY
    base_url: null  # 自定义端点
  
  # 分块配置
  chunking:
    strategy: auto  # auto | ast | character
    chunk_size: 500
    chunk_overlap: 100
    languages:
      python:
        enabled: true
        chunk_types: [class, method, function]
      java:
        enabled: false
      javascript:
        enabled: false
  
  # 检索配置
  retrieval:
    strategy: hybrid  # hybrid | semantic | keyword
    top_k: 10
    max_per_file: 2
    semantic_weight: 0.5
    keyword_weight: 0.3
    type_weight: 0.1
    
  # 索引配置
  indexing:
    auto_index: false  # 启动时自动索引
    watch_files: false  # 监听文件变化
    incremental: true  # 增量索引
  
  # Memory 融合配置
  memory_integration:
    enabled: true
    facts_weight: 0.4
    code_weight: 0.6
```

### 5.2 存储结构

```
.weavemind/
├── chroma/                    # Chroma 向量存储
│   ├── chroma.sqlite3
│   └── ...
├── keyword_index.db          # SQLite FTS 索引
├── code_graph.db             # 代码关系图（可选）
├── index_metadata.json       # 索引元数据（文件哈希、时间戳）
└── memory/
    ├── core.json             # 核心记忆块
    └── long_term.json        # 长期记忆
```

---

## 六、预期收益

### 6.1 量化指标

| 指标 | 当前 | Phase 2 后 | Phase 4 后 | Phase 6 后 |
|------|------|-----------|-----------|-----------|
| 代码理解准确率 | ~30% | ~65% | ~80% | ~90% |
| 检索延迟 | N/A | ~2s | ~1.5s | ~1.5s |
| 支持语言 | 0 | Python | Python | Python + Java/JS |
| 检索策略 | 无 | 单次向量 | 混合检索 | 自适应 |
| Multi-Agent 代码感知 | 无 | 部分 | 完整 | 完整 |

### 6.2 用户体验收益

1. **自然交互**：无需显式调用，Agent 自动感知代码库
2. **精准回答**：方法级检索，定位到具体实现
3. **上下文连贯**：长期记忆记住用户偏好，跨会话一致
4. **团队协作**：Multi-Agent 共享代码上下文，协同效率提升

---

## 七、风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| AST 解析失败 | 分块退化 | 回退到行级分块，记录日志 |
| Embedding 服务不可用 | RAG 失效 | 降级到关键词检索，提示用户 |
| 索引过大 | 内存/性能问题 | 增量索引 + 文件监听，跳过未变更 |
| 隐私泄露 | 敏感代码被索引 | 支持 `.ragignore` 文件排除 |
| Multi-Agent RAG 冲突 | 状态混乱 | 只读共享，每个 Worker 独立检索 |

---

## 八、下一步行动

立即开始 **Phase 1**，优先级如下：

1. ✅ **今天就做**：重构 `RAGPipeline` 支持可配置 embedding
2. **本周完成**：实现 `SearchCodeTool` 并注册
3. **本周完成**：添加 `/index` 和 `/search` CLI 命令
4. **下周开始**：AST Chunker 开发

实施前请先确认：
- Embedding provider 选择（建议先支持 Ollama 本地免费）
- 是否需要在 Phase 1 就接入 Agent 自动检索，还是等 Phase 4

---

*此文档基于 rag_implementation_analysis.md、Claude Code 设计分析、以及 2024-2025 主流 Agent RAG 研究综合而成。*
