"""HTML 正文提取器 — 噪声清理 + 语义定位 + 评分兜底 + Markdown 转换。

四步流程：
1. 清理噪声标签（script/style/nav/aside/footer/header/form/iframe + 广告/侧边栏）
2. 找主语义容器（article > main > [role=main]）
3. 打分兜底（文本长度 × (1 - 链接密度惩罚)）
4. 转 Markdown（标题/段落/代码/链接/列表/表格）

已知边界：
- JS 渲染的 SPA 页面可能提取不到正文
- Cloudflare 反爬页面提取到的是验证脚本
- 遇到空正文返回提示，让 Agent 知道是已知边界，不要反复重试
"""

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

# 需要清理的噪声标签
_NOISE_TAGS = {
    "script", "style", "nav", "aside", "footer", "header",
    "form", "iframe", "noscript", "svg", "canvas",
}

# 噪声关键词（class/id 中包含这些词的元素会被清理）
_NOISE_KEYWORDS = {
    "ads", "ad-", "banner", "sidebar", "comment", "widget",
    "footer", "header", "nav", "menu", "breadcrumb", "cookie",
    "popup", "modal", "overlay", "social", "share", "related",
}


class HtmlExtractor:
    """HTML 正文提取器。"""

    def extract(self, html: str) -> str:
        """从 HTML 提取正文，返回 Markdown。"""
        if not html or not html.strip():
            return ""

        soup = BeautifulSoup(html, "html.parser")

        # Step 1: 清理噪声
        self._remove_noise(soup)

        # Step 2: 找主语义容器
        main = self._find_main_content(soup)

        # Step 3: 评分兜底
        if main is None:
            main = self._score_and_pick(soup)

        if main is None:
            return "未提取到正文。可能是 JS 渲染或防爬页面，请勿重试。"

        # Step 4: 转 Markdown
        return self._to_markdown(main)

    def _remove_noise(self, soup: BeautifulSoup):
        """清理噪声标签和元素。"""
        # 删除噪声标签
        for tag_name in _NOISE_TAGS:
            for tag in soup.find_all(tag_name):
                tag.decompose()

        # 删除 class/id 包含噪声关键词的元素
        for tag in soup.find_all(True):
            classes = " ".join(tag.get("class", []))
            tag_id = tag.get("id", "")
            combined = f"{classes} {tag_id}".lower()
            if any(kw in combined for kw in _NOISE_KEYWORDS):
                tag.decompose()

    def _find_main_content(self, soup: BeautifulSoup) -> Optional[Tag]:
        """优先找语义化标签。"""
        for selector in ["article", "main", "[role='main']"]:
            result = soup.select_one(selector)
            if result and len(result.get_text(strip=True)) > 100:
                return result
        return None

    def _score_and_pick(self, soup: BeautifulSoup) -> Optional[Tag]:
        """给所有 block 元素打分，选最高分的。"""
        candidates = []
        for tag in soup.find_all(["div", "section", "article", "td", "main"]):
            score = self._score_element(tag)
            if score > 0:
                candidates.append((score, tag))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    @staticmethod
    def _score_element(el: Tag) -> float:
        """评分公式：文本长度 × (1 - 链接密度惩罚)。"""
        text = el.get_text(strip=True)
        text_len = len(text)

        if text_len < 80:
            return 0

        # 计算链接密度
        link_len = sum(len(a.get_text(strip=True)) for a in el.find_all("a"))
        link_ratio = link_len / text_len if text_len > 0 else 0
        penalty = min(link_ratio * 2.0, 1.0)

        return text_len * (1.0 - penalty)

    def _to_markdown(self, el: Tag) -> str:
        """将 HTML 元素转换为 Markdown。"""
        lines = []

        for child in el.descendants:
            if not isinstance(child, Tag):
                continue

            # 标题
            if child.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                level = int(child.name[1])
                text = child.get_text(strip=True)
                if text:
                    lines.append(f"\n{'#' * level} {text}\n")

            # 段落
            elif child.name == "p":
                text = child.get_text(strip=True)
                if text:
                    lines.append(f"\n{text}\n")

            # 代码块
            elif child.name == "pre":
                code = child.get_text()
                lines.append(f"\n```\n{code.strip()}\n```\n")

            elif child.name == "code" and child.parent.name != "pre":
                text = child.get_text(strip=True)
                if text:
                    lines.append(f"`{text}`")

            # 链接
            elif child.name == "a":
                href = child.get("href", "")
                text = child.get_text(strip=True)
                if href and text and not href.startswith("#"):
                    lines.append(f"[{text}]({href})")

            # 粗体
            elif child.name in ("strong", "b"):
                text = child.get_text(strip=True)
                if text:
                    lines.append(f"**{text}**")

            # 列表项
            elif child.name == "li":
                text = child.get_text(strip=True)
                if text:
                    lines.append(f"- {text}")

            # 表格行
            elif child.name == "tr":
                cells = child.find_all(["td", "th"])
                if cells:
                    cell_texts = [c.get_text(strip=True) for c in cells]
                    lines.append("| " + " | ".join(cell_texts) + " |")
                    # 表头后加分隔线
                    if child.find("th"):
                        lines.append("| " + " | ".join("---" for _ in cells) + " |")

        # 去重连续空行
        result = "\n".join(lines)
        while "\n\n\n" in result:
            result = result.replace("\n\n\n", "\n\n")

        return result.strip()
