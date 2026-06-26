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
import math
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
    """记忆条目 — 记忆系统的基本单元。

    访问统计字段（access_count / last_access）对标 Letta archival memory：
    高频被检索的记忆获得排名奖励，避免"重要但旧"被时间衰减压沉。
    importance 是可选乘子，留给上层（如 CoreMemory 提升、用户标注 pin）使用。
    """

    id: str
    content: str
    type: Literal["conversation", "fact", "summary"]
    timestamp: float
    token_count: int
    metadata: dict = field(default_factory=dict)
    access_count: int = 0
    last_access: float = 0.0
    importance: float = 1.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryEntry":
        # 老格式向后兼容：磁盘上可能没有新字段，用默认值补齐
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)


# ── 长期记忆 ─────────────────────────────────────────────


class LongTermMemory:
    """长期记忆 — JSON 文件持久化，支持去重和检索。

    特性：
    - 启动时自动从磁盘加载
    - 内容去重（MD5 hash）
    - 相似记忆更新（相似度 > 0.85 时替换旧内容）
    - 每次 store 即时持久化
    - 检索：子串匹配 + 字符 bigram 相似度 + 时间衰减
    """

    UPDATE_SIMILARITY_THRESHOLD = 0.85

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
        """存储一条记忆。

        返回 True 表示新增或更新，False 表示完全重复或内容无效。
        写入流程：
        1. 完全相同内容按 MD5 去重；
        2. 与已有记忆相似度 > 0.85 时，视为长期记忆 update，原地替换旧内容；
        3. 否则新增记忆。
        """
        content = content.strip()
        if not content or len(content) < 3:
            return False

        content_hash = self._hash(content)
        if content_hash in self._entries:
            return False

        similar = self._find_similar_entry(content, self.UPDATE_SIMILARITY_THRESHOLD)
        if similar:
            old_hash, entry, score = similar
            old_content = entry.content
            now = time.time()
            merged_metadata = dict(entry.metadata or {})
            if metadata:
                merged_metadata.update(metadata)
            merged_metadata.update({
                "updated_at": now,
                "updated_from": old_content,
                "update_similarity": round(score, 4),
            })

            entry.content = content
            entry.type = entry_type
            entry.timestamp = now
            entry.token_count = self._estimate_tokens(content)
            entry.metadata = merged_metadata

            del self._entries[old_hash]
            self._entries[content_hash] = entry
            self._save()
            logger.info(
                "长期记忆更新: similarity=%.4f, old=%s, new=%s",
                score,
                old_content[:50],
                content[:50],
            )
            return True

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

    def _find_similar_entry(
        self,
        content: str,
        threshold: float,
    ) -> Optional[tuple[str, MemoryEntry, float]]:
        """查找超过阈值的最相似记忆，用于 update 判断。"""
        if not self._entries:
            return None

        content_lower = content.lower().strip()
        best: tuple[str, MemoryEntry, float] | None = None
        for content_hash, entry in self._entries.items():
            score = self._bigram_similarity(content_lower, entry.content.lower().strip())
            if score > threshold and (best is None or score > best[2]):
                best = (content_hash, entry, score)
        return best

    def search(self, query: str, limit: int = 5) -> list[MemoryEntry]:
        """检索相关记忆 — 子串匹配 + 字符 bigram 相似度 + 时间衰减 + 使用频次。

        参考 Letta archival memory：召回时回写访问统计，
        让"被频繁使用的记忆"获得排名奖励，命中后立即持久化。
        """
        if not query or not self._entries:
            return []

        query_lower = query.lower().strip()
        scored: list[tuple[float, MemoryEntry]] = []
        now = time.time()

        for entry in self._entries.values():
            content_lower = entry.content.lower()
            score = 0.0

            # 子串匹配（精确）
            if query_lower in content_lower:
                score += 2.0

            # 字符 bigram 相似度（模糊，适合中文）
            score += self._bigram_similarity(query_lower, content_lower)

            # 时间衰减（7 天半衰期）
            age_hours = (now - entry.timestamp) / 3600
            decay = 0.5 ** (age_hours / 168)  # 168h = 7 days
            score *= 0.3 + 0.7 * decay  # 最低保留 30% 权重

            # 使用频次奖励（对数饱和，避免被高频条目垄断）
            if entry.access_count > 0:
                score += 0.15 * math.log1p(entry.access_count)

            # 最近使用奖励（1 天半衰期，最多 +0.3）
            if entry.last_access > 0:
                recent_hours = (now - entry.last_access) / 3600
                score += 0.3 * (0.5 ** (recent_hours / 24))

            # 重要度乘子（默认 1.0；用户/系统标记 pin 时上调）
            score *= entry.importance

            if score > 0.1:
                scored.append((score, entry))

        scored.sort(key=lambda x: -x[0])
        top = [e for _, e in scored[:limit]]

        # 命中即写：回写访问统计，让"用过"的记忆下次更易召回
        if top:
            for entry in top:
                entry.access_count += 1
                entry.last_access = now
            try:
                self._save()
            except Exception as e:
                # 写盘失败不影响检索结果返回
                logger.debug(f"长期记忆访问统计回写失败（忽略）: {e}")

        return top

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
        1. 动态记忆上下文（CLAUDE.md + MEMORY.md + CoreMemory + 相关事实）
        2. base.md（Identity + Language + Tools + Tool Policy + Browser + Safety）
        3. personality.md
        4. mode 提示词
        5. Skill 索引
        6. context-management.md
        7. handoff.md
        """
        from core.prompt_assembler import PromptAssembler, PromptMode

        # 组装动态记忆上下文
        memory_parts = []

        # CLAUDE.md
        if os.path.exists(self._claude_md):
            try:
                with open(self._claude_md, encoding="utf-8") as f:
                    memory_parts.append(f.read())
            except Exception:
                pass

        # MEMORY.md
        if os.path.exists(self._memory_md):
            try:
                with open(self._memory_md, encoding="utf-8") as f:
                    memory_parts.append(f.read())
            except Exception:
                pass

        # CoreMemory
        core_prompt = self.core.to_prompt()
        if core_prompt:
            memory_parts.append(core_prompt)

        # 检索相关事实
        if query:
            relevant = self.long_term.search(query, limit=5)
            if relevant:
                facts_text = "\n".join(f"- {e.content}" for e in relevant)
                memory_parts.append(f"## 相关记忆\n{facts_text}")

        memory_context = "\n\n".join(p for p in memory_parts if p and p.strip()) or None

        # Skill 索引
        skill_index = self._skill_index if hasattr(self, '_skill_index') and self._skill_index else None

        # 使用 PromptAssembler 组装
        if not hasattr(self, '_prompt_assembler'):
            self._prompt_assembler = PromptAssembler()

        content = self._prompt_assembler.assemble(
            mode=PromptMode.AGENT,
            memory_context=memory_context,
            skill_index=skill_index,
        )
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
        """已废弃 — 提示词已迁移到 prompts/ 目录下的 .md 文件。"""
        return ""
