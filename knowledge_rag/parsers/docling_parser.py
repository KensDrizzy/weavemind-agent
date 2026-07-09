"""Docling document parser for rich PDF/Office/HTML extraction."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from knowledge_rag.models import ParsedElement
from knowledge_rag.parsers.base import DocumentParser

logger = logging.getLogger(__name__)


class DoclingDocumentParser(DocumentParser):
    """Parser using IBM Docling when available."""

    provider = "docling"
    version = "docling@1"

    def __init__(self, export_format: str = "markdown"):
        self.export_format = export_format
        self._converter = None

    def supports(self, file_path: str) -> bool:
        suffix = Path(file_path).suffix.lower()
        return suffix in {
            ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
            ".html", ".htm", ".md", ".markdown", ".txt",
        }

    def parse(self, file_path: str) -> list[ParsedElement]:
        try:
            from docling.datamodel.base_models import ConversionStatus
            from docling.datamodel.document import ConversionResult
            from docling.document_converter import DocumentConverter
        except Exception as e:
            logger.warning("Docling 未安装: %s", e)
            return []

        if self._converter is None:
            self._converter = DocumentConverter()

        result: ConversionResult = self._converter.convert(file_path)
        if result.status != ConversionStatus.SUCCESS:
            logger.warning("Docling 转换失败: %s", result.status)
            return []

        elements: list[ParsedElement] = []
        page_number: Optional[int] = None
        section_path: list[str] = []

        for item in result.document.iterate_items():
            text = getattr(item, "text", "")
            if not text or not isinstance(text, str):
                continue
            label = str(getattr(item, "label", "paragraph")).lower()
            prov = getattr(item, "prov", None)
            if prov:
                page_number = getattr(prov, "page_no", page_number)

            element_type = self._map_label(label)
            if element_type == "title":
                level = self._heading_level(text)
                section_path = section_path[: max(level - 1, 0)] + [text.strip()]

            elements.append(ParsedElement(
                text=text.strip(),
                element_type=element_type,
                page_number=page_number,
                section_path=list(section_path),
            ))
        return elements

    @staticmethod
    def _map_label(label: str) -> str:
        mapping = {
            "title": "title",
            "section_header": "title",
            "heading": "title",
            "paragraph": "paragraph",
            "text": "paragraph",
            "list_item": "list",
            "table": "table",
            "figure": "image",
            "caption": "paragraph",
            "code": "code",
            "page_header": "paragraph",
            "page_footer": "paragraph",
        }
        return mapping.get(label, "paragraph")

    @staticmethod
    def _heading_level(text: str) -> int:
        stripped = text.lstrip("#")
        if stripped != text:
            return min(len(text) - len(stripped), 6) or 1
        return 1
