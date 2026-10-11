"""Language rule packs (rules/<lang>.yaml): stopwords, injection phrases, fact rules
(birthdays, relations, deadlines and amounts), and examples that test them.

Built-in packs live in this folder. You can add your own in ``rules/`` next to config.yaml
(``GRAPH_ME_RULES`` points there; the CLI sets it, so worker processes see it too): a pack for a
new language, or one that adds words to a built-in language (its lists are merged in). Files
whose name starts with ``_`` are skipped (``_template.yaml``). ``graph-me rules check <file>``
validates a pack and runs its examples.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

ENV_RULES = "GRAPH_ME_RULES"
DEADLINE_KEYS = ("expires", "renews", "due", "ends")
AMOUNT_KEYS = ("quote", "invoice")


class _Birthday(BaseModel):
    model_config = ConfigDict(extra="forbid")
    greetings: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class _Matches(BaseModel):
    model_config = ConfigDict(extra="forbid")
    match: list[str] = Field(default_factory=list)
    no_match: list[str] = Field(default_factory=list)


class _Expect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    expect: dict[str, str] = Field(default_factory=dict)  # key -> value; {} = nothing found


class _Examples(BaseModel):
    model_config = ConfigDict(extra="forbid")
    birthday: _Matches = Field(default_factory=_Matches)
    injection: _Matches = Field(default_factory=_Matches)
    values: list[_Expect] = Field(default_factory=list)  # deadlines and amounts
    relations: dict[str, str] = Field(default_factory=dict)  # "my sister" -> sibling


class PackFile(BaseModel):
    """The schema of a rules/<lang>.yaml file."""

    model_config = ConfigDict(extra="forbid")

    language: str = Field(pattern=r"^[a-z]{2,3}$")
    stopwords: list[str] = Field(default_factory=list)
    injection: list[str] = Field(default_factory=list)
    birthday: _Birthday = Field(default_factory=_Birthday)
    relations: dict[str, list[str]] = Field(default_factory=dict)
    date_order: Literal["DMY", "MDY", "YMD"] | None = None
    deadlines: dict[Literal["expires", "renews", "due", "ends"], list[str]] = Field(
        default_factory=dict
    )
    amounts: dict[Literal["quote", "invoice"], list[str]] = Field(default_factory=dict)
    examples: _Examples = Field(default_factory=_Examples)

    @field_validator("relations")
    @classmethod
    def _relation_labels(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        for label in value:
            if not label.replace("_", "").isalpha() or not label.islower():
                raise ValueError(f"relation label {label!r}: use lowercase words like sibling")
        return value


@dataclass(frozen=True)
class RulePack:
    language: str
    stopwords: frozenset[str]
    injection: tuple[str, ...]
    birthday_greetings: tuple[str, ...] = field(default=())
    birthday_exclude: tuple[str, ...] = field(default=())
    relations: dict[str, tuple[str, ...]] = field(default_factory=dict)
    date_order: str = "DMY"  # how to read 03/04/2026
    deadlines: dict[str, tuple[str, ...]] = field(default_factory=dict)  # key -> phrases
    amounts: dict[str, tuple[str, ...]] = field(default_factory=dict)


def read_pack(path: Path) -> PackFile:
    """Parse and validate one pack file. Raises ValueError with a readable message."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    words = raw.get("stopwords", []) if isinstance(raw, dict) else []
    if not all(isinstance(w, str) for w in words):
        raise ValueError(f"{path.name}: quote stopwords YAML reads as non-strings (on, no, ...)")
    return PackFile.model_validate(raw)


def user_dir() -> Path | None:
    value = os.environ.get(ENV_RULES)
    return Path(value).expanduser() if value else None


def _files() -> list[Path]:
    builtin = resources.files("graph_me.rules")
    found = sorted(Path(str(e)) for e in builtin.iterdir() if e.name.endswith(".yaml"))
    extra = user_dir()
    if extra and extra.is_dir():
        found += sorted(extra.glob("*.yaml"))
    return [f for f in found if not f.name.startswith("_")]


def _merge(a: PackFile, b: PackFile) -> PackFile:
    """``b`` adds to ``a`` (same language): lists are joined, ``date_order`` is replaced."""

    def join(x: list[str], y: list[str]) -> list[str]:
        return list(dict.fromkeys([*x, *y]))

    def joined(x: dict, y: dict) -> dict:
        return {k: join(x.get(k, []), y.get(k, [])) for k in dict.fromkeys([*x, *y])}

    return PackFile(
        language=a.language,
        stopwords=join(a.stopwords, b.stopwords),
        injection=join(a.injection, b.injection),
        birthday=_Birthday(
            greetings=join(a.birthday.greetings, b.birthday.greetings),
            exclude=join(a.birthday.exclude, b.birthday.exclude),
        ),  # fmt: skip
        relations=joined(a.relations, b.relations),
        date_order=b.date_order or a.date_order,
        deadlines=joined(a.deadlines, b.deadlines),
        amounts=joined(a.amounts, b.amounts),
    )


@cache
def load_all() -> dict[str, RulePack]:
    files: dict[str, PackFile] = {}
    for path in _files():
        try:
            pack = read_pack(path)
        except ValueError as exc:
            raise ValueError(f"rule pack {path}: {exc}") from exc
        files[pack.language] = _merge(files[pack.language], pack) if pack.language in files \
            else pack  # fmt: skip
    return {
        lang: RulePack(
            language=lang,
            stopwords=frozenset(w.casefold() for w in p.stopwords),
            injection=tuple(p.injection),
            birthday_greetings=tuple(p.birthday.greetings),
            birthday_exclude=tuple(p.birthday.exclude),
            relations={k: tuple(v) for k, v in p.relations.items()},
            date_order=p.date_order or "DMY",
            deadlines={k: tuple(v) for k, v in p.deadlines.items()},
            amounts={k: tuple(v) for k, v in p.amounts.items()},
        )
        for lang, p in files.items()
    }


def reload() -> None:
    """Forget every cache built from the packs (after adding or editing one)."""
    from graph_me.pipeline import sanitize
    from graph_me.pipeline.tier0 import extract, facts

    load_all.cache_clear()
    for cached in (facts._greetings, sanitize._phrases_by_first_word, extract._rules,
                   extract.rules_version):  # fmt: skip
        cached.cache_clear()


def all_stopwords() -> frozenset[str]:
    return frozenset().union(*(p.stopwords for p in load_all().values()))


def all_injection_phrases() -> tuple[str, ...]:
    return tuple(ph for p in load_all().values() for ph in p.injection)


def relation_words() -> dict[str, str]:
    """Folded relation word -> relation label, across all packs ("sister" -> "sibling")."""
    from graph_me.pipeline.sanitize import fold

    return {
        fold(word): relation
        for pack in load_all().values()
        for relation, words in pack.relations.items()
        for word in words
    }


def check_examples(pack: PackFile) -> list[str]:
    """Run a pack's examples against the loaded rules. Returns the failures, empty if none."""
    from graph_me.pipeline import sanitize
    from graph_me.pipeline.tier0 import extract, facts

    failures = []
    ex = pack.examples
    for text in ex.birthday.match:
        if facts.find_greeting(text) is None:
            failures.append(f"birthday: no greeting found in {text!r}")
    for text in ex.birthday.no_match:
        if facts.find_greeting(text) is not None:
            failures.append(f"birthday: a greeting was found in {text!r}")
    for text in ex.injection.match:
        if sanitize.sanitize(text).risk_score <= 0:
            failures.append(f"injection: not flagged: {text!r}")
    for text in ex.injection.no_match:
        if sanitize.sanitize(text).risk_score > 0:
            failures.append(f"injection: flagged by mistake: {text!r}")
    for case in ex.values:
        found = {k: v for k, v, _ in extract.extract(case.text)}
        if found != case.expect:
            failures.append(f"values: {case.text!r} gave {found}, expected {case.expect}")
    words = relation_words()
    for phrase, label in ex.relations.items():
        got = {words[w] for w in sanitize.fold(phrase).split() if w in words}
        if label not in got:
            failures.append(f"relations: {phrase!r} gave {sorted(got) or 'nothing'}, not {label}")
    return failures
