"""Deterministic document chunking for the RAG ingestion pipeline.

Chunks are derived from the source text so re-indexing a document with the
same settings reproduces identical offsets, overlap, heading context and page
numbers. Consecutive chunks overlap by up to ``overlap`` characters (aligned to
a word boundary) so a fact split across a boundary is retrievable from either
side. Token counts are approximations (``len // 4``) used only for budgets.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")


def estimate_tokens(text: str) -> int:
    """Rough token estimate for a piece of text."""
    return max(1, len(text) // 4)


@dataclass(frozen=True)
class ChunkSpec:
    text: str
    chunk_index: int
    offset_start: int
    offset_end: int
    heading_path: list[str]
    token_count: int
    page_number: int | None = None


@dataclass
class _HeadingIndex:
    """Heading path in effect from each heading line's offset onwards."""

    offsets: list[int] = field(default_factory=list)
    paths: list[list[str]] = field(default_factory=list)

    @classmethod
    def build(cls, text: str) -> _HeadingIndex:
        index = cls()
        stack: list[tuple[int, str]] = []
        offset = 0
        for line in text.split("\n"):
            match = _HEADING_RE.match(line.strip())
            if match:
                level = len(match.group(1))
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, match.group(2).strip()))
                index.offsets.append(offset)
                index.paths.append([title for _, title in stack])
            offset += len(line) + 1
        return index

    def path_at(self, offset: int) -> list[str]:
        position = bisect_right(self.offsets, offset) - 1
        return list(self.paths[position]) if position >= 0 else []


class TextChunker:
    """Split text into overlapping, heading-aware chunks."""

    def __init__(self, *, size: int = 1000, overlap: int = 200) -> None:
        if size <= 0:
            raise ValueError("chunk size must be positive")
        if overlap < 0 or overlap >= size:
            raise ValueError("overlap must be in [0, size)")
        self.size = size
        self.overlap = overlap
        # Guarantees forward progress even when a natural break falls early.
        self._min_step = max(1, (size - overlap) // 2)

    def chunk(self, text: str, *, page_offsets: Sequence[int] | None = None) -> list[ChunkSpec]:
        """Generate overlapping chunks from ``text``.

        ``page_offsets`` are the ascending start offsets of each page in the
        normalized text (from PDF extraction); chunks then carry the 1-based
        page on which they start.
        """
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        if not normalized.strip():
            return []
        headings = _HeadingIndex.build(normalized)
        pages = list(page_offsets or [])
        length = len(normalized)
        chunks: list[ChunkSpec] = []
        start = 0
        while start < length:
            end = self._boundary_end(normalized, start)
            raw = normalized[start:end]
            if raw.strip():
                chunks.append(
                    ChunkSpec(
                        text=raw,
                        chunk_index=len(chunks),
                        offset_start=start,
                        offset_end=end,
                        heading_path=headings.path_at(start),
                        token_count=estimate_tokens(raw),
                        page_number=bisect_right(pages, start) if pages else None,
                    )
                )
            if end >= length:
                break
            start = self._next_start(normalized, start, end)
        return chunks

    def _boundary_end(self, text: str, start: int) -> int:
        """End offset for the chunk at ``start``: paragraph, line, then word break."""
        hard_end = min(start + self.size, len(text))
        if hard_end >= len(text):
            return len(text)
        window_start = start + self.size // 2
        for separator in ("\n\n", "\n", " "):
            position = text.rfind(separator, window_start, hard_end)
            if position > start:
                return position
        return hard_end

    def _next_start(self, text: str, start: int, end: int) -> int:
        """Start the next chunk ``overlap`` characters back, on a word boundary."""
        candidate = max(end - self.overlap, start + self._min_step)
        if candidate >= end:
            return end
        if candidate > 0 and not text[candidate - 1].isspace():
            space = text.find(" ", candidate, end)
            newline = text.find("\n", candidate, end)
            breaks = [position for position in (space, newline) if position != -1]
            candidate = min(breaks) + 1 if breaks else candidate
        return min(candidate, end)


def chunk_text(
    text: str, *, size: int = 1000, overlap: int = 200, page_offsets: Sequence[int] | None = None
) -> list[ChunkSpec]:
    """Convenience wrapper around :class:`TextChunker`."""
    return TextChunker(size=size, overlap=overlap).chunk(text, page_offsets=page_offsets)
