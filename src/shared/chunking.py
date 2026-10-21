"""Word-based chunking for the embedding step (default 500 words / 50 overlap)."""
from __future__ import annotations

from typing import Iterator

CHUNK_SIZE = 500
CHUNK_OVERLAP = 50


def chunk_words(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> Iterator[str]:
    words = text.split()
    if not words:
        return
    step = max(1, size - overlap)
    start = 0
    while True:
        chunk = words[start:start + size]
        if start > 0 and len(chunk) < 20:
            break        # tail that the previous chunk already absorbed
        yield " ".join(chunk)
        if start + size >= len(words):
            break
        start += step


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    return list(chunk_words(text, size, overlap))
