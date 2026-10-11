"""Your corrections: ``overrides.yaml``, next to ``config.yaml``.

graph-me reads it as a built-in source (``_overrides``), so every correction is cited like any
other data and forgotten when you remove it:

- ``people``: one entry per person, keyed by an email or phone number (``who``). ``same_as``
  links more identifiers to them, ``relation`` says how they relate to you ("sibling"),
  ``name`` and ``facts`` set what graph-me should know.
- ``not_same``: identifiers that belong to different people, even if a contact card lists them
  together. Applied on the next full ``scan`` or ``sync``.
- ``reject``: facts that are wrong ("this birthday is not hers"), dropped on every run.

The ``graph-me link``, ``unlink``, ``relate`` and ``fact --set/--reject`` commands edit this
file; you can also edit it by hand. Nothing here writes to a source.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from graph_me import rules
from graph_me.pipeline.tier0.identity import norm_bday

SOURCE = "_overrides"  # the built-in source reading overrides.yaml
FACT_KEYS = ("birthday", "birth_year", "anniversary", "nickname", "organization", "job_title",
             "address")  # fmt: skip
HEADER = (
    "# graph-me: your corrections. Edited by `graph-me link`, `unlink`, `relate` and\n"
    "# `fact --set/--reject`; you can edit it by hand too. Run `graph-me sync` to apply.\n"
)


def relations() -> tuple[str, ...]:
    """Relation labels from the rule packs ("sibling", "landlord"...)."""
    return tuple(sorted({r for pack in rules.load_all().values() for r in pack.relations}))


def _same(a: str, b: str) -> bool:
    return "".join(a.split()).casefold() == "".join(b.split()).casefold()


class PersonOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    who: str  # an email or phone number, never a name: names are ambiguous
    same_as: list[str] = Field(default_factory=list)
    name: str | None = None
    relation: str | None = None
    facts: dict[str, str] = Field(default_factory=dict)

    @property
    def identifiers(self) -> list[str]:
        return [self.who, *self.same_as]

    def has(self, identifier: str) -> bool:
        return any(_same(identifier, i) for i in self.identifiers)

    @field_validator("relation")
    @classmethod
    def _known_relation(cls, value: str | None) -> str | None:
        if value is not None and value not in relations():
            raise ValueError(f"unknown relation {value!r} (known: {', '.join(relations())})")
        return value

    @field_validator("facts")
    @classmethod
    def _known_facts(cls, value: dict[str, str]) -> dict[str, str]:
        for key, fact in value.items():
            check_fact(key, fact)
        return value


class Rejection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    who: str
    fact: str
    value: str


class Overrides(BaseModel):
    model_config = ConfigDict(extra="forbid")

    people: list[PersonOverride] = Field(default_factory=list)
    not_same: list[list[str]] = Field(default_factory=list)
    reject: list[Rejection] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> Overrides:
        for n, person in enumerate(self.people):
            for other in self.people[n + 1 :]:
                if any(other.has(i) for i in person.identifiers):
                    raise ValueError(f"{person.who} and {other.who} are listed as two people "
                                     "but share an identifier: merge the two entries")  # fmt: skip
        for group in self.not_same:
            if len(group) < 2:
                raise ValueError(f"not_same {group}: list at least two identifiers")
            for person in self.people:
                linked = [i for i in group if person.has(i)]
                if len(linked) > 1:
                    raise ValueError(
                        f"{', '.join(linked)} are both linked (same_as) and kept apart (not_same)"
                    )
        return self

    def person(self, identifier: str) -> PersonOverride | None:
        return next((p for p in self.people if p.has(identifier)), None)

    def entry(self, identifier: str) -> PersonOverride:
        """The entry for ``identifier``, created if missing."""
        found = self.person(identifier)
        if found is None:
            found = PersonOverride(who=identifier)
            self.people.append(found)
        return found


def check_fact(key: str, value: str) -> str:
    """The value as stored, or ValueError for an unknown key or a malformed value."""
    if key == "relation_to_user":
        if value not in relations():
            raise ValueError(f"unknown relation {value!r} (known: {', '.join(relations())})")
        return value
    if key not in FACT_KEYS:
        raise ValueError(f"unknown fact {key!r} (known: {', '.join(FACT_KEYS)})")
    if key in ("birthday", "anniversary"):
        value = value.strip()
        day = norm_bday("--" + value if len(value) == 5 else value)  # MM-DD or YYYY-MM-DD
        if day is None:
            raise ValueError(f"{key} {value!r}: use MM-DD or YYYY-MM-DD")
        return day[-5:]
    if key == "birth_year" and not (value.isdigit() and len(value) == 4):
        raise ValueError(f"birth_year {value!r}: use four digits, e.g. 1990")
    return value.strip()


def load(path: Path) -> Overrides:
    """Read ``overrides.yaml``; a missing file means no corrections."""
    if not path.is_file():
        return Overrides()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Overrides.model_validate(raw)


def save(path: Path, overrides: Overrides) -> None:
    """Write ``overrides.yaml`` (private to you), keeping the previous version as ``.bak``."""
    Overrides.model_validate(overrides.model_dump())  # never write a file `load` would refuse
    data = overrides.model_dump(exclude_defaults=True)
    text = HEADER + (yaml.safe_dump(data, sort_keys=False, allow_unicode=True) if data else "")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(".yaml.bak"))
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


# -- edits used by the CLI ------------------------------------------------------------------


def link(overrides: Overrides, identifiers: list[str]) -> PersonOverride:
    """Say these identifiers are one person."""
    owners = [p for p in overrides.people if any(p.has(i) for i in identifiers)]
    keep = owners[0] if owners else overrides.entry(identifiers[0])
    for other in owners[1:]:  # two entries now describe one person: fold them
        keep.same_as += [i for i in other.identifiers if not keep.has(i)]
        keep.name = keep.name or other.name
        keep.relation = keep.relation or other.relation
        keep.facts = {**other.facts, **keep.facts}
        overrides.people.remove(other)
    keep.same_as += [i for i in identifiers if not keep.has(i)]
    # Linking undoes an earlier "not the same" between these identifiers.
    overrides.not_same = [g for g in overrides.not_same if sum(keep.has(i) for i in g) < 2]
    return keep


def unlink(overrides: Overrides, identifiers: list[str]) -> None:
    """Say these identifiers belong to different people."""
    for person in overrides.people:
        linked = [i for i in identifiers if person.has(i)]
        for identifier in linked[1:]:  # keep `who`, drop the conflicting links
            person.same_as = [s for s in person.same_as if not _same(s, identifier)]
    group = list(identifiers)
    if not any(len(g) == len(group) and all(any(_same(a, b) for b in g) for a in group)
               for g in overrides.not_same):  # fmt: skip
        overrides.not_same.append(group)


def relate(overrides: Overrides, who: str, relation: str | None) -> None:
    overrides.entry(who).relation = relation
    if relation:
        _unreject(overrides, who, "relation_to_user", relation)


def set_fact(overrides: Overrides, who: str, key: str, value: str) -> str:
    stored = check_fact(key, value)
    if key == "relation_to_user":
        relate(overrides, who, stored)
        return stored
    entry = overrides.entry(who)
    entry.facts[key] = stored
    if key == "birthday" and len(value.strip()) == 10:  # YYYY-MM-DD also gives the year
        entry.facts["birth_year"] = value.strip()[:4]
    value = stored
    _unreject(overrides, who, key, value)
    return value


def reject(overrides: Overrides, who: str, key: str, value: str) -> str:
    value = check_fact(key, value)
    person = overrides.person(who)
    if person and person.facts.get(key) == value:
        del person.facts[key]
    if person and key == "relation_to_user" and person.relation == value:
        person.relation = None
    if not any(_same(r.who, who) and r.fact == key and r.value == value for r in overrides.reject):
        overrides.reject.append(Rejection(who=who, fact=key, value=value))
    return value


def _unreject(overrides: Overrides, who: str, key: str, value: str) -> None:
    overrides.reject = [
        r for r in overrides.reject
        if not (_same(r.who, who) and r.fact == key and r.value == value)
    ]  # fmt: skip
