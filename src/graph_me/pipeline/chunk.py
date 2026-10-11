"""Split text into chunks of about ``max_tokens`` words, on paragraph boundaries when possible."""

from __future__ import annotations

import re

MAX_TOKENS = 500
_PARAGRAPHS = re.compile(r"\n\s*\n|\r\n\s*\r\n")


def chunk_table(text: str, max_tokens: int = MAX_TOKENS) -> list[tuple[str, int]]:
    """CSV/TSV: rows grouped into chunks, each starting with the header row, so a row found by
    search keeps its column names ("Date, Payee, Amount")."""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return chunk(text, max_tokens)
    header, rows = lines[0], lines[1:]
    budget = max(1, max_tokens - len(header.split()))
    chunks, buf, size = [], [], 0
    for row in rows:
        words = len(row.split()) or 1
        if buf and size + words > budget:
            chunks.append(("\n".join([header, *buf]), size + len(header.split())))
            buf, size = [], 0
        buf.append(row)
        size += words
    if buf:
        chunks.append(("\n".join([header, *buf]), size + len(header.split())))
    return chunks


def chunk(text: str, max_tokens: int = MAX_TOKENS) -> list[tuple[str, int]]:
    """Return ``(chunk_text, token_estimate)`` pairs. Tokens are estimated as words."""
    chunks: list[tuple[str, int]] = []
    buf: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buf, size
        if buf:
            chunks.append(("\n\n".join(buf), size))
        buf, size = [], 0

    for para in _PARAGRAPHS.split(text):
        words = para.split()
        if not words:
            continue
        if len(words) > max_tokens:  # one huge paragraph: cut it by words
            flush()
            for i in range(0, len(words), max_tokens):
                part = words[i : i + max_tokens]
                chunks.append((" ".join(part), len(part)))
            continue
        if size + len(words) > max_tokens:
            flush()
        buf.append(para.strip())
        size += len(words)
    flush()
    return chunks
