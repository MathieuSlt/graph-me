"""The built-in ``_overrides`` source: your corrections in ``overrides.yaml`` (see overrides.py).

Each ``people`` entry becomes one ``contact`` item listing its identifiers, so linking reuses the
usual merge on a shared email or phone, and every relation or fact you set is cited by it.
Removing an entry (or the file) forgets what it said on the next ``sync``.

Config keys: ``path``: the overrides.yaml file. graph-me adds this source itself.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import yaml

from graph_me import overrides
from graph_me.config import BlacklistConfig, SourceConfig
from graph_me.connectors.base import Contact, Item, SourceUnavailable


def _is_email(identifier: str) -> bool:
    return "@" in identifier


class OverridesConnector:
    type = "overrides"

    def __init__(self, name: str, source: SourceConfig, blacklist: BlacklistConfig) -> None:
        self.name = name
        self.path = Path((source.model_extra or {})["path"]).expanduser()
        self._people: dict[str, overrides.PersonOverride] = {}

    def _load(self) -> overrides.Overrides:
        try:
            return overrides.load(self.path)
        except (OSError, ValueError, yaml.YAMLError) as exc:  # ValueError: pydantic too
            raise SourceUnavailable(
                f"{self.path} has errors, so your corrections were left as they were:\n{exc}"
            ) from exc

    def list_ids(self) -> Iterator[tuple[str, str]]:
        self._people.clear()
        for person in self._load().people:
            external_id = "person:" + "".join(person.who.split()).casefold()
            self._people[external_id] = person
            data = json.dumps(person.model_dump(), sort_keys=True)
            yield external_id, hashlib.sha256(data.encode()).hexdigest()[:32]

    def fetch(self, external_ids: Iterable[str]) -> Iterator[Item]:
        modified = (
            datetime.fromtimestamp(self.path.stat().st_mtime, UTC) if self.path.exists() else None
        )
        for external_id in external_ids:
            person = self._people.get(external_id)
            if person is None:
                continue
            ids = person.identifiers
            told = [f"{k.replace('_', ' ')}: {v}" for k, v in person.facts.items()]
            if person.relation:
                told.insert(0, f"relation to you: {person.relation}")
            data = json.dumps(person.model_dump(), sort_keys=True)
            yield Item(
                external_id=external_id,
                version=hashlib.sha256(data.encode()).hexdigest()[:32],
                kind="contact",
                title=f"Your correction: {person.name or person.who}",
                uri=str(self.path),
                text="\n".join([person.name or "", *ids, *told]).strip(),
                modified_at=modified,
                trust="self",
                contact=Contact(
                    name=person.name,
                    emails=[i for i in ids if _is_email(i)],
                    phones=[i for i in ids if not _is_email(i)],
                ),
                extra={"override": {"relation": person.relation, "facts": person.facts}},
            )
