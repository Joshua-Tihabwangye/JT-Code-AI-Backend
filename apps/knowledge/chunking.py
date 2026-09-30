"""Deterministic document chunking for the RAG ingestion pipeline.

Chunks are derived from the source text so re-indexing a document with the
same settings reproduces identical offsets and heading context. Token counts
are approximations (``len // 4``) used only for cost/reserve estimates.
"""

from __future__ import annotations

from dataclasses import dataclass

MARKDOWN_HEADING = "#"


def estimate_tokens(text: str) -> int:
    """Rough token estimate for a piece of text (whitespace-aware)."""
    return max(1, len(text) // 4)


@dataclass(frozen=True)
class ChunkSpec:
    text: str
    chunk_index: int
    offset_start: int
    offset_end: int
    heading_path: list[str]
    token_count: int


class TextChunker:
    """Split text into overlapping, heading-aware chunks."""

    def __init__(self, *, size: int = 1000, overlap: int = 200) -> None:
        if size <= 0:
            raise ValueError("chunk size must be positive")
        if overlap < 0 or overlap >= size:
            raise ValueError("overlap must be in [0, size)")
        self.size = size
        self.overlap = overlap

    @staticmethod
    def _heading_of(line: str) -> str | None:
        stripped = line.strip()
        if stripped.startswith(MARKDOWN_HEADING):
            return stripped.lstrip(MARKDOWN_HEADING).strip()
        return None

    def _active_headings(self, lines: list[str], up_to: int) -> list[str]:
        """Return the heading stack active immediately before ``up_to``."""
        stack: list[str] = []
        for line in lines[:up_to]:
            heading = self._heading_of(line)
            if heading:
                stack.append(heading)
            elif line.strip():
                stack = []
        return stack

    def chunk(self, text: str) -> list[ChunkSpec]:
        """Generate contiguous, overlapping chunks from ``text``."""
        if not text.strip():
            return []
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized.split("\n")
        step = self.size - self.overlap
        cursor = 0
        chunks: list[ChunkSpec] = []

        while cursor < len(normalized):
            end = self._boundary_end(normalized, cursor)
            raw = normalized[cursor:end]
            if raw.strip():
                up_to_line = normalized.count("\n", 0, cursor)
                chunks.append(
                    ChunkSpec(
                        text=raw,
                        chunk_index=len(chunks),
                        offset_start=cursor,
                        offset_end=end,
                        heading_path=self._active_headings(lines, up_to_line),
                        token_count=estimate_tokens(raw),
                    )
                )
            cursor = end if end > cursor else cursor + step

        if not chunks and normalized.strip():
            chunks.append(
                ChunkSpec(
                    text=normalized.strip(),
                    chunk_index=0,
                    offset_start=0,
                    offset_end=len(normalized),
                    heading_path=[],
                    token_count=estimate_tokens(normalized),
                )
            )
        return chunks

    def _boundary_end(self, text: str, start: int) -> int:
        """Return the end offset for the chunk starting at ``start``.

        Prefers a paragraph boundary (``\\n\\n``) inside the acceptable window,
        then a single newline, then a hard character limit. This keeps chunks
        readable while guaranteeing full coverage and overlap.
        """
        if start >= len(text):
            return start
        soft_min = start + self.size - self.overlap
        if soft_min >= len(text):
            return len(text)
        hard_end = min(start + self.size, len(text))

        paragraph_break = text.find("\n\n", soft_min, hard_end)
        if paragraph_break != -1:
            return paragraph_break
        newline = text.rfind("\n", soft_min, hard_end)
        if newline != -1 and newline > soft_min:
            return newline
        return hard_end


def chunk_text(text: str, *, size: int = 1000, overlap: int = 200) -> list[ChunkSpec]:
    """Convenience wrapper around :class:`TextChunker`."""
    return TextChunker(size=size, overlap=overlap).chunk(text)
