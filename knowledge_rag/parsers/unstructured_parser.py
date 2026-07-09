"""Unstructured.io parser for heterogeneous documents."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from knowledge_rag.models import ParsedElement
from knowledge_rag.parsers.base import DocumentParser

logger = logging.getLogger(__name__)


class UnstructuredDocumentParser(DocumentParser):
    """Parser using unstructured when available."""

    provider = "unstructured"
    version = "unstructured@1"

    def supports(self, file_path: str) -> bool:
        suffix = Path(file_path).suffix.lower()
        return suffix in {
            ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".txt", ".md",
            ".html", ".htm", ".eml", ".msg", ".epub",
        }

    def parse(self, file_path: str) -> list[ParsedElement]:
        try:
            from unstructured.partition.auto import partition
        except Exception as e:
            logger.warning("unstructured 未安装: %s", e)
            return []

        try:
            elements = partition(filename=file_path)
        except Exception as e:
            logger.warning("unstructured 解析失败: %s", e)
            return []

        results: list[ParsedElement] = []
        section_path: list[str] = []
        page_number: Optional[int] = None

        for el in elements:
            text = getattr(el, "text", "")
            if not text or not isinstance(text, str):
                continue
            el_type = type(el).__name__.lower()
            element_type = self._map_type(el_type)
            page_number = getattr(el, "metadata", {}).get("page_number", page_number)

            if element_type == "title":
                level = getattr(el, "metadata", {}).get("category_depth", 1) or 1
                section_path = section_path[: max(level - 1, 0)] + [text.strip()]

            results.append(ParsedElement(
                text=text.strip(),
                element_type=element_type,
                page_number=page_number,
                section_path=list(section_path),
            ))
        return results

    @staticmethod
    def _map_type(type_name: str) -> str:
        mapping = {
            "title": "title",
            "narrativetext": "paragraph",
            "text": "paragraph",
            "listitem": "list",
            "bulletedtext": "list",
            "table": "table",
            "image": "image",
            "figurecaption": "paragraph",
            "header": "paragraph",
            "footer": "paragraph",
            "code": "code",
        }
        return mapping.get(type_name, "paragraph")
