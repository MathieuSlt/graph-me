"""Language rule packs (rules/<lang>.yaml): stopwords, injection phrases, fact rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from importlib import resources

import yaml


@dataclass(frozen=True)
class RulePack:
    language: str
    stopwords: frozenset[str]
    injection: tuple[str, ...]
    birthday_greetings: tuple[str, ...] = field(default=())
    birthday_exclude: tuple[str, ...] = field(default=())


@cache
def load_all() -> dict[str, RulePack]:
    packs = {}
    for entry in resources.files("graph_me.rules").iterdir():
        if not entry.name.endswith(".yaml"):
            continue
        raw = yaml.safe_load(entry.read_text(encoding="utf-8"))
        words = raw.get("stopwords", [])
        if not all(isinstance(w, str) for w in words):
            raise ValueError(
                f"{entry.name}: quote stopwords YAML reads as non-strings (on, no, ...)"
            )
        packs[raw["language"]] = RulePack(
            language=raw["language"],
            stopwords=frozenset(w.casefold() for w in words),
            injection=tuple(raw.get("injection", [])),
            birthday_greetings=tuple(raw.get("birthday", {}).get("greetings", [])),
            birthday_exclude=tuple(raw.get("birthday", {}).get("exclude", [])),
        )
    return packs


def all_stopwords() -> frozenset[str]:
    return frozenset().union(*(p.stopwords for p in load_all().values()))


def all_injection_phrases() -> tuple[str, ...]:
    return tuple(ph for p in load_all().values() for ph in p.injection)
