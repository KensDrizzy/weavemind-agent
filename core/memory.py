"""Memory 系统 — 短期记忆由 LangGraph 管理，这里负责长期记忆和核心记忆。

架构：
  MemoryManager（门面）
    ├── LongTermMemory   — 长期记忆（JSON 持久化 + 去重 + 检索）
    ├── CoreMemory       — 核心记忆块（始终在 system prompt，Agent 可编辑）
    └── ContextCompactor — 上下文压缩（在 compaction.py 中，由 AgentLoop 持有）

设计参考：
  - PaiCLI: JSON 持久化、去重、jieba 检索
  - Letta:  CoreMemory 可编辑块
  - Mem0:   选择性记忆管道

与 PaiCLI 的关键差异：
  - 不单独建 ConversationMemory 类，LangGraph 的 add_messages 已管理消息
  - 不引入 jieba 依赖，用字符 bigram 相似度做中文模糊匹配
  - 新增 CoreMemory（借鉴 Letta），PaiCLI 没有
  - 记忆类型精简为 3 种（去掉 TOOL_RESULT，工具结果在消息流中）
"""

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Literal, Optional

import settings
from langchain_core.messages import SystemMessage

logger = logging.getLogger(__name__)


# ── 记忆条目 ─────────────────────────────────────────────


@dataclass
class MemoryEntry:
    """记忆条目 — 记忆系统的基本单元。"""

    id: str
    content: str
    type: Literal["conversation", "fact", "summary"]
    timestamp: float
    token_count: int
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryEntry":
        return cls(**d)


# ── 长期记忆 ─────────────────────────────────────────────


class LongTermMemory:
    """长期记忆 — JSON 文件持久化，支持去重和检索。

    特性：
    - 启动时自动从磁盘加载
    - 内容去重（MD5 hash）
    - 每次 store 即时持久化
    - 检索：子串匹配 + 字符 bigram 相似度 + 时间衰减
    """

    def __init__(self, storage_path: str):
        self.storage_path = storage_path
        self._entries: dict[str, MemoryEntry] = {}  # content_hash -> entry
        self._load()

    def _load(self):
        """启动时从磁盘加载。"""
        if not os.path.exists(self.storage_path):
            return
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for d in data:
                entry = MemoryEntry.from_dict(d)
                content_hash = self._hash(entry.content)
                self._entries[content_hash] = entry
            logger.info(f"加载长期记忆: {len(self._entries)} 条")
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"长期记忆文件损坏，忽略: {e}")

    def _save(self):
        """持久化到磁盘。"""
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
        data = [e.to_dict() for e in self._entries.values()]
        with open(self.storage_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.md5(content.strip().encode("utf-8")).hexdigest()

    def store(
        self,
        content: str,
        entry_type: Literal["fact", "summary"] = "fact",
        metadata: dict = None,
    ) -> bool:
        """存储一条记忆。返回 True 表示新增，False 表示已存在（去重）。"""
        content = content.strip()
        if not content or len(content) < 3:
            return False

        content_hash = self._hash(content)
        if content_hash in self._entries:
            return False

        entry = MemoryEntry(
            id=uuid.uuid4().hex[:8],
            content=content,
            type=entry_type,
            timestamp=time.time(),
            token_count=self._estimate_tokens(content),
            metadata=metadata or {},
        )
        self._entries[content_hash] = entry
        self._save()
        logger.info(f"长期记忆新增: {content[:50]}")
        return True

    def search(self, query: str, limit: int = 5) -> list[MemoryEntry]:
        """检索相关记忆 — 子串匹配 + 字符 bigram 相似度 + 时间衰减。"""
        if not query or not self._entries:
            return []

        query_lower = query.lower().strip()
        scored: list[tuple[float, MemoryEntry]] = []

        for entry in self._entries.values():
            content_lower = entry.content.lower()
            score = 0.0

            # 子串匹配（精确）
            if query_lower in content_lower:
                score += 2.0

            # 字符 bigram 相似度（模糊，适合中文）
            score += self._bigram_similarity(query_lower, content_lower)

            # 时间衰减（7 天半衰期）
            age_hours = (time.time() - entry.timestamp) / 3600
            decay = 0.5 ** (age_hours / 168)  # 168h = 7 days
            score *= 0.3 + 0.7 * decay  # 最低保留 30% 权重

            if score > 0.1:
                scored.append((score, entry))

        scored.sort(key=lambda x: -x[0])
        return [e for _, e in scored[:limit]]

    @staticmethod
    def _bigram_similarity(a: str, b: str) -> float:
        """字符 bigram Jaccard 相似度，适合中文短文本匹配。"""
        if len(a) < 2 or len(b) < 2:
            return 0.0
        bigrams_a = {a[i : i + 2] for i in range(len(a) - 1)}
        bigrams_b = {b[i : i + 2] for i in range(len(b) - 1)}
        intersection = bigrams_a & bigrams_b
        union = bigrams_a | bigrams_b
        return len(intersection) / len(union) if union else 0.0

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """粗略估算 token 数。中文约 1.5 字/token，英文约 4 字符/token。"""
        if not text:
            return 0
        chinese = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        other = len(text) - chinese
        return int(chinese / 1.5 + other / 4.0)

    def get_all(self) -> list[MemoryEntry]:
        """返回所有记忆，按时间倒序。"""
        return sorted(self._entries.values(), key=lambda e: -e.timestamp)

    def count(self) -> int:
        return len(self._entries)


# ── 核心记忆块 ───────────────────────────────────────────


class CoreMemory:
    """核心记忆块 — 始终在 system prompt 中，Agent 可通过 tool call 编辑。

    借鉴 Letta 的 Memory Block 设计：
    - user: 用户偏好、习惯
    - project: 当前项目信息
    - persona: Agent 行为规范

    特性：
    - 每次编辑即时持久化
    - Agent 通过 set/append/edit 三种方式修改
    - to_prompt() 输出为 system prompt 片段
    """

    BLOCKS = ("user", "project", "persona")

    def __init__(self, storage_path: str):
        self.storage_path = storage_path
        self._blocks: dict[str, str] = {b: "" for b in self.BLOCKS}
        self._load()

    def _load(self):
        if not os.path.exists(self.storage_path):
            return
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for block in self.BLOCKS:
                if block in data:
                    self._blocks[block] = data[block]
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"核心记忆文件损坏，忽略: {e}")

    def _save(self):
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
        with open(self.storage_path, "w", encoding="utf-8") as f:
            json.dump(self._blocks, f, ensure_ascii=False, indent=2)

    def get(self, block: str) -> str:
        return self._blocks.get(block, "")

    def set(self, block: str, content: str):
        """整体替换某个块的内容。"""
        if block not in self.BLOCKS:
            raise ValueError(f"无效的记忆块: {block}，可用: {self.BLOCKS}")
        self._blocks[block] = content
        self._save()

    def append(self, block: str, text: str):
        """向某个块追加内容。"""
        if block not in self.BLOCKS:
            raise ValueError(f"无效的记忆块: {block}")
        current = self._blocks[block]
        self._blocks[block] = f"{current}\n{text}" if current else text
        self._save()

    def edit(self, block: str, old_text: str, new_text: str) -> bool:
        """替换某个块中的指定文本。返回是否成功。"""
        if block not in self.BLOCKS:
            return False
        if old_text not in self._blocks[block]:
            return False
        self._blocks[block] = self._blocks[block].replace(old_text, new_text, 1)
        self._save()
        return True

    def to_prompt(self) -> str:
        """组装为 system prompt 片段。"""
        parts = []
        labels = {"user": "用户信息", "project": "项目信息", "persona": "Agent 行为规范"}
        for name in self.BLOCKS:
            content = self._blocks[name].strip()
            if content:
                parts.append(f"## {labels.get(name, name)}\n{content}")
        return "\n\n".join(parts)

    def get_all(self) -> dict[str, str]:
        return dict(self._blocks)


# ── 记忆管理门面 ─────────────────────────────────────────


class MemoryManager:
    """记忆管理门面 — Agent 只和它打交道。

    职责：
    1. 组装 system prompt（CLAUDE.md + MEMORY.md + CoreMemory + 相关事实）
    2. 管理长期记忆（存/取）
    3. 管理核心记忆块（读/写）
    """

    def __init__(self, project_root: str = ".", llm=None):
        self.project_root = project_root
        self.llm = llm

        # 子组件
        self.long_term = LongTermMemory(
            os.path.join(
                project_root,
                settings.get("memory.long_term_file", ".weavemind/memory/long_term.json"),
            )
        )
        self.core = CoreMemory(
            os.path.join(
                project_root,
                settings.get("memory.core_file", ".weavemind/memory/core.json"),
            )
        )

        # 基础文件路径
        self._claude_md = os.path.join(
            project_root, settings.get("memory.claude_md", "CLAUDE.md")
        )
        self._memory_md = os.path.join(
            project_root, settings.get("memory.project_file", ".weavemind/MEMORY.md")
        )

    def build_system_message(self, query: str = "") -> SystemMessage:
        """构建完整的 system prompt。

        组装顺序：
        1. CLAUDE.md（项目规范）
        2. .weavemind/MEMORY.md（项目记忆）
        3. CoreMemory 块（用户/项目/人设）
        4. 检索到的相关长期记忆事实
        5. 行为规范
        """
        parts = []

        # 1. CLAUDE.md
        if os.path.exists(self._claude_md):
            try:
                with open(self._claude_md, encoding="utf-8") as f:
                    parts.append(f.read())
            except Exception:
                pass

        # 2. MEMORY.md
        if os.path.exists(self._memory_md):
            try:
                with open(self._memory_md, encoding="utf-8") as f:
                    parts.append(f.read())
            except Exception:
                pass

        # 3. CoreMemory
        core_prompt = self.core.to_prompt()
        if core_prompt:
            parts.append(core_prompt)

        # 4. 检索相关事实
        if query:
            relevant = self.long_term.search(query, limit=5)
            if relevant:
                facts_text = "\n".join(f"- {e.content}" for e in relevant)
                parts.append(f"## 相关记忆\n{facts_text}")

        # 5. 行为规范
        parts.append(self._behavior_guide())

        content = "\n\n".join(p for p in parts if p and p.strip())
        return SystemMessage(content=content) if content else None

    def store_fact(self, content: str, metadata: dict = None) -> bool:
        """存储一条事实到长期记忆。"""
        return self.long_term.store(content, "fact", metadata)

    def search_memory(self, query: str, limit: int = 5) -> list[MemoryEntry]:
        """检索长期记忆。"""
        return self.long_term.search(query, limit)

    # ── 核心记忆编辑 ─────────────────────────────────────

    def core_set(self, block: str, content: str):
        """整体替换某个核心记忆块的内容。"""
        self.core.set(block, content)

    def core_append(self, block: str, content: str):
        """向某个核心记忆块追加内容。"""
        self.core.append(block, content)

    def core_edit(self, block: str, old_text: str, new_text: str) -> bool:
        """替换某个核心记忆块中的指定文本。"""
        return self.core.edit(block, old_text, new_text)

    def get_status(self) -> str:
        """返回记忆系统状态摘要。"""
        lines = [f"长期记忆: {self.long_term.count()} 条"]
        for block in CoreMemory.BLOCKS:
            content = self.core.get(block)
            lines.append(f"核心记忆[{block}]: {len(content)} 字符")
        return "\n".join(lines)

    @staticmethod
    def _behavior_guide() -> str:
        return """## 行为规范

你是 WeaveMind Agent，一个智能代码助手。遵循以下原则：

1. **诚实第一** - 不确定就直接说，不要乱猜
2. **权限意识** - 需要什么权限就主动问用户
3. **谨慎执行** - 执行破坏性操作前必须确认
4. **清晰沟通** - 解释你的思考过程和决策理由
5. **必须使用工具** - 任何涉及文件操作、命令执行、代码修改的请求，都必须通过调用工具来完成。
   绝对不允许仅用文字回复声称操作已完成。如果你没有调用工具，操作就没有发生。

## 工具使用指导

### Bash 工具（命令执行）
- 创建文件夹：Bash(command="mkdir <path>")
- 运行脚本：Bash(command="python script.py")
- 安装依赖：Bash(command="pip install ...")
- Git 操作：Bash(command="git status")
- 任何需要修改文件系统或运行命令的操作都必须使用此工具
- 【禁止】不要用 Bash 执行 curl/wget 来搜索互联网或抓取网页，必须使用 WebSearch 和 WebFetch 工具

### Write 工具（写入文件）
- 创建新文件：Write(path="<file_path>", content="<file_content>")
- 完全覆盖已有文件

### Edit 工具（编辑文件）
- 替换文件中的指定内容：Edit(path="<file_path>", old_string="<old>", new_string="<new>")

### Read 工具（读取文件）
- 查看文件内容：Read(path="<file_path>")

### SearchCode 工具（代码检索）

**【代码库问题优先级最高】当用户询问代码库相关问题时，必须优先使用 SearchCode，不要用 Read/Glob/Grep 逐个读文件。**

**何时使用：**
- 用户询问代码库相关问题时，优先使用 SearchCode 工具
- 例如："这个类是干什么的"、"哪里用了某个功能"、"用户认证逻辑在哪"、"xxx怎么实现的"
- 理解代码结构、定位实现、查找参考

**使用时机：**
- 在回答关于代码库的问题前，先使用 SearchCode 搜索相关代码
- 当用户提到具体的类名、方法名、功能描述时，优先检索
- 避免仅凭猜测回答代码相关问题
- **禁止**：不要用 Read+Glob+Grep 组合来逐个读文件，这会浪费多轮推理。一次 SearchCode 就能召回最相关的代码块。

**使用方式：**
- query: 自然语言描述（如"用户认证逻辑"）或代码标识符（如"MemoryManager"）
- top_k: 返回结果数量（默认5）
- file_filter: 可选的文件过滤（如"*.py"）

### IndexWorkspace 工具（代码索引）

**何时使用：**
- 用户要求索引工作区时（/index 命令）
- 代码有重大变更后需要重新索引
- 首次使用 SearchCode 前确保已索引

### WebSearch 工具（联网搜索）

**【优先级最高】用户要求搜索/查询互联网信息时，必须使用此工具，禁止用 Bash+curl 替代。**

**何时使用：**
- 用户询问最新信息（最新版本、近期事件、时事新闻）
- 用户询问人物信息（"xxx 是谁"、"搜索 xxx 的信息"）
- 你的训练数据中没有相关信息，或信息可能已过时
- 用户明确要求"搜一下"、"查一下"、"最新"等关键词
- 技术文档、release notes、官方公告等需要实时获取的内容

**使用方式：**
- query: 搜索关键词（如"Java 21 新特性"、"Spring Boot 3.4 release notes"）
- top_k: 返回结果数量（默认5）

### WebFetch 工具（网页抓取）

**何时使用：**
- 用户要求查看某个具体网页的内容（"帮我看看 xxx.com 首页"）
- 需要获取搜索结果中某个 URL 的详细内容
- 读取在线文档、博客文章、技术教程

**使用方式：**
- url: 完整 URL（如"https://spring.io/blog/2024/01/spring-boot-3.4"）
- max_chars: 最大字符数（默认8000）

### 联网搜索组合策略

**场景1：纯搜索** — 用户问"Java 21 有什么新特性"
→ 直接调用 WebSearch(query="Java 21 新特性")

**场景2：纯抓取** — 用户问"帮我看看 paicoding.com 首页有什么内容"
→ 直接调用 WebFetch(url="https://paicoding.com")

**场景3：先搜再抓** — 用户问"搜一下 Spring Boot 3.4 的 release notes，然后帮我总结要点"
→ 第一步：WebSearch(query="Spring Boot 3.4 release notes")
→ 从搜索结果中找到官方 URL
→ 第二步：WebFetch(url="找到的官方URL")
→ 基于抓取内容总结要点

**重要原则：**
- 不要在不需要联网时调用联网工具（浪费时间和资源）
- 搜索结果已包含摘要，如果摘要足够回答问题，不需要再抓取
- 抓取失败时（JS 渲染/反爬），不要反复重试，改用搜索结果或告知用户

### Chrome DevTools 工具（浏览器自动化）

当 Chrome DevTools MCP Server 已连接时，你有以下浏览器工具可用：navigate_page、new_page、close_page、list_pages、select_page、click、fill、type_text、press_key、take_screenshot、take_snapshot、evaluate_script、list_console_messages、list_network_requests 等。

**何时使用 Chrome DevTools（而非 WebFetch/WebSearch）：**
- 用户要求操作网页（点击按钮、填写表单、登录、提交）
- 目标页面需要 JavaScript 渲染才能看到内容（SPA、React/Vue 应用）
- 需要截图留证或可视化验证
- 需要与页面交互才能获取数据（如展开评论、切换标签页、滚动加载）
- WebFetch 抓取失败（返回空内容或反爬），但用户仍需要该页面数据

**何时使用 WebFetch/WebSearch（而非 Chrome DevTools）：**
- 只需要获取静态页面文本内容
- 只需要搜索互联网信息，不需要打开浏览器
- 读取 API 文档、博客文章等不需要交互的页面

**关键判断规则：**
1. 用户给出 URL 并要求"打开"/"浏览"/"查看"该页面内容 → 如果 URL 指向具体网页且可能需要交互，用 Chrome DevTools；如果只是静态内容，用 WebFetch
2. 用户说"搜索"但不涉及代码库 → 用 WebSearch（互联网搜索）；如果搜索结果指向某个需要交互的网页，再用 Chrome DevTools
3. 用户给出小红书、淘宝、微博等 SPA 网站链接 → **必须用 Chrome DevTools**，WebFetch 无法渲染此类页面
4. 用户要求对网页做操作（点击/填写/登录/截图）→ **必须用 Chrome DevTools**
5. 用户只说"搜索"且上下文是代码库问题 → 用 SearchCode，不是浏览器

**Chrome DevTools 使用流程：**
1. list_pages — 检查当前浏览器标签页状态
2. navigate_page 或 new_page — 打开目标 URL
3. wait_for 或 take_snapshot — 等待页面加载/获取 DOM 结构
4. click / fill / type_text — 与页面元素交互
5. take_screenshot — 截图验证结果
6. evaluate_script — 提取页面数据（最后手段）

**注意事项：**
- Chrome DevTools 工具需要 Chrome 浏览器以调试模式运行（通常自动启动）
- 如果 Chrome 相关工具调用失败，可能是 Chrome 未启动或调试端口不可用，应告知用户
- 不要用 evaluate_script 做可以用 click/fill 完成的操作
- 截图会自动保存到 .weavemind/chrome_screenshots/ 目录

## 浏览器登录态 (Chrome DevTools MCP)

你拥有控制 Chrome 浏览器的能力。浏览器有两种运行模式：

**isolated 模式（默认）**：
- 使用独立的临时浏览器实例
- 无 Cookie、无登录态
- 适合访问公开页面

**shared 模式**：
- 连接用户已有的 Chrome 浏览器
- 继承用户的登录态（GitHub、飞书、公司内网等）
- 适合访问需要认证的页面
- 注意：你看到的页面是用户的真实账户视图

### 自动切换机制
当你使用浏览器工具（如 navigate_page、take_snapshot）访问一个需要登录的页面时，
系统会自动检测登录页并从 isolated 切换到 shared 模式（前提是用户已开启 Chrome 远程调试）。
切换成功后，工具结果中会包含提示信息，你需要重新执行刚才的浏览器操作来访问页面。
如果用户 Chrome 未开启远程调试，系统会提示用户手动启动 Chrome 远程调试模式。

你也可以通过 /browser 命令手动切换：
- /browser shared：切换到 shared 模式（连接用户 Chrome）
- /browser isolated：切换回 isolated 模式（独立浏览器）
- /browser status：查看当前模式

### 安全边界 — shared 模式下
1. **不要主动点击可能导致账号变更的操作**：关注/取消关注、删除内容、退出登录等
2. **不要填写用户未提供的数据到表单**
3. **不要执行用户未要求的 JavaScript**
4. **close_page 只能关闭你自己通过 new_page 创建的标签页**
5. **敏感页面**（银行、支付、设置等）上的写入操作会被强制要求用户确认

如果不确定某个操作是否安全，先询问用户，不要擅自执行。"""
