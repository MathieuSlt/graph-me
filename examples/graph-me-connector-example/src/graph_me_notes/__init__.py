"""An example graph-me connector: notes kept in a JSON Lines file, one note per line.

    {"id": "n1", "title": "Boiler", "text": "Serviced in May", "updated": "2026-05-02T10:00:00Z",
     "author": "sam@example.org"}

Config::

    sources:
      notes:
        type: notes-jsonl
        path: ~/notes.jsonl

A connector lists what exists (``list_ids``) and fetches what changed (``fetch``). graph-me
computes the differences and forgets what is gone: a connector never deletes anything.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from graph_me.config import BlacklistConfig, SourceConfig
from graph_me.connectors.base import Item, Party, SourceUnavailable


class NotesConnector:
    type = "notes-jsonl"

    def __init__(self, name: str, source: SourceConfig, blacklist: BlacklistConfig) -> None:
        extra = source.model_extra or {}
        if "path" not in extra:
            raise ValueError(f"source {name!r}: set `path:` to your notes file")
        self.name = name
        self.path = Path(extra["path"]).expanduser()
        self._notes: dict[str, dict] = {}

    def list_ids(self) -> Iterator[tuple[str, str]]:
        # Rule 3: an unreachable source raises instead of looking empty.
        if not self.path.is_file():
            raise SourceUnavailable(f"source {self.name!r}: {self.path} not found")
        self._notes.clear()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            note = json.loads(line)
            # Rule 1: the version changes only when the note does.
            version = hashlib.sha256(json.dumps(note, sort_keys=True).encode()).hexdigest()[:16]
            self._notes[str(note["id"])] = {**note, "_version": version}
            yield str(note["id"]), version

    def fetch(self, external_ids: Iterable[str]) -> Iterator[Item]:
        for external_id in external_ids:
            note = self._notes.get(external_id)
            if note is None:  # Rule 2: skip what is gone
                continue
            updated = note.get("updated")
            yield Item(
                external_id=external_id,
                version=note["_version"],
                kind="file",
                title=note.get("title"),
                uri=f"{self.path}#{external_id}",
                text=note.get("text", ""),
                # Rule 4: aware dates.
                modified_at=datetime.fromisoformat(updated).astimezone(UTC) if updated else None,
                author=Party(email=note["author"]) if note.get("author") else None,
                trust="self",
            )
