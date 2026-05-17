"""SKILL.md frontmatter 解析器（极简 YAML 子集）。"""
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ParseResult:
    frontmatter: dict = field(default_factory=dict)
    body: str = ""
    warnings: list[str] = field(default_factory=list)


class SkillFrontmatterParser:
    """支持: 单行 key: value, 多行 key: |, 行内数组 [a, b, c]。"""

    @staticmethod
    def parse(text: Optional[str]) -> ParseResult:
        if not text:
            return ParseResult(warnings=["SKILL.md 内容为空"])
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        if not normalized.startswith("---\n"):
            return ParseResult(body=normalized, warnings=["缺少 frontmatter 起始标记"])
        end = normalized.find("\n---\n", 4)
        if end < 0:
            return ParseResult(body=normalized, warnings=["缺少 frontmatter 结束标记"])
        fm_text = normalized[4:end]
        body = normalized[end + 5:]
        warnings = []
        fm = SkillFrontmatterParser._parse_kv(fm_text, warnings)
        return ParseResult(frontmatter=fm, body=body, warnings=warnings)

    @staticmethod
    def _parse_kv(text: str, warnings: list) -> dict:
        result = {}
        lines = text.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]
            if not line.strip() or line.strip().startswith("#"):
                i += 1
                continue
            colon = line.find(":")
            if colon < 0:
                warnings.append(f"无法解析: {line}")
                i += 1
                continue
            key = line[:colon].strip()
            val = line[colon + 1:].strip()
            if not key:
                i += 1
                continue
            if val == "|" or val == "|+":
                # 多行值
                parts, i = [], i + 1
                base_indent = None
                while i < len(lines):
                    ln = lines[i]
                    if not ln.strip():
                        parts.append("")
                        i += 1
                        continue
                    indent = len(ln) - len(ln.lstrip())
                    if indent == 0:
                        break
                    if base_indent is None:
                        base_indent = indent
                    parts.append(ln[base_indent:] if indent >= base_indent else ln.lstrip())
                    i += 1
                result[key] = "\n".join(parts).strip()
            elif val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                result[key] = [SkillFrontmatterParser._unquote(x.strip()) for x in inner.split(",") if x.strip()] if inner else []
                i += 1
            elif val.startswith("{"):
                warnings.append(f"不支持嵌套对象: {key}")
                i += 1
            else:
                result[key] = SkillFrontmatterParser._unquote(val)
                i += 1
        return result

    @staticmethod
    def _unquote(s: str) -> str:
        if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
            return s[1:-1]
        return s
