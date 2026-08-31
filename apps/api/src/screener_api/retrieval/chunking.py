"""Chunking that preserves character offsets into the source text.

Offsets are not bookkeeping. In M6 the scorer must verify that every quote it
cites appears **verbatim** in the document, and that check is only possible if a
chunk knows exactly where it came from. A chunker that returns text without
positions quietly makes evidence verification impossible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_SIZE = 1200
DEFAULT_OVERLAP = 200
MIN_CHUNK = 80

# Split on paragraph breaks first, then sentences. Splitting mid-sentence hurts
# retrieval quality and produces quotes that read as fragments.
_PARAGRAPH = re.compile(r"\n{2,}")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    char_start: int
    char_end: int
    section: str | None = None

    def verify(self, source: str) -> bool:
        """The invariant every chunk must satisfy: it is exactly the slice of the
        source it claims to be."""
        return source[self.char_start : self.char_end] == self.text


def _boundaries(text: str) -> list[int]:
    """Candidate split points, preferring paragraph then sentence breaks."""
    points = {0, len(text)}
    for match in _PARAGRAPH.finditer(text):
        points.add(match.end())
    for match in _SENTENCE.finditer(text):
        points.add(match.end())
    return sorted(points)


def chunk_text(
    text: str,
    *,
    size: int = DEFAULT_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    sections: dict[str, str] | None = None,
) -> list[Chunk]:
    """Split text into overlapping chunks that each know their exact span."""
    if not text.strip():
        return []
    if overlap >= size:
        raise ValueError("overlap must be smaller than size")

    points = _boundaries(text)
    chunks: list[Chunk] = []
    start = 0
    index = 0

    while start < len(text):
        target = start + size
        if target >= len(text):
            end = len(text)
        else:
            # Prefer the latest natural boundary inside the window; fall back to
            # a hard cut only when a single sentence is longer than the window.
            candidates = [p for p in points if start + MIN_CHUNK < p <= target]
            end = candidates[-1] if candidates else target

        body = text[start:end]
        if body.strip():
            chunks.append(
                Chunk(
                    index=index,
                    text=body,
                    char_start=start,
                    char_end=end,
                    section=_section_for(start, text, sections),
                )
            )
            index += 1

        if end >= len(text):
            break
        start = max(end - overlap, start + 1)

    return chunks


def _section_for(offset: int, text: str, sections: dict[str, str] | None) -> str | None:
    """Which named section this offset falls in, if segmentation ran."""
    if not sections:
        return None
    best: tuple[int, str] | None = None
    for name, body in sections.items():
        if not body:
            continue
        position = text.find(body[:120])
        if 0 <= position <= offset and (best is None or position > best[0]):
            best = (position, name)
    return best[1] if best else None
