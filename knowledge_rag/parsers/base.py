"""Parser interfaces for knowledge RAG."""

from abc import ABC, abstractmethod
from pathlib import Path

from knowledge_rag.models import ParsedElement


class DocumentParser(ABC):
    provider = "base"
    version = "base@1"

    @abstractmethod
    def parse(self, file_path: str) -> list[ParsedElement]:
        """Parse a document into structured elements."""

    def supports(self, file_path: str) -> bool:
        return Path(file_path).is_file()
