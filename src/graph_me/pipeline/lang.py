"""Language detection without a model: the rule pack whose stopwords match most wins."""

from __future__ import annotations

import re

from graph_me import rules

_WORD = re.compile(r"\w+")
_SAMPLE_WORDS = 2000
_MIN_HITS = 3


def detect(text: str) -> str | None:
    words = [w.casefold() for w in _WORD.findall(text[: _SAMPLE_WORDS * 8])][:_SAMPLE_WORDS]
    scores = {
        lang: sum(w in pack.stopwords for w in words) for lang, pack in rules.load_all().items()
    }
    if not scores:
        return None
    best = max(scores, key=scores.__getitem__)
    ranked = sorted(scores.values(), reverse=True)
    if ranked[0] < _MIN_HITS or (len(ranked) > 1 and ranked[0] == ranked[1]):
        return None
    return best
