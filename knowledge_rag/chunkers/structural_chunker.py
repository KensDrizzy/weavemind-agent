"""Structure-aware chunker for user documents."""

from __future__ import annotations

import hashlib
import time

from knowledge_rag.models import KnowledgeChunk, KnowledgeDocument, ParsedElement


class StructuralChunker:
    """First-pass chunker that respects titles, lists, tables and pages."""

    version = "structural@1"

    def __init__(self, max_chars: int = 3600, overlap_chars: int = 400):
        self.max_chars = max_chars
        self.overlap_chars = overlap_chars

    def chunk(self, document: KnowledgeDocument, elements: list[ParsedElement]) -> list[KnowledgeChunk]:
        chunks: list[KnowledgeChunk] = []
        current: list[ParsedElement] = []
        current_len = 0

        def flush():
            nonlocal current, current_len
            if not current:
                return
            content = self._render_elements(current)
            if content.strip():
                chunks.append(self._make_chunk(document, content, current, len(chunks)))
            overlap = self._tail_overlap(current)
            current = overlap
            current_len = sum(len(e.text) for e in current)

        for element in elements:
            text_len = len(element.text)
            starts_new_section = element.element_type == "title" and current
            would_overflow = current and current_len + text_len > self.max_chars
            if starts_new_section or would_overflow:
                flush()

            if text_len > self.max_chars:
                for part in self._split_large_text(element.text):
                    split_element = element.model_copy(update={"text": part})
                    current.append(split_element)
                    current_len += len(part)
                    flush()
                continue

            current.append(element)
            current_len += text_len

        current_len_before_flush = current_len
        if current_len_before_flush:
            current_saved = current
            current = current_saved
            current_len = current_len_before_flush
            content = self._render_elements(current)
            if content.strip():
                chunks.append(self._make_chunk(document, content, current, len(chunks)))
        return chunks

    def _make_chunk(
        self,
        document: KnowledgeDocument,
        content: str,
        elements: list[ParsedElement],
        index: int,
    ) -> KnowledgeChunk:
        first = elements[0]
        doc_key = f"{document.doc_id}:{index}:{content[:120]}"
        chunk_id = hashlib.sha1(doc_key.encode("utf-8")).hexdigest()
        element_type = first.element_type
        if any(e.element_type == "table" for e in elements):
            element_type = "table"
        return KnowledgeChunk(
            chunk_id=chunk_id,
            doc_id=document.doc_id,
            tenant_id=document.tenant_id,
            workspace_id=document.workspace_id,
            collection_id=document.collection_id,
            source_file=document.source_file,
            file_name=document.file_name,
            content=content,
            page_number=first.page_number,
            section_path=list(first.section_path),
            element_type=element_type,
            bbox=first.bbox,
            created_at=time.time(),
            acl_hash=document.acl_hash,
            parser_version=document.parser_version,
            chunker_version=self.version,
            embedding_provider=document.embedding_provider,
            embedding_model=document.embedding_model,
            embedding_dimension=document.embedding_dimension,
            embedding_revision=document.embedding_revision,
        )

    @staticmethod
    def _render_elements(elements: list[ParsedElement]) -> str:
        lines: list[str] = []
        previous_type = None
        for element in elements:
            if element.element_type == "title":
                lines.append(f"# {element.text}")
            elif element.element_type == "table":
                lines.append(element.text)
            else:
                if previous_type and previous_type != element.element_type:
                    lines.append("")
                lines.append(element.text)
            previous_type = element.element_type
        return "\n".join(lines).strip()

    def _tail_overlap(self, elements: list[ParsedElement]) -> list[ParsedElement]:
        if self.overlap_chars <= 0:
            return []
        kept: list[ParsedElement] = []
        total = 0
        for element in reversed(elements):
            total += len(element.text)
            kept.append(element)
            if total >= self.overlap_chars:
                break
        return list(reversed(kept))

    def _split_large_text(self, text: str) -> list[str]:
        parts: list[str] = []
        cursor = 0
        while cursor < len(text):
            end = min(cursor + self.max_chars, len(text))
            boundary = text.rfind("。", cursor, end)
            if boundary <= cursor:
                boundary = text.rfind(".", cursor, end)
            if boundary <= cursor:
                boundary = end
            else:
                boundary += 1
            parts.append(text[cursor:boundary].strip())
            cursor = max(boundary - self.overlap_chars, boundary)
        return [p for p in parts if p]
