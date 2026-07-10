# WeaveMind 多模态输入升级计划

> 参考来源：`docs/多模态.pdf`（PaiCLI 多模态升级方案，Java 实现）
> 目标：让 WeaveMind 具备图片输入与理解能力，支持 `@image` 引用、剪贴板抓图、MCP 截图注入、历史图片裁剪。
> 撰写时间：2026-07-10

---

## 一、PaiCLI 方案要点提炼

PaiCLI 的多模态升级围绕 **"图片作为消息的一部分进入 LLM 上下文"** 展开，关键技术点：

| 模块 | 核心做法 | 对 WeaveMind 的启示 |
|------|---------|-------------------|
| 模型路由 | 根据模型名前缀 `glm-5v` 切换 `MULTIMODAL_API_URL` | 在 `llm_factory.py` 增加"模型能力表"，自动路由到支持 vision 的 endpoint |
| 消息协议 | `Message.content` 从 `String` 升级为 `List<ContentPart>`，兼容 text / image_url | LangChain `HumanMessage(content=[...])` 原生支持多模态列表，可直接复用 |
| 用户输入 | `@image:./shot.png` / `@clipboard` 引用解析 | CLI prompt_toolkit 输入预处理，解析图片引用并加载 |
| MCP 截图 | 工具返回 `image` content 时，用 **tool message 文本 fallback + user message 真图** 的方式注入 | `mcp_client/tools.py` 的 `_format_result()` 已识别 `[图片数据]`，需把 base64 转回消息链 |
| 多图输入 | 单条消息可附加多张图片 | 一条 `HumanMessage` 的 content 列表可包含多个 image block |
| 图片预处理 | 三层决策：① 小图无 alpha 直通；② 有 alpha 白底 flatten；③ 超大图等比缩放 + JPEG 质量降级 | 用 Pillow 实现同等工作流，5MB / 2000x2000 作为阈值 |
| 上下文裁剪 | `pruneHistoricalImagePayloads()`：每轮只保留最近一轮图片实体，旧图替换为占位文本 | 在 `ContextCompactor` 或 `_think` 前增加图片裁剪步骤，避免 token 爆炸 |

---

## 二、WeaveMind 现状差距

1. **消息 content 全按字符串处理**
   - `cli/app.py:417`、`agent_loop.py:1237/1270` 等所有 `HumanMessage(content=user_input)` 只接受字符串。
   - `ContextCompactor.count_tokens()` 用 `tiktoken` 对 `m.content` 编码，未处理多模态 content 列表。

2. **无模型能力感知**
   - `llm_factory.create_llm()` 只看 provider / base_url，没有"当前模型是否支持 vision"的判断。
   - 绑定 tools 时也未区分模型能力，vision 模型需要额外配置或自动探测。

3. **MCP 图片结果只生成占位文本**
   - `mcp_client/tools.py:147-149` 把 image content 转成 `[图片数据: image/png]`，模型看不到实际内容。
   - 没有将 MCP 返回的 image base64 重新注入对话历史的机制。

4. **CLI 无图片输入入口**
   - `prompt_toolkit` 只接收文本，没有 `@image` / `@clipboard` 解析、剪贴板抓图、文件拖拽支持。

5. **上下文压缩未考虑图片**
   - `ContextCompactor` 只按 token 阈值摘要，未对历史图片做 payload 裁剪。
   - 若每轮都带截图，base64 会迅速撑满上下文。

6. **缺少图片安全/权限控制**
   - 无文件类型白名单、大小上限、路径校验；用户可能通过 `@image:/etc/passwd` 读取任意文件。

---

## 三、升级方案设计

### 3.1 总体架构

```
用户输入
  │
  ├─ 文本 ─────────────────────┐
  ├─ @image:path ──→ ImageReferenceParser ──→ ImageProcessor ──┤
  ├─ @clipboard ───→ ClipboardCapture ───────→ ImageProcessor ──┤ → HumanMessage(content=[text, image_url, ...])
  └─ MCP tool image ───→ ToolResultFormatter ──→ ImageInjector ──┘
                                                    │
                                                    ↓
                                    prune_historical_image_payloads()
                                                    │
                                                    ↓
                                              LLM invoke
```

### 3.2 消息协议层：多模态 HumanMessage

LangChain `HumanMessage` 的 `content` 支持 `str | list[dict]`。多模态格式：

```python
HumanMessage(content=[
    {"type": "text", "text": "帮我分析这张截图"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo..."}},
])
```

改造点：
- 新增 `core/multimodal/content_part.py`：定义 `TextPart`、`ImageUrlPart`、`ImageBase64Part`，提供统一转换方法。
- 新增 `core/multimodal/message_builder.py`：把用户原始文本 + 零到多个图片对象组装成 `HumanMessage`。
- 所有 `HumanMessage(content=...)` 调用点统一走 `message_builder`，返回的 `content` 可能是字符串或列表。

### 3.3 模型能力感知与路由

新增 `core/multimodal/model_capabilities.py`：

```python
VISION_MODELS = {
    "claude-sonnet-4-20250514": True,
    "claude-opus-4-8-20250514": True,
    "gpt-4o": True,
    "gpt-4o-mini": True,
    "gemini-2.5-flash": True,
    "kimi-k2.7": True,        # Moonshot 多模态主模型
    "deepseek-v4-pro": False,
    "mimo-v2.5-pro": False,
}

def supports_vision(model_name: str) -> bool:
    normalized = (model_name or "").lower()
    return any(v in normalized for v in ["claude", "gpt-4o", "gemini", "glm-5v", "qwen-vl", "kimi-k2"])
```

在 `llm_factory.py` 中：
- 当请求包含图片但当前模型不支持 vision 时，发出明确警告，并建议切换模型（或仅把图片描述作为文本输入）。
- 若 provider 配置中区分了 text / vision endpoint（如 PaiCLI 的 GLM-5.1 vs GLM-5V），按模型名自动选择 `base_url`。
- **kimi-k2.7 通过 Moonshot OpenAI 兼容端点接入**：把 `moonshot` 加入 `OPENAI_COMPAT_PROVIDERS`，`base_url` 指向 `https://api.moonshot.cn/v1`（以实际可用 endpoint 为准）。kimi-k2.7 同时支持文本和 vision，无需单独的 `vision_base_url`。

配置示例（`config.yaml`）：

```yaml
providers:
  zhipu:
    base_url: https://open.bigmodel.cn/api/coding/paas/v4
    vision_base_url: https://open.bigmodel.cn/api/paas/v4
    api_key_env: ZHIPU_API_KEY
    default_model: glm-5.1
    vision_model: glm-5v-turbo

  moonshot:
    base_url: https://api.moonshot.cn/v1
    api_key_env: MOONSHOT_API_KEY
    default_model: kimi-k2.7
    # kimi-k2.7 本身即为多模态模型，text / vision 可共用同一 endpoint
```

### 3.4 用户输入：`@image` / `@clipboard`

新增 `core/multimodal/image_reference.py`：

- 正则匹配 `@image:<...>` / `@image:path` / `@clipboard`。
- 支持相对路径、绝对路径、`file://` 协议、尖括号包裹含空格路径、中文路径。
- 解析为 `ImageRef(path_or_source, source_type)` 列表。

新增 `core/multimodal/image_loader.py`：

- `load_from_path(path)`：读取本地文件，校验路径（限制在项目根或白名单目录，防路径穿越）。
- `load_from_clipboard()`：
  - macOS：调用 `osascript` 读取剪贴板 PNG / TIFF，TIFF 用 `sips` 转 PNG。
  - Linux/Windows：用 `PIL.ImageGrab.grabclipboard()`。
  - headless 环境给出友好提示。

CLI 集成点（`cli/app.py`）：
- 在 `user_input` 进入 `self.conversation` 之前，先调用 `ImageReferenceParser.parse(user_input)`。
- 替换或保留原始文本中的 `@image` 标记（保留便于用户回溯），实际图片由 `MessageBuilder` 附加。
- 终端显示：`[已附加图片: ./shot.png, mimeType=image/png, bytes=123456]`。

### 3.5 MCP 工具图片结果注入

当前 `mcp_client/tools.py` 的 `_format_result()` 对 image 只输出占位文本。升级后：

1. `_format_result()` 仍然返回文本 fallback（保持 tool message 合法）。
2. 额外在 tool result 对象上附加 `image_parts: list[ImageBase64Part]`。
3. `AgentLoop._act()` 执行完工具后，检查所有 tool result：
   - 若存在 image parts，构造一条新的 `HumanMessage`，把图片作为 content 列表追加到对话历史。
   - 顺序：`assistant(tool_calls)` → `tool(text fallback)` → `user(text + image block)`。

代码骨架：

```python
# core/agent_loop.py _act() 工具执行后
text_result = tool.invoke(...)
image_parts = getattr(tool, "_last_image_parts", [])
if image_parts:
    state["messages"].append(ToolMessage(content=text_result, tool_call_id=tc["id"]))
    state["messages"].append(HumanMessage(content=[
        {"type": "text", "text": f"工具 {tool_name} 返回了图片内容，请结合文本结果分析。"},
    ] + image_parts))
else:
    state["messages"].append(ToolMessage(content=text_result, tool_call_id=tc["id"]))
```

### 3.6 图片预处理（ImageProcessor）

新增 `core/multimodal/image_processor.py`，决策树与 PaiCLI 保持一致：

```python
API_IMAGE_MAX_BASE64_SIZE = 5 * 1024 * 1024  # 5MB base64 字符串
MAX_IMAGE_DIMENSION = 2000
JPEG_QUALITIES = [0.85, 0.70, 0.55, 0.40, 0.25]

def process_image(pil_image: Image.Image, mime_hint: str = "image/png") -> ProcessedImage:
    # 1) 无 alpha 且 base64 估算 <= 5MB → 直通 PNG
    # 2) 有 alpha 且 <= 5MB → 白底 flatten → PNG
    # 3) 超过 5MB → 等比缩放到 2000x2000 → 尝试 PNG → 逐级 JPEG 降质
    # 返回 base64 payload + mime_type + width + height
```

关键点：
- 比较的是 **base64 编码后大小**，不是原始字节（膨胀比 4/3）。
- alpha flatten 用 Pillow `Image.new('RGB', size, (255,255,255))` 后 `paste()`。
- 预处理耗时 50ms~200ms，应异步或在输入阶段完成，不阻塞主循环。

### 3.7 历史图片裁剪（ContextCompactor 增强）

新增 `core/multimodal/image_pruner.py`：

```python
def prune_historical_image_payloads(messages: list[BaseMessage], keep_last_n_rounds: int = 1):
    """把旧消息中的 image_url block 替换为文本占位，只保留最近 N 轮图片实体。"""
```

执行位置：
- 在 `AgentLoop._think()` 调用 LLM 之前，先对 `state["messages"]` 执行裁剪。
- 在 `ContextCompactor.compact()` 之前也执行一次，避免摘要时把 base64 算进 token。

占位文本示例：

```python
{"type": "text", "text": "[图片已省略，参见上文描述]"}
```

### 3.8 安全与权限

新增 `permissions/image_guard.py`：

1. **路径围栏**：`@image` 引用的路径必须位于项目根目录或 `multimodal.allowed_image_dirs` 白名单内，禁止 `..` 穿越。
2. **文件类型白名单**：仅允许 `image/png`, `image/jpeg`, `image/webp`, `image/gif`。
3. **大小上限**：单张图片原始文件不超过 `multimodal.max_image_file_size`（默认 20MB），base64 不超过 5MB。
4. **审计日志**：在 `.weavemind/audit/audit-YYYY-MM-DD.jsonl` 记录图片加载事件（路径、大小、来源）。
5. **隐私提示**：首次使用剪贴板/图片输入时提示用户"图片将上传至 LLM provider"。

配置示例：

```yaml
multimodal:
  enabled: true
  max_image_file_size: 20971520       # 20MB
  max_total_image_base64_size: 5242880 # 5MB
  allowed_mime_types: ["image/png", "image/jpeg", "image/webp", "image/gif"]
  allowed_image_dirs: []
  clipboard:
    enabled: true
    timeout_seconds: 8
  image_pruning:
    keep_last_n_rounds: 1
```

---

## 四、实施路线（P0 / P1 / P2）

### Phase 1：核心多模态链路（P0，约 1 周）

1. **消息协议升级**（1 天）
   - 新增 `content_part.py` / `message_builder.py`。
   - 修改 `cli/app.py` 中 `HumanMessage` 构造，支持 content 列表。

2. **模型能力感知**（1 天）
   - 新增 `model_capabilities.py`。
   - 修改 `llm_factory.py`：vision endpoint 路由、不支持 vision 时的降级提示。

3. **@image 引用解析与加载**（2 天）
   - 新增 `image_reference.py` / `image_loader.py` / `image_guard.py`。
   - CLI 输入预处理集成；终端显示附加提示。

4. **图片预处理**（2 天）
   - 新增 `image_processor.py`（Pillow）。
   - 与 `image_loader` 串联，输出 base64 + mime。

5. **MCP 截图注入**（2 天）
   - 修改 `mcp_client/tools.py`：保留 image base64，附加到 tool result。
   - 修改 `AgentLoop._act()`：image parts → user message。

### Phase 2：体验与成本优化（P1，约 3–4 天）

6. **历史图片裁剪**（1 天）
   - 新增 `image_pruner.py`，接入 `AgentLoop._think()` 和 `ContextCompactor`。

7. **剪贴板抓图**（1 天）
   - macOS AppleScript 实现；Linux/Windows Pillow fallback。

8. **配置化与审计**（1 天）
   - `config.yaml.example` 增加 `multimodal` 段。
   - 图片加载事件写入审计日志。

9. **单测补充**（1 天）
   - `tests/test_multimodal.py`：解析器、预处理、裁剪、MCP image 注入。

### Phase 3：扩展能力（P2，后续）

10. **微信图片输入**：iLink 消息中的图片下载后走同一套 `ImageProcessor`。
11. **PDF/Word 内图片**：与 Knowledge RAG 的图片 OCR / caption 链路打通。
12. **视频/音频**：暂不实现，保持方案可扩展（ContentPart 预留类型字段）。

---

## 五、关键文件修改清单

| 文件 | 修改内容 |
|------|---------|
| `core/multimodal/content_part.py` | 新增 ContentPart 数据模型 |
| `core/multimodal/message_builder.py` | 新增多模态消息构造器 |
| `core/multimodal/model_capabilities.py` | 新增模型 vision 能力表 |
| `core/multimodal/image_reference.py` | 新增 `@image` / `@clipboard` 解析 |
| `core/multimodal/image_loader.py` | 新增本地文件/剪贴板加载 |
| `core/multimodal/image_processor.py` | 新增图片预处理管线 |
| `core/multimodal/image_pruner.py` | 新增历史图片裁剪 |
| `core/llm_factory.py` | vision endpoint 路由、能力检测 |
| `core/agent_loop.py` | 接入图片解析、MCP image 注入、图片裁剪 |
| `core/compaction.py` | token 计数兼容 content 列表；压缩前裁剪图片 |
| `cli/app.py` | 输入预处理、终端提示 |
| `mcp_client/tools.py` | image content 提取与传递 |
| `permissions/image_guard.py` | 新增图片安全校验 |
| `config.yaml.example` | 新增 `multimodal` 配置段 |
| `tests/test_multimodal.py` | 新增测试 |

---

## 六、风险与注意事项

1. **LangChain 序列化兼容性**
   - `HumanMessage(content=list)` 在部分 provider 的序列化中可能不一致，需要在 `llm_factory.py` 的 `MiMoChatOpenAI._get_request_payload` 等自定义序列化点做兼容测试。

2. **Token 计数失真**
   - `tiktoken` 无法准确计算 vision token。压缩阈值仍需以文本 token 为主，图片通过独立大小上限控制。

3. **MCP image 注入顺序**
   - 必须保证 `tool message` 在前、`user image message` 在后，且 tool_call_id 配对正确，否则 LangGraph 会报错。

4. **路径穿越风险**
   - `@image:../../../etc/passwd` 必须被 `ImageGuard` 拦截，不能依赖 LLM 自律。

5. **剪贴板隐私**
   - 剪贴板可能包含敏感信息，首次使用需提示，且只读取图片类型数据。

6. **模型能力探测不完全**
   - `supports_vision()` 用模型名启发式判断，新增模型时需维护列表；后续可改为调用 `model_info` 或 LLM self-report。

---

## 七、面试/简历亮点映射

| 升级点 | 可讲故事 |
|--------|---------|
| ContentPart 协议升级 | LangChain 多模态消息格式、OpenAI / Anthropic 内容块协议兼容 |
| `@image` / `@clipboard` | 终端原生图片输入协议设计、正则解析、跨平台剪贴板 |
| MCP 截图注入 | tool message 不支持 content array 的协议限制与绕过方案 |
| 图片预处理管线 | 性能与质量的 tradeoff、alpha flatten、base64 膨胀估算 |
| 历史图片裁剪 | 上下文窗口成本控制、长对话多模态推理优化 |
| ImageGuard | 最小权限原则、路径围栏、文件类型/大小安全策略 |

---

## 八、下一步行动

建议先开分支做 **Phase 1 的消息协议升级 + @image 解析 + MCP 截图注入** 的最小闭环，跑通"用户输入 `@image:./screenshot.png` → LLM 描述图片内容"和"MCP 截图 → LLM 分析页面"两条主链路，再补充剪贴板、裁剪、审计等 P1 能力。
