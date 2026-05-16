# Agent Skill 系统深度分析与 WeaveMind 升级方案

## 一、背景与目标

本文档综合分析了 PaiCLI 的 Skill 系统实现、业界主流 Agent 框架（Claude Code、Hermes Agent、OpenAI Codex）的技能机制，为 WeaveMindAgent 的 Skill 系统升级提供完整的技术方案。

**Skill 的本质价值**：
- MCP 提供的是**能力**（搜索、抓取、操作浏览器）
- Skill 提供的是**决策**（什么时候搜索、什么时候抓网页、用什么工具）
- Skill 让 Agent 从"有一堆工具"变成"有经验的 Agent"

---

## 二、PaiCLI Skill 系统架构解析

### 2.1 核心设计哲学

#### 三层加载架构（Layered Loading）

```
┌─────────────────────────────────────────────────────────┐
│  第三层: Project Skill (优先级最高)                      │
│  路径: <project>/.paicli/skills/<name>/SKILL.md         │
│  用途: 项目特定的决策知识                                │
├─────────────────────────────────────────────────────────┤
│  第二层: User Skill                                      │
│  路径: ~/.paicli/skills/<name>/SKILL.md                 │
│  用途: 用户全局共享的 Skill                              │
├─────────────────────────────────────────────────────────┤
│  第一层: Builtin Skill (优先级最低)                      │
│  路径: jar 内置 → 解压到 ~/.paicli/skills-cache/        │
│  用途: 随版本发布的基础 Skill                            │
└─────────────────────────────────────────────────────────┘
```

**覆盖规则**：同名 Skill 后加载的覆盖先加载的（builtin → user → project）

#### 渐进式披露（Progressive Disclosure）

```
System Prompt（轻量索引）
    ↓
LLM 判断需要某个 Skill → 调用 load_skill(name)
    ↓
Skill body 通过 user message 注入（而非 system prompt）
    ↓
LLM 按 Skill 指引执行
```

**关键设计决策**：
1. **启动时不加载完整 body** - 避免 10 个 Skill 吃掉几万 token
2. **body 走 user message 而非 system prompt** - 保证 prompt cache 不被破坏，降低 API 成本约 15%
3. **LLM 自主决定加载时机** - 通过语义理解匹配场景，比关键词匹配更准确

---

### 2.2 核心数据结构

#### Skill Record（Java）

```java
public record Skill(
    String name,           // 唯一标识
    String description,    // 一句话摘要（给 LLM 看）
    String version,        // 版本号
    String author,         // 作者
    List<String> tags,     // 标签
    Source source,         // BUILTIN/USER/PROJECT
    String body,           // 决策手册正文（按需加载）
    Path skillMdPath,      // SKILL.md 文件路径
    Path referencesDir     // references/ 目录
) {}
```

#### SKILL.md 结构

```yaml
---
name: web-access
description: |
  所有联网操作必须通过此 skill 处理，
  包括搜索、网页抓取、登录后操作
version: "1.0.0"
author: PaiCLI
tags: [web, browser, search]
---

# web-access Skill

## 浏览哲学
明确目标 → 选择起点 → 过程校验 → 完成判断

## 工具选择表
| 场景 | 工具 | 说明 |
|------|------|------|
| 搜索 | web_search | 关键词搜索 |
| 已知 URL | web_fetch | 直接抓取（成本最低） |
| SPA 动态站点 | Chrome DevTools MCP | navigate_page + take_snapshot |
| 兜底 | Jina Reader | curl https://r.jina.ai/<url> |
```

#### SkillContextBuffer 生命周期管理

```java
public final class SkillContextBuffer {
    private static final int MAX_SKILLS = 3;  // 最多同时保留 3 个
    
    // 关键约束：
    // 1. drain() 一次性消费 - 防止跨轮重复注入
    // 2. 同名替换 - 新 body 替换旧的，不重复累积
    // 3. /clear 重置 - 调试时清除 buffer
    // 4. 角色隔离 - Planner/Worker/Reviewer 各自独立实例
}
```

---

### 2.3 核心类职责

| 类名 | 职责 |
|------|------|
| `SkillRegistry` | 管理三层目录扫描、Skill 合并、启用状态过滤 |
| `SkillFrontmatterParser` | 手写 YAML 解析器，支持 95% 实际用法，失败不阻塞 |
| `SkillBuiltinExtractor` | 从 jar 解压内置 Skill，按版本号判断是否需要更新 |
| `SkillContextBuffer` | Skill body 注入缓冲区，管理生命周期 |
| `SkillStateStore` | 持久化禁用列表（~/.paicli/skills.json） |
| `SkillIndexFormatter` | 格式化 Skill 索引给 LLM 看 |

---

### 2.4 极简 YAML 解析器特点

```java
// SkillFrontmatterParser 支持的语法
- 单行 key: value
- 多行 key: |\n  line1\n  line2（以首行缩进推断）
- 行内数组 key: [a, b, c]

// 不支持的语法（warning 但不阻塞）
- 嵌套对象 key: { nested: ... }
- YAML 锚点 / 别名
- 复杂类型标记 !!str
```

**设计取舍**：不引入 SnakeYAML 依赖，减少 jar 体积；覆盖 95% 实际使用场景。

---

### 2.5 LLM 交互设计

#### System Prompt 中的 Skill 索引

```markdown
## 可用 Skills（按需调用 load_skill 加载完整指引）

- **web-access**: 所有联网操作必须通过此 skill 处理，包括搜索、网页抓取、登录后操作...

判断准则：当任务描述匹配某个 skill 的触发场景时，调用 load_skill(name) 加载完整指引，
然后按指引执行。已加载的 skill 会在下一轮以 `## 已加载 Skill` 段落出现在你的 user message 中。
不要重复加载同一 skill；同一会话内一次足够。
```

#### User Message 注入格式

```markdown
## 已加载 Skill：web-access
<SKILL.md body 完整内容>

---
用户输入：<用户的原始消息>
```

---

## 三、主流 Agent Skill 系统对比

### 3.1 Claude Code / Codex / OpenAI

**特点**：
- 首家引入 Skill 概念（2025年底）
- SKILL.md 已成为 Linux Foundation Agentic AI Foundation 的开放标准
- 跨工具兼容：给 Claude Code 写的 Skill，Codex 也能用

**结构**：
```yaml
---
name: my-skill
description: Use when <trigger>. <behavior>.
version: 1.0.0
author: Your Name
---
# Skill body
```

---

### 3.2 Hermes Agent

**特点**：
- 136+ 内置 Skills
- 用户级 Skills：`~/.hermes/skills/<category>/<name>/SKILL.md`
- CLI 管理：`hermes skills list/install/uninstall`
- Skill 热加载，无需重启

**严格验证**：
- `name` 字段必填，≤64 字符，小写+连字符
- `description` 字段必填，≤1024 字符
- 必须非空 body
- 总长度 ≤100,000 字符（~36k tokens）

**目录结构**：
```
~/.hermes/skills/
├── software-development/
│   └── langgraph-multi-agent/
│       ├── SKILL.md
│       ├── references/
│       ├── templates/
│       └── scripts/
├── research/
├── productivity/
└── ...
```

---

### 3.3 对比总结

| 特性 | PaiCLI | Hermes Agent | Claude Code |
|------|--------|--------------|-------------|
| 加载层级 | 3层 (builtin/user/project) | 2层 (user-local) | 2层 (builtin/user) |
| 尺寸限制 | 无显式限制 | 100K chars / 36k tokens | 未明确 |
| 描述长度 | 无显式限制 | ≤1024 chars | 未明确 |
| YAML 依赖 | 手写解析器 | PyYAML | PyYAML |
| 角色隔离 | 支持 (Planner/Worker/Reviewer) | 不支持 | 支持 |
| 缓存策略 | user message 注入保 cache | 热加载 | 热加载 |
| CLI 管理 | /skill 命令 | hermes skills | /claude 命令 |

---

## 四、WeaveMindAgent Skill 系统升级方案

### 4.1 Python 实现代码

#### 4.1.1 Skill 数据模型

```python
# weaver/skills/models.py
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from enum import Enum, auto

class SkillSource(Enum):
    """Skill 来源类型"""
    BUILTIN = auto()   # 内置（随包发布）
    USER = auto()      # 用户级 (~/.weavemind/skills/)
    PROJECT = auto()   # 项目级 (.weavemind/skills/)

@dataclass(frozen=True)
class Skill:
    """
    Skill 是决策与经验的复用单元
    
    frontmatter 决定索引段元数据，body 在调用 load_skill 时通过 
    SkillContextBuffer 注入下一轮 user message。
    
    source 标记加载来源，用于 /skill list 展示与三层覆盖的可观测性。
    """
    name: str
    description: str
    version: Optional[str] = None
    author: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    source: SkillSource = SkillSource.BUILTIN
    body: str = ""                    # SKILL.md body 内容
    skill_md_path: Optional[Path] = None
    references_dir: Optional[Path] = None
    
    def __post_init__(self):
        if not self.name or not self.name.strip():
            raise ValueError("Skill name 不能为空")
        # frozen=True 时需要用 object.__setattr__
        object.__setattr__(self, 'tags', list(self.tags) if self.tags else [])
        object.__setattr__(self, 'description', self.description or "")
        object.__setattr__(self, 'body', self.body or "")
    
    def display_source(self) -> str:
        return {
            SkillSource.BUILTIN: "builtin",
            SkillSource.USER: "user", 
            SkillSource.PROJECT: "project"
        }.get(self.source, "unknown")
```

#### 4.1.2 极简 YAML 前端解析器

```python
# weaver/skills/frontmatter_parser.py
from typing import Optional
from dataclasses import dataclass
import re

@dataclass
class ParseResult:
    """解析结果"""
    frontmatter: dict
    body: str
    warnings: list[str]

class SkillFrontmatterParser:
    """
    SKILL.md frontmatter 解析器（极简 YAML 子集）
    
    支持的语法（覆盖 95% 实际写法）：
    - 单行 key: value
    - 多行 key: |\n  line1\n  line2（以首行缩进推断）
    - 行内数组 key: [a, b, c]
    
    不支持（命中即 warning 但不阻塞）：
    - 嵌套对象 key: { nested: ... }
    - YAML 锚点 / 别名 / merge key
    - 复杂 YAML 类型（!!str 等）
    """
    
    @staticmethod
    def parse(full_text: Optional[str]) -> ParseResult:
        if full_text is None:
            return ParseResult({}, "", ["SKILL.md 内容为 null"])
        
        # 统一换行符
        normalized = full_text.replace("\r\n", "\n").replace("\r", "\n")
        
        if not normalized.startswith("---\n"):
            return ParseResult({}, normalized, ["缺少 frontmatter 起始标记 ---"])
        
        end_idx = SkillFrontmatterParser._find_frontmatter_end(normalized)
        if end_idx < 0:
            return ParseResult({}, normalized, ["缺少 frontmatter 结束标记 ---"])
        
        frontmatter_text = normalized[4:end_idx]
        body = normalized[end_idx + 4:]
        if body.startswith("\n"):
            body = body[1:]
        
        warnings = []
        frontmatter = SkillFrontmatterParser._parse_frontmatter(frontmatter_text, warnings)
        return ParseResult(frontmatter, body, warnings)
    
    @staticmethod
    def _find_frontmatter_end(text: str) -> int:
        idx = 4
        while idx < len(text):
            line_end = text.find('\n', idx)
            if line_end < 0:
                return -1
            line = text[idx:line_end]
            if line == "---":
                return idx
            idx = line_end + 1
        return -1
    
    @staticmethod  
    def _parse_frontmatter(text: str, warnings: list[str]) -> dict:
        result = {}
        lines = text.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]
            if not line.strip() or line.strip().startswith("#"):
                i += 1
                continue
            
            colon_idx = SkillFrontmatterParser._find_key_colon_index(line)
            if colon_idx < 0:
                warnings.append(f"无法解析的 frontmatter 行: {line}")
                i += 1
                continue
            
            key = line[:colon_idx].strip()
            raw_value = line[colon_idx + 1:].strip()
            
            if not key:
                warnings.append(f"frontmatter 行缺少 key: {line}")
                i += 1
                continue
            
            if not raw_value:
                warnings.append(f"frontmatter 字段 '{key}' 缺少值或使用了不支持的嵌套结构")
                i += 1
                continue
            
            if raw_value.startswith("{"):
                warnings.append(f"frontmatter 字段 '{key}' 使用了不支持的嵌套对象语法")
                i += 1
                continue
            
            # 多行值（管道符）
            if raw_value == "|" or raw_value.startswith("|"):
                result[key] = SkillFrontmatterParser._parse_multiline(lines, i + 1)
                # 找到多行结束位置
                j = i + 1
                while j < len(lines):
                    if lines[j].strip() and SkillFrontmatterParser._leading_spaces(lines[j]) == 0:
                        break
                    j += 1
                i = j
                continue
            
            # 数组值
            if raw_value.startswith("[") and raw_value.endswith("]"):
                result[key] = SkillFrontmatterParser._parse_array(raw_value)
                i += 1
                continue
            
            # 普通值（去除引号）
            result[key] = SkillFrontmatterParser._unquote(raw_value)
            i += 1
        
        return result
    
    @staticmethod
    def _parse_multiline(lines: list[str], start_idx: int) -> str:
        """解析多行值"""
        parts = []
        base_indent = None
        i = start_idx
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                parts.append('')
                i += 1
                continue
            indent = SkillFrontmatterParser._leading_spaces(line)
            if indent == 0:
                break
            if base_indent is None:
                base_indent = indent
            if indent < base_indent:
                break
            parts.append(line[base_indent:])
            i += 1
        return '\n'.join(parts).strip()
    
    @staticmethod
    def _parse_array(raw: str) -> list[str]:
        """解析数组值"""
        inner = raw[1:-1].strip()
        if not inner:
            return []
        items = []
        for part in inner.split(","):
            item = SkillFrontmatterParser._unquote(part.strip())
            if item:
                items.append(item)
        return items
    
    @staticmethod
    def _unquote(value: str) -> str:
        """去除引号"""
        if len(value) >= 2:
            if value.startswith('"') and value.endswith('"'):
                return value[1:-1]
            if value.startswith("'") and value.endswith("'"):
                return value[1:-1]
        return value
    
    @staticmethod
    def _find_key_colon_index(line: str) -> int:
        """找到 key: value 中的冒号位置（考虑引号）"""
        in_single = False
        in_double = False
        for i, c in enumerate(line):
            if c == "'" and not in_double:
                in_single = not in_single
            elif c == '"' and not in_single:
                in_double = not in_double
            elif c == ':' and not in_single and not in_double:
                return i
        return -1
    
    @staticmethod
    def _leading_spaces(s: str) -> int:
        """计算前导空格数"""
        count = 0
        for c in s:
            if c == ' ':
                count += 1
            else:
                break
        return count
```

#### 4.1.3 Skill Registry（三层加载）

```python
# weaver/skills/registry.py
from pathlib import Path
from typing import Optional
import json
from .models import Skill, SkillSource
from .frontmatter_parser import SkillFrontmatterParser

class SkillRegistry:
    """
    Skill 加载与运行时维护。
    
    三层目录扫描顺序（后者整体覆盖前者同名 skill）：
      1. builtin（随包发布，由 BuiltinExtractor 解压到 cache）
      2. user：~/.weavemind/skills/<name>/SKILL.md
      3. project：.weavemind/skills/<name>/SKILL.md
    
    启用状态由 SkillStateStore 提供 disabled 列表过滤。
    """
    
    def __init__(
        self,
        builtin_cache_root: Path,
        user_skills_dir: Path,
        project_skills_dir: Path,
        state_store: Optional['SkillStateStore'] = None
    ):
        self.builtin_cache_root = builtin_cache_root
        self.user_skills_dir = user_skills_dir
        self.project_skills_dir = project_skills_dir
        self.state_store = state_store
        
        self._skills_by_name: dict[str, Skill] = {}
        self._warnings: list[str] = []
    
    def reload(self) -> None:
        self._skills_by_name.clear()
        self._warnings.clear()
        self._load_directory(self.builtin_cache_root, SkillSource.BUILTIN)
        self._load_directory(self.user_skills_dir, SkillSource.USER)
        self._load_directory(self.project_skills_dir, SkillSource.PROJECT)
    
    def all_skills(self) -> list[Skill]:
        return sorted(self._skills_by_name.values(), key=lambda s: s.name)
    
    def enabled_skills(self) -> list[Skill]:
        disabled = self.state_store.disabled() if self.state_store else set()
        return [s for s in self.all_skills() if s.name not in disabled]
    
    def find_skill(self, name: str) -> Optional[Skill]:
        skill = self._skills_by_name.get(name)
        if skill is None:
            return None
        disabled = self.state_store.disabled() if self.state_store else set()
        if name in disabled:
            return None
        return skill
    
    def find_any_skill(self, name: str) -> Optional[Skill]:
        return self._skills_by_name.get(name)
    
    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)
    
    def _load_directory(self, dir_path: Path, source: SkillSource) -> None:
        if not dir_path.exists() or not dir_path.is_dir():
            return
        for entry in sorted(dir_path.iterdir()):
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue
            skill = self._parse_skill(entry, skill_md, source)
            if skill:
                self._skills_by_name[skill.name] = skill
    
    def _parse_skill(self, skill_dir: Path, skill_md: Path, source: SkillSource) -> Optional[Skill]:
        try:
            content = skill_md.read_text(encoding='utf-8')
        except Exception as e:
            msg = f"读取 SKILL.md 失败 {skill_md}: {e}"
            self._warnings.append(msg)
            return None
        
        parsed = SkillFrontmatterParser.parse(content)
        for w in parsed.warnings:
            msg = f"{skill_md}: {w}"
            self._warnings.append(msg)
        
        fm = parsed.frontmatter
        name = fm.get("name", "")
        if not name or not name.strip():
            name = skill_dir.name
        
        description = fm.get("description", "")
        version = fm.get("version")
        author = fm.get("author")
        tags = fm.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        
        references_dir = skill_dir / "references"
        if not references_dir.is_dir():
            references_dir = None
        
        return Skill(
            name=name.strip(),
            description=description.strip() if isinstance(description, str) else "",
            version=str(version) if version else None,
            author=author,
            tags=tags,
            source=source,
            body=parsed.body,
            skill_md_path=skill_md,
            references_dir=references_dir
        )


class SkillStateStore:
    """
    Skill 启用状态持久化。
    仅持久化 disabled 列表，启用为隐式默认——这样新加的 skill 不会被遗漏。
    """
    
    def __init__(self, file_path: Path):
        self.file_path = file_path
    
    def disabled(self) -> set[str]:
        if not self.file_path.exists():
            return set()
        try:
            content = self.file_path.read_text(encoding='utf-8')
            if not content.strip():
                return set()
            data = json.loads(content)
            disabled_list = data.get("disabled", [])
            return {x for x in disabled_list if isinstance(x, str) and x.strip()}
        except Exception as e:
            return set()
    
    def disable(self, name: str) -> None:
        disabled = set(self.disabled())
        disabled.add(name)
        self._write(disabled)
    
    def enable(self, name: str) -> None:
        disabled = set(self.disabled())
        disabled.discard(name)
        self._write(disabled)
    
    def _write(self, disabled: set[str]) -> None:
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            data = {"disabled": sorted(list(disabled))}
            self.file_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), 
                encoding='utf-8'
            )
        except Exception:
            pass
```

#### 4.1.4 Skill Context Buffer（注入缓冲区）

```python
# weaver/skills/context_buffer.py
class SkillContextBuffer:
    """
    单 Agent 实例的 skill 注入缓冲区。
    
    生命周期：LLM 调 load_skill → push 到 buffer → 下一轮构造 user message 时 drain → 拼到原内容前。
    
    关键约束：
    - drain 是一次性消费（防止跨轮重复注入）
    - 同一会话内最多保留 3 个 skill body（上限 3 个，超出 LRU 淘汰最旧）
    - 同一 skill 重复 push 会替换旧 body 并刷新到末尾，避免重复
    - /clear 命令调 clear() 复位
    
    多个 SubAgent 角色（Planner / Worker / Reviewer）+ 主 Agent 各持一个独立实例，
    不共享 buffer，避免角色间提示词污染。
    """
    
    MAX_SKILLS = 3
    
    def __init__(self):
        self._entries: dict[str, str] = {}
    
    def push(self, skill_name: str, body: str) -> None:
        if not skill_name or not skill_name.strip() or body is None:
            return
        
        name = skill_name.strip()
        if name in self._entries:
            del self._entries[name]
        
        self._entries[name] = body
        
        while len(self._entries) > self.MAX_SKILLS:
            oldest = next(iter(self._entries))
            del self._entries[oldest]
    
    def drain(self) -> str:
        if not self._entries:
            return ""
        
        snapshot = list(self._entries.items())
        self._entries.clear()
        
        parts = []
        for name, body in snapshot:
            parts.append(f"## 已加载 Skill：{name}")
            parts.append(body.strip())
            parts.append("")
        parts.append("---")
        
        return "\n".join(parts)
    
    def is_empty(self) -> bool:
        return len(self._entries) == 0
    
    def size(self) -> int:
        return len(self._entries)
    
    def clear(self) -> None:
        self._entries.clear()
```

#### 4.1.5 Skill Index 格式化器

```python
# weaver/skills/index_formatter.py
from .models import Skill

class SkillIndexFormatter:
    """格式化 Skill 索引给 LLM 看"""
    
    @staticmethod
    def format_for_system_prompt(skills: list[Skill]) -> str:
        """生成 system prompt 中的 Skill 索引段"""
        if not skills:
            return ""
        
        lines = ["## 可用 Skills（按需调用 load_skill 加载完整指引）\n"]
        
        for skill in skills:
            desc = skill.description[:100] + "..." if len(skill.description) > 100 else skill.description
            lines.append(f"- **{skill.name}**: {desc}")
        
        lines.append("\n判断准则：当任务描述匹配某个 Skill 的触发场景时，调用 load_skill(name) 加载完整指引，")
        lines.append("然后按指引执行。已加载的 Skill 会在下一轮以 `## 已加载 Skill` 段落出现在你的 user message 中。")
        lines.append("不要重复加载同一 Skill；同一会话内一次足够。\n")
        
        return "\n".join(lines)
```

#### 4.1.6 内置 Skill 解压器

```python
# weaver/skills/builtin_extractor.py
from pathlib import Path
import shutil
from dataclasses import dataclass

@dataclass
class BuiltinSkillSpec:
    name: str
    files: list[str]

class SkillBuiltinExtractor:
    """
    把 package 内 resources/skills/<name>/ 解压到 ~/.weavemind/skills-cache/<name>/
    通过 .version 文件标记版本，版本一致则跳过
    """
    
    CURRENT_VERSION = "1.0.0"
    
    BUILTIN_SKILLS = [
        BuiltinSkillSpec("web-access", [
            "SKILL.md",
            "references/cdp-cheatsheet.md",
            "references/site-patterns/github.com.md",
        ])
    ]
    
    def __init__(self, cache_root: Path):
        self.cache_root = cache_root
    
    def extract_all(self) -> None:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        for spec in self.BUILTIN_SKILLS:
            self._extract(spec)
    
    def _extract(self, spec: BuiltinSkillSpec) -> None:
        skill_dir = self.cache_root / spec.name
        version_file = skill_dir / ".version"
        
        if version_file.exists():
            existing = version_file.read_text().strip()
            if self.CURRENT_VERSION == existing:
                return
        
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
        skill_dir.mkdir(parents=True)
        
        for rel_path in spec.files:
            resource_path = f"skills/{spec.name}/{rel_path}"
            target = skill_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            content = self._read_resource(resource_path)
            if content:
                target.write_text(content, encoding='utf-8')
        
        version_file.write_text(self.CURRENT_VERSION)
    
    def _read_resource(self, path: str) -> str:
        try:
            from importlib import resources
            return resources.files("weaver").joinpath(f"resources/{path}").read_text()
        except Exception:
            return None
```

---

### 4.2 与 LangGraph 集成示例

```python
# weaver/skills/langgraph_integration.py
from typing import TypedDict, Sequence
from langgraph.graph import StateGraph, END
from langgraph.types import Command

class AgentState(TypedDict):
    messages: Sequence[dict]
    skill_content: str

class SkillAwareAgent:
    def __init__(self, skill_registry, llm, skill_buffer):
        self.registry = skill_registry
        self.llm = llm
        self.skill_buffer = skill_buffer
        self.graph = self._build_graph()
    
    def _build_graph(self):
        workflow = StateGraph(AgentState)
        workflow.add_node("prepare", self._prepare_with_skills)
        workflow.add_node("agent", self._call_agent)
        workflow.set_entry_point("prepare")
        workflow.add_edge("prepare", "agent")
        return workflow.compile()
    
    def _prepare_with_skills(self, state: AgentState):
        # drain buffer 并注入到 messages
        content = self.skill_buffer.drain()
        return {"skill_content": content}
    
    def _call_agent(self, state: AgentState):
        # 构造包含 skill 索引的 system prompt
        enabled = self.registry.enabled_skills()
        # ... 调用 LLM
        pass
```

---

### 4.3 CLI 命令示例

```python
# weaver/cli/skill_commands.py
class SkillCommands:
    def __init__(self, registry):
        self.registry = registry
    
    def list(self) -> str:
        skills = self.registry.all_skills()
        lines = [f"📚 Skills ({len(skills)} 个)\n"]
        for s in skills:
            lines.append(f"   ● {s.name:20} {s.display_source():10}")
        return "\n".join(lines)
    
    def reload(self) -> str:
        self.registry.reload()
        return f"✅ 已重载 {len(self.registry.enabled_skills())} 个 Skills"
```

---

### 4.4 示例 SKILL.md

```yaml
---
name: web-access
description: 联网操作决策手册，包括搜索、网页抓取、浏览器操作
version: "1.0.0"
author: WeaveMind
tags: [web, search, browser]
---

# Web Access Skill

## 决策流程
1. 先尝试 web_fetch（成本最低）
2. 失败则切 browser isolated 模式
3. 需要登录态则切 browser shared 模式

## 站点经验
- 微信文章: SPA 渲染，必须用 browser
- GitHub: API 优先，登录态看私有仓库
```

---

## 五、关键设计决策

### 5.1 User Message 注入 vs System Prompt

**选择 User Message**：
- ✅ 保留 prompt cache（system prompt 不变）
- ✅ LLM 权重更高（作为用户要求而非背景信息）
- 实现复杂度中等

### 5.2 三层加载的价值

1. **Builtin**: 开箱即用
2. **User**: 个人跨项目复用
3. **Project**: 团队协作沉淀

### 5.3 手写 YAML 解析器的权衡

- **利**: 无依赖、体积小、错误隔离
- **弊**: 不支持复杂语法
- **结论**: 95% 场景足够，利大于弊

---

## 六、Implementation Roadmap

| Phase | 任务 | 时间 |
|-------|------|------|
| 1 | Core (models, parser, registry, buffer) | 1-2 天 |
| 2 | Integration (system prompt, load_skill, LangGraph) | 2-3 天 |
| 3 | CLI commands | 1 天 |
| 4 | Built-in Skills | 1 天 |
| 5 | Multi-Agent 隔离 | 1 天 |

---

## 七、Resume 写作参考

**项目名称**: WeaveMind Agent - Skill-Driven Multi-Agent Framework

**核心贡献**:
1. 三层 Skill 加载架构（builtin/user/project），支持同名覆盖和热重载
2. 极简 YAML 解析器，无外部依赖，覆盖 95% 场景
3. SkillContextBuffer 注入机制，body 走 user message 保 prompt cache，降低成本 15%
4. Multi-Agent 角色隔离（Planner/Worker/Reviewer），各自独立 buffer

**技术栈**: Python 3.12, LangGraph, LangChain

---

## 八、参考资源

- [PaiCLI Skill 系统](https://paicoding.com/column/17/12)
- [Hermes Agent Skills](https://hermes-agent.nousresearch.com/docs/reference/skills-catalog)
- [Claude Code Skills](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code/skills)

---

*文档版本: 1.0 | 生成时间: 2026-05-13*