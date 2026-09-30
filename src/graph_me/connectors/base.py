"""The connector contract: list what exists, fetch what changed. The core computes the diff."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from graph_me.config import BlacklistConfig, SourceConfig

ItemKind = Literal["file", "email", "message", "contact", "thread"]


class Party(BaseModel):
    """A person or account taking part in an item (author, recipient)."""

    name: str | None = None
    email: str | None = None
    phone: str | None = None
    handle: str | None = None


class Item(BaseModel):
    external_id: str
    version: str  # opaque; a different version means "changed"
    kind: ItemKind
    title: str | None = None
    uri: str | None = None  # path or deep link
    thread_id: str | None = None
    created_at: datetime | None = None
    modified_at: datetime | None = None
    author: Party | None = None
    recipients: list[Party] = Field(default_factory=list)
    attachments: list[str] = Field(default_factory=list)  # external ids or content hashes
    text: str | None = None  # None: the pipeline parses `path`
    path: Path | None = None  # local file to parse when text is None
    content_hash: str | None = None
    trust: Literal["self", "known", "untrusted"] = "untrusted"
    extra: dict = Field(default_factory=dict)


@runtime_checkable
class Connector(Protocol):
    type: str

    def __init__(self, name: str, source: SourceConfig, blacklist: BlacklistConfig) -> None: ...

    def list_ids(self) -> Iterator[tuple[str, str]]:
        """Yield ``(external_id, version)`` for everything that currently exists."""
        ...

    def fetch(self, external_ids: Iterable[str]) -> Iterator[Item]:
        """Yield full items for the given ids. Ids that vanished meanwhile are skipped."""
        ...
