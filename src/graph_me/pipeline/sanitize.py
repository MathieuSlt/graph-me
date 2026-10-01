"""Ingest-time sanitization (OWASP LLM prompt-injection cheat sheet: remote content).

Content from files and messages is untrusted. Before indexing we:

- strip invisible characters (zero-width, bidi overrides, Unicode tag characters) that can hide
  instructions from a human reader;
- decode long base64 / hex runs and look inside them;
- fuzzy-match known injection phrases, ignoring case, accents, spacing and scrambled inner
  letters ("ignroe prevoius instructions").

Nothing is removed except invisible characters: the text stays searchable, and the result
carries a ``risk_score`` (0 to 1) that the query layer shows to the agent.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass, field
from functools import cache

from graph_me import rules

_INVISIBLE = re.compile(
    r"[\u00ad\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]"
)
_B64_RUN = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
_HEX_RUN = re.compile(r"\b[0-9a-fA-F]{40,}\b")
_WORD = re.compile(r"\w+")

# Score contributions, capped at 1.
_PHRASE_WEIGHT = 0.6
_ENCODED_PHRASE_WEIGHT = 0.8
_INVISIBLE_WEIGHT = 0.1


@dataclass
class Sanitized:
    text: str
    risk_score: float
    flags: list[str] = field(default_factory=list)


def fold(text: str) -> str:
    """Casefold and strip accents: 'Café Déjà' -> 'cafe deja'."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _same_word(candidate: str, target: str) -> bool:
    """Exact match, or a typoglycemia variant: same length, first and last letters, same letters."""
    if candidate == target:
        return True
    if len(target) < 4 or len(candidate) != len(target):
        return False
    return (
        candidate[0] == target[0]
        and candidate[-1] == target[-1]
        and sorted(candidate[1:-1]) == sorted(target[1:-1])
    )


def _key(word: str) -> tuple[str, str, int]:
    # Any typoglycemia variant keeps its first letter, last letter and length.
    return (word[0], word[-1], len(word))


@cache
def _phrases_by_first_word() -> dict[tuple[str, str, int], list[tuple[str, ...]]]:
    index: dict[tuple[str, str, int], list[tuple[str, ...]]] = {}
    for raw in rules.all_injection_phrases():
        phrase = tuple(_WORD.findall(fold(raw)))
        if phrase:
            index.setdefault(_key(phrase[0]), []).append(phrase)
    return index


def find_phrases(text: str) -> list[str]:
    """Return the injection phrases found in ``text`` (fuzzy, word-aligned)."""
    words = _WORD.findall(fold(text))
    index = _phrases_by_first_word()
    found: dict[str, None] = {}
    for i, word in enumerate(words):
        for phrase in index.get(_key(word), ()):
            n = len(phrase)
            if i + n <= len(words) and all(_same_word(words[i + k], phrase[k]) for k in range(n)):
                found[" ".join(phrase)] = None
    return list(found)


def _decoded_runs(text: str) -> list[str]:
    decoded = []
    for match in _B64_RUN.finditer(text):
        run = match.group(0)
        try:
            raw = base64.b64decode(run + "=" * (-len(run) % 4), validate=True)
        except (binascii.Error, ValueError):
            continue
        decoded.append(raw.decode("utf-8", errors="ignore"))
    for match in _HEX_RUN.finditer(text):
        run = match.group(0)
        if len(run) % 2:
            continue
        decoded.append(bytes.fromhex(run).decode("utf-8", errors="ignore"))
    return [d for d in decoded if _mostly_printable(d)]


def _mostly_printable(s: str) -> bool:
    return bool(s) and sum(ch.isprintable() or ch.isspace() for ch in s) / len(s) > 0.9


def sanitize(text: str) -> Sanitized:
    flags: list[str] = []
    score = 0.0

    clean, invisible = _INVISIBLE.subn("", text)
    if invisible:
        flags.append(f"invisible:{invisible}")
        score += _INVISIBLE_WEIGHT

    phrases = find_phrases(clean)
    if phrases:
        flags.extend(f"phrase:{p}" for p in phrases)
        score += _PHRASE_WEIGHT

    for decoded in _decoded_runs(clean):
        hidden = find_phrases(decoded)
        if hidden:
            flags.extend(f"encoded:{p}" for p in hidden)
            score += _ENCODED_PHRASE_WEIGHT
            break

    return Sanitized(text=clean, risk_score=min(score, 1.0), flags=flags)
