"""Parser factory for knowledge RAG."""

from __future__ import annotations

import logging

import settings
from knowledge_rag.parsers.base import DocumentParser
from knowledge_rag.parsers.docling_parser import DoclingDocumentParser
from knowledge_rag.parsers.text_parser import PlainTextDocumentParser
from knowledge_rag.parsers.unstructured_parser import UnstructuredDocumentParser

logger = logging.getLogger(__name__)


def create_parser() -> DocumentParser:
    """Create document parser based on config with fallback chain."""
    provider = settings.get("knowledge_rag.parser.provider", "plain-text")
    fallback = settings.get("knowledge_rag.parser.fallback", "plain-text")

    candidates = [provider, fallback]
    parsers: list[DocumentParser] = []
    for name in candidates:
        if name == "docling":
            parsers.append(DoclingDocumentParser())
        elif name == "unstructured":
            parsers.append(UnstructuredDocumentParser())
        else:
            parsers.append(PlainTextDocumentParser())

    if len(parsers) == 1:
        return parsers[0]
    return _ChainedParser(parsers)


class _ChainedParser(DocumentParser):
    """Try primary parser first, fall back on empty result or unsupported file."""

    provider = "chained"
    version = "chained@1"

    def __init__(self, parsers: list[DocumentParser]):
        self.parsers = parsers

    def supports(self, file_path: str) -> bool:
        return any(p.supports(file_path) for p in self.parsers)

    def parse(self, file_path: str) -> list[ParsedElement]:
        for parser in self.parsers:
            if not parser.supports(file_path):
                continue
            try:
                elements = parser.parse(file_path)
                if elements:
                    return elements
            except Exception as e:
                logger.debug("Parser %s 失败: %s", parser.provider, e)
        return []
