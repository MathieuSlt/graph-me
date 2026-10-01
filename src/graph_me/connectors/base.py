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


class Contact(BaseModel):
    """A raw address-book entry (vCard, CardDAV)."""

    name: str | None = None
    nicknames: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    birthday: str | None = None  # "MM-DD" or "YYYY-MM-DD"
    org: str | None = None
    note: str | None = None


class Attachment(BaseModel):
    content_hash: str | None = None  # sha256 of the bytes
    filename: str | None = None
    mime_type: str | None = None


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
    attachments: list[Attachment] = Field(default_factory=list)
    text: str | None = None  # None: the pipeline parses `path`
    path: Path | None = None  # local file to parse when text is None
    content_hash: str | None = None
    trust: Literal["self", "known", "untrusted"] = "untrusted"
    is_from_me: bool = False  # authored by the user (sent mail, own chat messages)
    # Contacts: raw address-book fields (names, emails, phones, birthday, nickname...).
    contact: Contact | None = None
    extra: dict = Field(default_factory=dict)


class SourceUnavailable(RuntimeError):
    """The source can't be read right now (folder missing, drive unplugged, database absent).

    Connectors raise it instead of listing nothing, so ``sync`` never mistakes an unreachable
    source for a source whose items were all deleted.
    """


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

    # Optional: ``excluded(external_id) -> bool`` says an id missing from ``list_ids`` still
    # exists but the config now leaves it out (noise globs, ignore marker). Sync forgets it
    # without counting it toward the mass-forget guard, which is about sources that went away.
