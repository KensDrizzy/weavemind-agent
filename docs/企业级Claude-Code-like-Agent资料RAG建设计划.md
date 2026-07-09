# 企业级 Claude Code-like Agent 资料 RAG 建设计划

目标：在 WeaveMind 中新增一套面向真实用户和企业客户的通用资料 RAG。它能接收用户上传的 PDF、Word、图片、Markdown、HTML 等资料，完成解析、分块、向量化、检索、问答、引用溯源、权限隔离和企业级运维，并和现有代码 RAG、Memory、MCP、Agent Loop 协同工作。

本文写于 2026-07-01，结合当前仓库实现和主流 RAG/Agent 工程实践。

## 设计原则

1. 代码 RAG 和资料 RAG 分开。当前 `rag/CodeRAGPipeline` 面向本地代码库；资料 RAG 面向用户上传内容，两者的解析、chunk、权限和引用方式不同。
2. 检索主线直接按 Milvus 设计。开发环境可以用 Milvus Lite 或 Milvus Standalone，本地快速 demo 才允许 Chroma fallback；企业版主线优先使用 Milvus dense vector + Milvus Sparse-BM25 hybrid search，复杂关键词搜索场景再接 OpenSearch/Elasticsearch。
3. 检索质量优先于组件堆叠。必须建立评测集，用 Recall@5、MRR、nDCG、引用准确率、拒答准确率、P95/P99 延迟和 QPS 做选型依据。
4. 所有检索结果必须可溯源。企业用户不只要答案，还要知道答案来自哪份文档、哪一页、哪个段落或表格。
5. 权限是检索链路的一部分。多租户、项目、用户、角色、文档 ACL 必须在召回阶段生效，不能只在回答阶段过滤。

## 主流实现参考

| 方向 | 主流做法 | 对本项目的启发 |
| --- | --- | --- |
| Claude Code-like Agent | CLAUDE.md/Memory、工具权限、Hooks、MCP、subagents、项目上下文 | 资料 RAG 应作为 Agent 工具和上下文服务，而不是直接塞进 system prompt |
| OpenAI File Search | 文件上传到 vector store 后自动 chunk、embed、检索 | 产品体验上应提供“上传即建知识库”，但底层本项目自建以支持私有化 |
| RAGFlow | 深度文档理解、模板化 chunk、引用问答 | PDF/Word/图片解析质量是 RAG 质量的上限 |
| Docling/Unstructured/Marker | 文档解析为结构化元素、Markdown/JSON、OCR、表格 | 资料 RAG 要先结构化，再 chunk，不要直接按字符切 |
| Milvus | Dense vector + Sparse-BM25 + hybrid search、属性过滤、扩展部署 | 主线采用 Milvus，优先用内置 BM25 做关键词检索 |
| Elasticsearch/OpenSearch/Azure AI Search | 成熟全文检索、复杂分词、RRF、运营搜索能力 | 作为复杂关键词搜索、企业已有搜索基础设施或审计检索的增强后端 |
| Weaviate | 向量库 + 内置 hybrid search | 只作为需要一体化 hybrid search 但不选 Milvus 时的备选 |

参考来源见文末。

## 技术选型结论

### 1. 向量数据库

最终推荐：

- 开发/个人版：Milvus Lite 或 Milvus Standalone；Chroma 只作为无 Docker/无 Milvus 环境的 fallback。
- 企业单机版：Milvus Standalone。
- 企业规模化版：Milvus Distributed。
- 保留 Weaviate adapter，但只作为备选，不作为主线。

选择原因：

- 当前 WeaveMind 已有 Chroma，用它做 demo 成本最低，但这不是企业路线。
- 真实企业用户会要求多租户、属性过滤、备份恢复、监控、并发、P99 和水平扩展。Milvus 官方部署形态覆盖 Lite、Standalone、Distributed，并给出从几百万向量到十亿级向量的扩展路径。
- Milvus 负责 dense vector retrieval，也可以用内置 Sparse-BM25 做 full-text/关键词检索，再用 hybrid search/RRF 类 ranker 融合。这样第一版可以少维护一个搜索集群。
- OpenSearch/Elasticsearch 不是必选项，而是增强项：当企业需要复杂中文分词、同义词运营、搜索高亮、聚合分析、审计检索、跨业务日志搜索时再接入。
- Weaviate 原生支持 vector + BM25 hybrid search，但如果已经明确使用 Milvus，则 Weaviate 不作为主线。

第一阶段不要把业务代码写死到某个向量库，定义接口：

```python
class VectorStoreAdapter:
    def upsert_chunks(self, chunks): ...
    def search(self, query_vector, top_k, filters): ...
    def delete_by_document(self, doc_id): ...
    def rebuild_collection(self, collection): ...
```

### 2. 关键词检索

最终推荐：

- 默认方案：Milvus 内置 Full-Text Search / Sparse-BM25。
- 开发/个人版：Milvus Lite/Standalone + Milvus BM25；SQLite FTS5 仅作为极轻量 fallback。
- 企业版：Milvus BM25 仍作为主线；当搜索需求超过 Milvus full-text 能力时，接 OpenSearch 或 Elasticsearch。
- 如果团队已有 Elastic 技术栈，增强后端优先 Elasticsearch；如果偏开源、自托管和成本控制，增强后端优先 OpenSearch。

选择原因：

- 纯向量检索不擅长精确术语、编号、错误码、接口名、合同条款号。
- 微信文章中提到的实现是“BM25 分离部署，用 Elasticsearch”，所以没有采用 Weaviate 的内置 BM25 优势。这是文章里的方案，不是唯一方案。
- Milvus 2.5+ 已支持原生 Full-Text Search：通过 analyzer/tokenizer 将文本转为 sparse vector，并用 BM25 metric 做关键词相关性检索；`hybrid_search()` 可以组合 dense semantic search 和 sparse BM25 search。
- 企业已有日志和搜索基础设施时，OpenSearch/Elasticsearch 更容易接入监控、权限、备份、复杂中文分词、同义词和业务词典。
- 生产融合策略采用 RRF 或 Milvus hybrid ranker。RRF 的好处是不用强行把 BM25 分数和 dense vector 相似度归一到同一尺度。

### 3. 文档解析

最终推荐：

- 主解析器：Docling。
- 备用解析器：Unstructured。
- 特殊 PDF/扫描件增强：Marker 或 MinerU 作为可选插件。
- OCR：优先使用 Docling 内置能力；中文扫描件可接 PaddleOCR；高价值低吞吐场景可接视觉模型做二次校正。

选择原因：

- 企业资料里最难的是 PDF、扫描件、表格、列表、页眉页脚、跨页表格和阅读顺序。
- Docling 明确支持 PDF、DOCX、PPTX、XLSX、HTML、图片等格式，并提供 layout、reading order、table structure、OCR 等能力。
- Unstructured 的 partitioning 能把原始文档拆成 Title、NarrativeText、ListItem 等元素，适合做元素级 chunk。
- Marker/MinerU 更适合复杂 PDF 转 Markdown/JSON 的增强链路，但依赖和资源成本较高，不放在 MVP 必选项。

### 4. Embedding 模型

最终推荐：

- 私有化默认：BGE-M3 或 Qwen3-Embedding 系列。
- 中文企业增强：Qwen3-Embedding-4B/8B 或 BGE-M3，根据硬件成本和评测结果选择。
- 云模型选项：OpenAI `text-embedding-3-large`、Voyage embedding。
- 领域客户：积累困难正负例后做 embedding 微调，微调后全量重建索引。

选择原因：

- 企业客户常要求数据不出域，必须支持私有化 embedding。
- BGE-M3 支持多语言、多粒度，并支持 dense/sparse/multi-vector 思路，适合中英文混合和长文档。
- Qwen3-Embedding 在中文、多语言和代码检索上有较强表现，适合中文企业和代码/文档混合场景。
- OpenAI 和 Voyage 适合 SaaS 版和允许云调用的客户，质量强、维护少。

接口上必须记录 embedding 版本：

```text
embedding_provider
embedding_model
embedding_dimension
embedding_revision
chunker_version
parser_version
index_version
```

只要模型、维度、chunker 或 parser 变更，就进入 reindex 队列。

### 5. Reranker

最终推荐：

- 私有化默认：BGE reranker 或 Qwen3-Reranker。
- 云选项：Voyage reranker 或主 LLM rerank。
- MVP：先用启发式 + LLM 可选，不阻塞主链路。

选择原因：

- embedding 召回负责“不要漏”，reranker 负责“把最相关排前面”。
- 企业问答通常更看重 top 3 是否可靠，rerank 对最终答案影响很大。
- 私有化客户不能依赖外部 API，需要本地 reranker。

### 6. Chunk 策略

最终推荐：结构感知 chunk，而不是固定 token chunk。

策略：

- 标题层级作为 breadcrumb 写入每个 chunk。
- 普通段落：800-1200 token 上限。
- 合同/条款/制度：优先按章节、条款、编号切分；关键条款可放宽到 1500 token 左右。
- 表格：小表整体保留；大表按“表头 + 行组”切分；跨页表格合并后再切。
- 列表：每个列表项保留前导句。
- 图片：保存 OCR 文本、caption、图片路径、页码、bbox。
- overlap：80-150 token，按句子边界对齐。

每个 chunk 必须带 metadata：

```text
tenant_id
workspace_id
collection_id
doc_id
source_file
page_number
section_path
element_type
bbox
created_at
acl_hash
parser_version
chunker_version
```

## 目标架构

```text
用户/企业空间
  |
  v
Upload API / CLI / Agent Tool
  |
  v
Ingestion Queue
  |
  +--> 文件安全扫描 / 类型识别 / 去重
  +--> 原文件入对象存储 S3/MinIO
  +--> Docling/Unstructured/Marker 解析
  +--> 结构化元素 JSON + Markdown
  +--> Chunker 结构感知分块
  +--> Embedding Worker
  +--> Milvus dense vector 索引
  +--> Milvus sparse BM25/full-text 索引
  +--> 可选 OpenSearch/Elasticsearch 关键词增强索引
  +--> Postgres metadata/ACL/index status

Agent 查询
  |
  v
Query Router
  |
  +--> SearchCode：本地代码库实现问题
  +--> SearchKnowledge：上传资料/企业知识库
  +--> MemorySearch：用户偏好/历史事实
  +--> WebSearch：外部最新资料
  |
  v
Hybrid Retrieval: Milvus dense vector + Milvus Sparse-BM25 + metadata filters
  |
  v
RRF Fusion -> Rerank -> Context Packing -> LLM Answer with Citations
```

## 模块规划

新增目录：

```text
knowledge_rag/
  models.py
  pipeline.py
  parsers/
    base.py
    docling_parser.py
    unstructured_parser.py
    marker_parser.py
  chunkers/
    structural_chunker.py
    table_chunker.py
    image_chunker.py
  embeddings/
    provider.py
    openai_provider.py
    local_provider.py
  stores/
    vector_base.py
    milvus_store.py
    chroma_store.py          # fallback only
    keyword_base.py
    milvus_bm25_store.py
    sqlite_fts_store.py
    opensearch_store.py
    metadata_store.py
  retrieval/
    hybrid.py
    rrf.py
    rerank.py
    context_packer.py
  tools.py
```

新增工具：

- `IndexKnowledge`：索引文件或目录。
- `SearchKnowledge`：检索资料片段。
- `AskKnowledge`：基于资料问答，必须带引用。
- `ListKnowledge`：列出知识库文档。
- `DeleteKnowledge`：删除文档和索引。
- `ReindexKnowledge`：parser/chunker/embedding 版本变更后重建索引。

新增 CLI：

```bash
/kb add <file-or-dir> [--collection name]
/kb search <query> [--collection name]
/kb ask <query> [--collection name]
/kb list
/kb delete <doc_id>
/kb reindex [--collection name]
```

## Agent 集成方案

WeaveMind 要像 Claude Code 一样，关键不是让所有内容常驻 prompt，而是把上下文变成可调用工具：

- 代码相关问题：优先 `SearchCode`。
- 上传资料、合同、制度、图片、PDF、Word 相关问题：优先 `SearchKnowledge` 或 `AskKnowledge`。
- 用户偏好、项目决策：用 `MemorySearch`。
- 最新外部信息：用 `WebSearch`。
- 企业内部系统：通过 MCP 接入。

需要在 `AgentLoop` 增加类似 `_maybe_force_search_code` 的资料检索触发：

```text
如果用户问题包含“上传的文件/这份 PDF/合同/制度/图片/资料/知识库/文档里”
并且 KnowledgeRAG 已启用，
首跳构造 SearchKnowledge 或 AskKnowledge tool_call。
```

同时要避免直接把整份资料塞进 system prompt。Agent 只拿 top chunks 和引用元数据。

## 企业级安全与治理

必须从第一版设计：

- 多租户隔离：tenant_id/workspace_id/collection_id 全链路过滤。
- 文档 ACL：检索前过滤，不允许回答阶段才过滤。
- 审计日志：上传、解析、检索、回答、删除、导出都记录。
- 文件安全：大小限制、MIME 检测、病毒扫描、压缩包炸弹防护。
- 数据加密：对象存储、Postgres、Milvus/OpenSearch 磁盘加密；传输 TLS。
- 密钥管理：企业 KMS 或环境隔离，不把 API key 写入索引元数据。
- 权限策略：沿用 Claude Code-like 的 allow/ask/deny 思路，高风险工具走 HITL。
- MCP 治理：MCP server allowlist、工具级权限、PreToolUse hook 可阻断。
- 数据生命周期：支持文档删除、租户删除、索引删除、备份恢复、保留周期。

## 评测体系

每次变更 parser、chunker、embedding、vector DB、reranker，都跑离线评测。

核心指标：

- Recall@5 / Recall@10
- MRR / nDCG
- answer faithfulness
- citation accuracy
- unsupported question refusal accuracy
- P50/P95/P99 retrieval latency
- end-to-end latency
- indexing throughput
- QPS under 10/50/100 concurrency
- storage cost per 10k documents

评测集构造：

- 每个文档人工标注 5-20 个问题。
- 保留困难负例：术语相近但业务含义不同。
- 覆盖表格、列表、跨页、扫描件、图片、代码块、合同条款。
- 每个答案绑定 gold chunk/page/citation。

## 分阶段计划

### Phase 0：基线与接口设计（2-3 天）

目标：先定边界，不急着接复杂依赖。

- 定义 `KnowledgeDocument`、`KnowledgeChunk`、`KnowledgeSearchResult`。
- 定义 `VectorStoreAdapter`、`KeywordStoreAdapter`、`DocumentParser`、`Chunker` 接口。
- 增加 `knowledge_rag.*` 配置段。
- 建立 `.weavemind/knowledge/` 目录结构。
- 写最小单测：模型序列化、metadata、adapter mock。

验收：

- 不接真实解析器也能跑通 mock ingest/search。

### Phase 1：本地 MVP（1 周）

目标：PDF/DOCX/TXT/MD/图片 OCR 文本可索引、可检索，主链路使用 Milvus。

- 使用 Docling 做主解析器。
- Milvus Lite/Standalone 作为默认 dense vector 后端。
- Milvus Full-Text Search / Sparse-BM25 作为默认关键词检索后端。
- SQLite FTS5 仅作为本地极简 fallback；OpenSearch 只在需要复杂搜索能力时启动。
- 实现结构感知 chunker 的第一版：标题、段落、表格、列表。
- 实现 `SearchKnowledge` 和 `/kb add`、`/kb search`。
- 复用现有 embedding 配置。
- 复用现有 RRF/rerank 思路，但默认融合策略改为 RRF。

验收：

- 10 份混合格式资料可索引。
- 查询能返回 chunk、页码、文件名。
- 重复上传同文件可 hash 去重。

### Phase 2：问答与引用（1 周）

目标：用户能问“这份资料里说了什么”，并拿到带引用的答案。

- 实现 `AskKnowledge`。
- 实现 context packing：按 token budget 组装 top chunks。
- 回答格式强制引用 `[文件名 p.12]`。
- 无证据时拒答。
- CLI 增加 `/kb ask`。
- 增加 Agent 触发规则 `_maybe_force_search_knowledge`。

验收：

- 问答结果至少包含一个可追踪引用。
- 无关问题不会编造答案。

### Phase 3：企业存储后端（1-2 周）

目标：把 Milvus hybrid search 和 metadata 存储补齐为生产架构，并预留 OpenSearch/Elasticsearch 增强后端。

- 完善 `MilvusVectorStoreAdapter` 的 collection schema、metadata filter、批量 upsert、删除和重建。
- 实现 Milvus dense + sparse BM25 的 collection schema 和 hybrid search。
- 实现可选 `OpenSearchKeywordStoreAdapter` 或 `ElasticsearchKeywordStoreAdapter`。
- Postgres metadata store。
- S3/MinIO 原文件和解析结果存储。
- 支持 collection、tenant、workspace metadata filter。
- 支持 `DeleteKnowledge` 和 `ReindexKnowledge`。

验收：

- 同一套 pipeline 默认跑 Milvus dense + sparse hybrid；可通过配置切换到 Chroma/SQLite fallback 或 Milvus + OpenSearch 增强模式。
- 检索阶段强制 tenant/workspace/ACL filter。

### Phase 4：异步索引与可观测性（1 周）

目标：真实用户上传大文件时不阻塞 Agent。

- 引入 ingestion job：pending/running/succeeded/failed。
- Worker 异步解析、chunk、embedding、写索引。
- 增加进度查询和失败重试。
- 增加 metrics：解析耗时、embedding 批次、索引耗时、检索耗时。
- 增加审计日志。

验收：

- 100MB 级资料上传后后台处理。
- 用户可查看索引状态。
- 失败文档可重试。

### Phase 5：检索质量增强（2 周）

目标：从“能搜”变成“搜得准”。

- 加 reranker：本地 BGE/Qwen reranker，云端 Voyage/OpenAI 可选。
- query rewrite：同义词、缩写、指代消解、领域词典。
- 表格检索增强：表格摘要 + 行级 chunk + 原表引用。
- 图片检索增强：OCR + caption；必要时多模态 embedding。
- 构建第一版评测集。
- 建立 nightly eval。

验收：

- Recall@5、MRR、citation accuracy 有可视化报告。
- parser/chunker/embedding 变更有量化对比。

### Phase 6：企业治理与交付（2-4 周）

目标：可以给真实企业试点。

- RBAC/ACL 接入。
- 租户级数据隔离。
- 文件安全扫描。
- 数据删除和索引清理。
- 备份恢复方案。
- 管理后台或 CLI 管理命令。
- MCP 工具 allowlist 和 hook 审计。
- 压测报告和容量规划。

验收：

- 可部署到单企业私有环境。
- 有部署文档、运维文档、压测报告和安全说明。

## 推荐配置

开发版：

```yaml
knowledge_rag:
  enabled: true
  parser:
    provider: docling
  vector_store:
    provider: milvus
    uri: ./weavemind_knowledge.db  # Milvus Lite；也可替换为 http://localhost:19530
  keyword_store:
    provider: milvus_bm25
  retrieval:
    fusion: rrf
    top_k: 8
    rerank: heuristic
```

企业版：

```yaml
knowledge_rag:
  enabled: true
  parser:
    provider: docling
    fallback: unstructured
  object_store:
    provider: s3
    bucket: weavemind-knowledge
  metadata_store:
    provider: postgres
  vector_store:
    provider: milvus
    uri: http://milvus:19530
    collection_prefix: knowledge
  keyword_store:
    provider: milvus_bm25
    # optional_provider: opensearch
    # index_prefix: knowledge
  embedding:
    provider: local
    model: BAAI/bge-m3
  rerank:
    provider: local
    model: bge-reranker-v2-m3
  retrieval:
    fusion: rrf
    top_k: 12
    max_context_chunks: 8
```

## 当前项目落地建议

第一步不要替换现有 `rag/`。保留代码 RAG，新增 `knowledge_rag/`。

需要改的入口：

- `tools/registry.py`：注册 `SearchKnowledge`、`AskKnowledge`。
- `cli/commands.py`：增加 `/kb` 子命令。
- `core/agent_loop.py`：增加资料类问题的首跳检索。
- `config.yaml.example`：增加 `knowledge_rag` 配置。
- `tests/`：新增 `test_knowledge_rag.py`、`test_knowledge_rag_tools.py`。

可以复用的能力：

- 现有 embedding provider 创建逻辑。
- 现有 Chroma 持久化经验。
- 现有 SQLite FTS5/BM25 思路。
- 现有 QueryRewriter、ResultReranker、SearchCache 设计。
- 现有 ToolRegistry、HITL、AgentLoop 工具调用链。

## 风险与取舍

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| PDF 解析质量差 | 检索召回差，引用不可信 | Docling 主解析，Unstructured/Marker fallback，保留 parsed artifact 便于排查 |
| 只用向量检索 | 编号、术语、表格查询漏召回 | 必须做 BM25 + vector hybrid |
| chunk 固定切分 | 语义断裂、表格丢表头 | 结构感知 chunker |
| embedding 云调用受限 | 企业客户不能用 | 支持本地 BGE/Qwen embedding |
| 权限后过滤 | 可能泄露 chunk | metadata filter 在召回阶段执行 |
| parser/chunker 变化 | 旧索引失效 | index version + reindex 队列 |
| 复杂部署拖慢 MVP | 第一版做不出来 | Milvus Lite 或 Standalone 作为默认开发形态，Chroma/SQLite 只做 fallback |

## 最终建议

如果目标是“像 Claude Code 一样给真实用户和企业用”，不要把资料 RAG 做成一个简单的“上传文件 + Chroma search”功能。那适合 demo，但不适合企业。

正确路线是：

1. 本地 MVP 用 Milvus Lite 或 Milvus Standalone 验证主链路，关键词检索默认用 Milvus Sparse-BM25。
2. 架构上从第一天抽象存储后端。
3. 企业主线按 Milvus dense + sparse hybrid search + Postgres + S3/MinIO 设计；OpenSearch/Elasticsearch 作为复杂搜索增强项。
4. 文档解析优先投入 Docling/Unstructured 和结构感知 chunk。
5. 检索默认 hybrid + RRF + rerank。
6. 用评测集驱动每个选型，不用排行榜驱动。

这条路线既能快速落地，又能自然演进到企业生产环境。

## 参考资料

- 公众号文章：<https://mp.weixin.qq.com/s/W6s6PtqwNMy340SoSSfQog>
- Claude Code Hooks：<https://code.claude.com/docs/en/hooks>
- Claude Code Permissions：<https://code.claude.com/docs/en/permissions>
- Claude Code Security：<https://code.claude.com/docs/en/security>
- Claude Code Memory：<https://code.claude.com/docs/en/memory>
- Claude Code extension overview：<https://code.claude.com/docs/en/features-overview>
- OpenAI File Search：<https://platform.openai.com/docs/guides/tools-file-search>
- OpenAI Embeddings：<https://developers.openai.com/api/docs/guides/embeddings>
- Milvus deployment options：<https://milvus.io/docs/install-overview.md>
- Milvus full-text search：<https://milvus.io/docs/full-text-search.md>
- Milvus multi-vector hybrid search：<https://milvus.io/docs/multi-vector-search.md>
- Weaviate hybrid search：<https://docs.weaviate.io/weaviate/search/hybrid>
- Elasticsearch RRF：<https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion>
- OpenSearch RRF hybrid search：<https://opensearch.org/blog/introducing-reciprocal-rank-fusion-hybrid-search/>
- Azure AI Search RRF：<https://learn.microsoft.com/en-us/azure/search/hybrid-search-ranking>
- Docling：<https://docling-project.github.io/docling/>
- Unstructured partitioning：<https://docs.unstructured.io/open-source/core-functionality/partitioning>
- Marker：<https://github.com/datalab-to/marker>
- RAGFlow：<https://ragflow.io/docs/>
- BGE-M3：<https://bge-model.com/bge/bge_m3.html>
- Qwen3 Embedding：<https://qwenlm.github.io/blog/qwen3-embedding/>
