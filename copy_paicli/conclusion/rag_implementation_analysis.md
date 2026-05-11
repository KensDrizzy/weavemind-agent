# RAG 实现分析与 WeaveMind 改造计划

## 一、PaiCLI 的 RAG 实现总结

### 1.1 整体架构

PaiCLI 的 RAG 采用 **SQLite + Embedding + AST 解析** 的轻量级方案，专为 CLI 代码助手设计。

```
用户问题 → 查询分词(jieba) → 混合检索(语义+关键词) → 格式化结果 → LLM 回答
```

### 1.2 核心模块（10 个类）

| 模块 | 职责 |
|------|------|
| `CodeChunk` | 代码块数据模型（record 类型） |
| `CodeChunker` | AST 解析分块器 |
| `EmbeddingClient` | 向量化客户端（支持 Ollama/智谱/千问） |
| `VectorStore` | SQLite 向量存储 |
| `CodeAnalyzer` | AST 关系分析（extends/imports/calls 等） |
| `CodeRelation` | 关系数据模型 |
| `CodeIndex` | 索引入口 |
| `CodeRetriever` | 检索入口 |
| `RagQueryTokenizer` | 查询分词（jieba） |
| `SearchResultFormatter` | 结果格式化 |

### 1.3 代码分块策略（AST 解析）

**Java 文件**：
- **类级**：保留类声明 + 前 5 行（字段、签名），不塞整个类
- **方法级**：完整方法体单独成块
- 两者都存，检索时按粒度匹配

**非 Java 文件**：
- 按字符大小切，每段 ≤2000 字符
- 带起止行号，方便跳转定位

**容错**：JavaParser 解析失败时自动回退到按大小分段。

### 1.4 Embedding 方案

- **默认**：Ollama 本地模型（nomic-embed-text），免费断网可用
- **备选**：智谱/千问远程 API，环境变量切换
- **输入限制**：MAX_INPUT_CHARS = 2000，超出截断
- **向量维度**：768 维

### 1.5 向量存储（SQLite）

**为什么选 SQLite**：
- CLI 工具不适合 Milvus/Weaviate 这种重量级方案
- 个人项目几百到几千个代码块，1000 块约 3MB 内存
- 单次检索几十毫秒，完全够用

**存储格式**：
- 向量以 JSON 数组存入 TEXT 字段
- 余弦相似度手撸实现（点除模长）
- 事务保护批量插入

### 1.6 混合检索（核心亮点）

**三层加权**：

1. **语义检索打底**：查询向量化 → 余弦相似度 → TopK
2. **关键词加权**：
   - jieba 切词，保留代码标识符
   - 类名/方法名命中 +0.3
   - 文件路径命中 +0.1
   - 内容命中 +0.1
3. **类型加分**：
   - method 块 +0.15
   - class 块 +0.1
   - file 块 +0

**特殊机制**：
- **双重命中奖励**：语义 + 关键词都命中 → 额外 +0.1（只给一次）
- **同文件去重**：每个文件最多保留 2 条，避免大文件霸榜

### 1.7 代码关系图谱

用 AST 提取 5 种关系：
- `extends`：类继承
- `implements`：接口实现
- `imports`：依赖导入（过滤 JDK）
- `contains`：类包含方法
- `calls`：方法调用（简化版只记方法名）

**用途**：通过 `/graph Agent` 命令查看调用链，理解代码结构。

### 1.8 集成到 Agent

- 注册 `search_code` 工具，告诉 LLM 遇到代码问题优先调用
- 系统提示词更新，引导 LLM 主动检索
- `/index` 命令同步索引路径到 ToolRegistry

---

## 二、主流 Agent RAG 方案对比

### 2.1 传统 RAG vs Agentic RAG

| 维度 | 传统 RAG | Agentic RAG |
|------|----------|-------------|
| 检索方式 | 单次检索 → 生成 | Agent 主导，多次迭代检索 |
| 决策权 | 固定流程 | Agent 决定何时检索、检索什么 |
| 查询优化 | 无 | 自动改写、分解查询 |
| 结果验证 | 无 | Agent 评估结果质量，决定是否重新检索 |
| 多源融合 | 单一知识库 | 多工具、多知识库协同 |

### 2.2 2024-2025 主流方案

#### （1）Modular RAG（模块化 RAG）

**论文**：arXiv:2407.21059

**核心思想**：将 RAG 系统拆分为独立模块和算子，像乐高一样可重组。

**模块划分**：
- **Indexing**：文档加载 → 分块 → 向量化 → 存储
- **Retrieval**：查询改写 → 多路召回 → 重排序
- **Augmentation**：上下文压缩 → 多轮对话融合
- **Generation**：提示工程 → 答案生成 → 引用标注

**优势**：
- 线性、条件、分支、循环等多种模式可配置
- 路由、调度、融合机制灵活组合

#### （2）Graph RAG（图谱增强 RAG）

**代表**：Microsoft GraphRAG

**核心思想**：在向量检索基础上，构建知识图谱，支持多跳推理。

**流程**：
1. 实体抽取 → 构建知识图谱
2. 社区检测 → 层次化摘要
3. 查询时结合图谱遍历 + 向量检索

**适用场景**：
- 需要理解实体关系的复杂查询
- 全局性问题（"这个项目的整体架构是什么"）

#### （3）Corrective RAG（纠正式 RAG）

**核心思想**：Agent 评估检索结果质量，决定是否需要修正。

**流程**：
1. 初始检索
2. Agent 评估相关性（相关/模糊/不相关）
3. 不相关 → 改写查询重新检索
4. 模糊 → 补充 Web 搜索
5. 相关 → 直接生成

#### （4）Self-RAG（自省式 RAG）

**核心思想**：LLM 自己决定何时需要检索，而不是每次都检索。

**特殊 Token**：
- `[Retrieve]`：是否需要检索
- `[IsRel]`：检索结果是否相关
- `[IsSup]`：生成内容是否有检索支持
- `[IsUse]`：生成内容是否有用

#### （5）Adaptive RAG（自适应 RAG）

**核心思想**：根据查询复杂度动态选择策略。

**策略路由**：
- 简单查询 → 直接生成（无需检索）
- 中等查询 → 单次检索
- 复杂查询 → 多步检索 + 推理

### 2.3 代码场景的特殊考量

| 挑战 | 解决方案 |
|------|----------|
| 代码标识符精确匹配 | 混合检索（语义 + 关键词 + BM25） |
| 代码结构语义 | AST 解析，按类/方法/函数分块 |
| 跨文件依赖 | 代码关系图谱（imports/calls/extends） |
| 上下文窗口限制 | 分层检索（文件级 → 类级 → 方法级） |
| 代码更新频繁 | 增量索引 + 文件监听 |

---

## 三、WeaveMind RAG 改造计划

### 3.1 现状分析

**当前实现**（`rag/pipeline.py`）：
- ✅ 基础框架：Chroma + OpenAI Embeddings + RecursiveCharacterTextSplitter
- ❌ 未接入 CLI（`rag/` 和 `mcp/` 模块已定义但未集成）
- ❌ 无代码分块策略（按字符硬切，会切碎代码结构）
- ❌ 无混合检索（纯向量，精确匹配差）
- ❌ 无代码关系图谱
- ❌ 无增量索引

### 3.2 改造目标

1. **短期**：基础 RAG 可用，能索引和检索代码库
2. **中期**：AST 分块 + 混合检索，检索质量接近 PaiCLI
3. **长期**：Agentic RAG，Agent 主导检索决策

### 3.3 分阶段实施计划

#### Phase 1：基础 RAG 接入（1 周）

**目标**：让 RAG 模块接入 CLI，能跑通基本流程。

**任务**：
1. 修复 `rag/pipeline.py` 的依赖问题
2. 在 `ToolRegistry` 中注册 `search_code` 工具
3. 添加 `/index` 命令触发索引
4. 添加 `/search` 命令手动检索
5. 更新系统提示词，引导 LLM 使用 RAG

**代码改动**：
```python
# tools/builtin/search_code.py（新增）
class SearchCodeTool(WeaveMindTool):
    name = "SearchCode"
    description = "语义检索代码库，根据自然语言描述查找相关代码块"
    
    def _run(self, query: str, top_k: int = 5) -> str:
        results = self.rag_pipeline.search(query, k=top_k)
        return "\n---\n".join(results)
```

#### Phase 2：AST 代码分块（1-2 周）

**目标**：按代码结构分块，提升检索精度。

**任务**：
1. 实现 Python AST 解析器（参考 PaiCLI 的 JavaParser 方案）
2. 支持类级、方法级、函数级分块
3. 非 Python 文件回退到按大小分段
4. 分块时记录文件路径、起止行号、块类型

**技术选型**：
- Python：`ast` 标准库 + `libcst`（保留注释）
- 通用：`tree-sitter`（支持多语言）

**数据模型扩展**：
```python
# rag/models.py（新增）
class CodeChunk(BaseModel):
    file_path: str
    chunk_type: str  # "file" | "class" | "method" | "function"
    name: str        # 类名/方法名
    content: str
    start_line: int
    end_line: int
    embedding: Optional[list[float]] = None
```

#### Phase 3：混合检索（1 周）

**目标**：语义 + 关键词 + 类型加分，提升代码标识符匹配。

**任务**：
1. 实现查询分词（英文按空格 + 驼峰拆分，中文用 jieba）
2. 关键词检索（SQLite LIKE 或 BM25）
3. 混合评分算法（参考 PaiCLI 的三层加权）
4. 同文件去重
5. 双重命中奖励

**评分公式**：
```
final_score = semantic_score * 0.6 
            + keyword_boost * 0.3 
            + type_boost * 0.1
            + dual_hit_bonus * 0.1
```

#### Phase 4：向量存储升级（可选）

**目标**：根据项目规模选择合适的存储方案。

**方案对比**：

| 方案 | 适用规模 | 优势 | 劣势 |
|------|----------|------|------|
| Chroma（当前） | 小型项目 | 简单易用 | 性能一般 |
| SQLite | 中小型项目 | 轻量，无需额外服务 | 无原生向量索引 |
| FAISS | 中大型项目 | 高性能向量检索 | 需要预构建索引 |
| Milvus | 大型项目 | 分布式，生产级 | 部署复杂 |

**建议**：
- 保持 Chroma 作为默认（已集成）
- 提供 SQLite 备选（参考 PaiCLI）
- 未来可扩展 FAISS/Milvus

#### Phase 5：代码关系图谱（2 周）

**目标**：提取代码实体关系，支持调用链查询。

**任务**：
1. 实现 Python AST 关系提取（extends/imports/calls/contains）
2. 存储到 SQLite 关系表
3. 提供 `/graph` 命令查询
4. 检索时自动扩展相关上下文（如查方法时带上类定义）

**关系类型**：
- `inherits`：类继承
- `imports`：模块导入
- `calls`：函数/方法调用
- `contains`：模块包含类、类包含方法
- `uses`：变量引用

#### Phase 6：Agentic RAG（长期）

**目标**：Agent 主导检索决策，实现自适应 RAG。

**能力**：
1. **查询改写**：Agent 分析用户问题，生成更精确的检索查询
2. **多轮检索**：首次结果不满意时，自动改写查询重试
3. **结果验证**：Agent 评估检索结果相关性，决定是否需要补充检索
4. **策略路由**：根据查询复杂度选择单次/多次/无检索

**集成到 AgentLoop**：
```python
# core/agent_loop.py 改动
def _think(self, state: AgentState) -> dict:
    # ... 现有逻辑 ...
    
    # Agentic RAG：Agent 决定是否需要检索
    if self._should_retrieve(state):
        retrieval_results = self._retrieve(state)
        # 将检索结果注入上下文
        messages = self._inject_retrieval(messages, retrieval_results)
    
    # ... 调用 LLM ...
```

### 3.4 配置扩展

```yaml
# config.yaml 新增
rag:
  enabled: true
  provider: "chroma"  # chroma | sqlite | faiss
  chunk_size: 500
  chunk_overlap: 100
  embedding_model: "text-embedding-3-small"
  embedding_provider: "openai"  # openai | ollama | zhipu
  
  # 代码分块
  code_chunking:
    enabled: true
    languages: ["python", "java", "javascript", "typescript"]
    max_file_size: 50000  # 超过此大小跳过
    
  # 混合检索
  hybrid_search:
    enabled: true
    semantic_weight: 0.6
    keyword_weight: 0.3
    type_weight: 0.1
    max_per_file: 2
    
  # 关系图谱
  code_graph:
    enabled: false
    db_path: ".weavemind/code_graph.db"
```

### 3.5 预期收益

| 指标 | 当前 | Phase 1 后 | Phase 3 后 | Phase 6 后 |
|------|------|-----------|-----------|-----------|
| 代码理解准确率 | ~30% | ~60% | ~80% | ~90% |
| 检索延迟 | N/A | ~2s | ~1s | ~1s |
| 支持文件类型 | 无 | 文本文件 | 代码文件 | 代码+文档 |
| 检索策略 | 无 | 单次向量 | 混合检索 | 自适应 |

---

## 四、参考资源

### 论文
- [A Survey on RAG for LLMs](https://arxiv.org/abs/2404.10981)
- [Modular RAG](https://arxiv.org/abs/2407.21059)
- [GraphRAG](https://arxiv.org/abs/2404.16130)
- [Self-RAG](https://arxiv.org/abs/2310.11511)
- [Corrective RAG](https://arxiv.org/abs/2401.15884)

### 开源项目
- [LlamaIndex](https://github.com/run-llama/llama_index)：全栈 RAG 框架
- [LangChain RAG](https://python.langchain.com/docs/tutorials/rag/)：LangChain RAG 教程
- [RAGFlow](https://github.com/infiniflow/ragflow)：端到端 RAG 引擎
- [Kotaemon](https://github.com/Cinnamon/kotaemon)：开源 RAG UI

### PaiCLI 参考
- [PaiCLI RAG 实现](https://paicoding.com/column/17/5)：SQLite + AST + 混合检索
- [PaiCLI Plan-and-Execute](https://paicoding.com/column/17/2)：DAG 执行引擎

---

## 五、总结

WeaveMind 的 RAG 改造应遵循 **渐进式演进** 策略：

1. **先跑通**：Phase 1 让基础 RAG 可用，验证价值
2. **再优化**：Phase 2-3 提升检索质量，接近 PaiCLI 水平
3. **后智能化**：Phase 6 实现 Agentic RAG，Agent 自主决策

关键原则：
- **轻量优先**：CLI 工具不适合重量级基础设施
- **代码感知**：AST 分块 + 混合检索是代码 RAG 的核心
- **渐进集成**：每个 Phase 独立可用，不阻塞其他开发
