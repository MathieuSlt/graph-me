"""msgvault connector: mail and chats archived by msgvault (https://github.com/kenn-io/msgvault).

msgvault is the recommended way to bring mail (Gmail, IMAP, Microsoft 365, MBOX, PST...) and
chats (WhatsApp, iMessage, Slack, Discord...) into graph-me. graph-me only *reads* its SQLite
database, read-only; it never syncs, writes or deletes anything in msgvault or in the accounts.

Config keys (under ``sources.<name>``):

- ``db``: msgvault's database file, or its home folder (default ``~/.msgvault``)
- ``accounts``: optional list of account identifiers to include (emails, phone numbers)
- ``contacts``: include raw address-book entries synced into msgvault (default true)

Messages msgvault marks as deleted (locally or at the source) are treated as gone, so graph-me
forgets them: delete spam in msgvault and it disappears from graph-me too.

graph-me reads raw data only (messages, participants, attachments, address-book fields). It does
not import msgvault's own person merges or inferred facts: people and facts are built by graph-me
from the messages, the same way for every connector.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from graph_me.config import BlacklistConfig, SourceConfig
from graph_me.connectors.base import Attachment, Contact, Item, Party
from graph_me.connectors.vcard import contact_text
from graph_me.pipeline.parse import html_to_text
from graph_me.pipeline.tier0.identity import norm_bday

TESTED_WITH = "v0.20.0"
DEFAULT_HOME = Path("~/.msgvault")

# Columns graph-me relies on. Checked at startup so a msgvault schema change fails loudly.
REQUIRED_COLUMNS = {
    "sources": {"id", "source_type", "identifier"},
    "messages": {
        "id", "conversation_id", "source_id", "source_message_id", "message_type", "sent_at",
        "sender_id", "is_from_me", "subject", "snippet", "deleted_at", "deleted_from_source_at",
    },
    "message_bodies": {"message_id", "body_text", "body_html"},
    "message_recipients": {"message_id", "participant_id", "recipient_type"},
    "participants": {"id", "email_address", "phone_number", "display_name"},
    "conversations": {"id", "conversation_type", "title", "source_conversation_id"},
    "conversation_participants": {"conversation_id", "participant_id"},
    "attachments": {"message_id", "filename", "mime_type", "content_hash"},
}  # fmt: skip
_SKIPPED_ATTACHMENT_ROLES = ("avatar", "thumbnail", "preview", "sticker", "ui_asset")
_RAW_SOURCES = ("carddav_import", "vcard_import", "user")  # address-book data, not inferences
_BATCH = 500


class MsgvaultSchemaError(RuntimeError):
    pass


def _db_path(value: str | None) -> Path:
    path = Path(value).expanduser() if value else DEFAULT_HOME.expanduser()
    return path / "msgvault.db" if path.is_dir() or path.suffix != ".db" else path


def _in(ids: list) -> str:
    return ",".join("?" * len(ids))


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace(" ", "T", 1))
    except ValueError:
        return None


class MsgvaultConnector:
    type = "msgvault"

    def __init__(self, name: str, source: SourceConfig, blacklist: BlacklistConfig) -> None:
        extra = source.model_extra or {}
        self.name = name
        self.path = _db_path(extra.get("db"))
        self.accounts = [str(a) for a in extra.get("accounts", [])]
        self.contacts = bool(extra.get("contacts", True))
        self._ids: dict[str, int] = {}
        self._contacts: dict[str, tuple[Contact, str]] = {}
        self._known: set[int] | None = None
        self._conn: sqlite3.Connection | None = None

    # -- database -------------------------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            if not self.path.is_file():
                raise FileNotFoundError(
                    f"source {self.name!r}: msgvault database not found at {self.path}. "
                    "Install msgvault and sync an account, or set `db:` in config.yaml."
                )
            self._conn = sqlite3.connect(f"file:{quote(str(self.path))}?mode=ro", uri=True)
            self._conn.row_factory = sqlite3.Row
            self._check_schema()
        return self._conn

    def _columns(self, table: str) -> set[str]:
        return {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}

    def _check_schema(self) -> None:
        missing = []
        for table, needed in REQUIRED_COLUMNS.items():
            lacking = needed - self._columns(table)
            if lacking:
                missing.append(f"{table}: {', '.join(sorted(lacking))}")
        if missing:
            raise MsgvaultSchemaError(
                f"source {self.name!r}: unsupported msgvault database ({'; '.join(missing)}). "
                f"graph-me is tested with msgvault {TESTED_WITH}."
            )
        cols = self._columns("messages")
        self._version_expr = next(
            (c for c in ("content_changed_at", "last_modified", "archived_at") if c in cols),
            "sent_at",
        )
        self._has_role = "attachment_role" in self._columns("attachments")
        self._has_persons = bool(
            self._conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE name IN "
                "('persons', 'person_names', 'person_contact_points', 'person_dates')"
            ).fetchone()[0]
            == 4
        )

    def _account_filter(self) -> tuple[str, list]:
        if not self.accounts:
            return "", []
        return f" AND s.identifier IN ({_in(self.accounts)})", list(self.accounts)

    # -- list -----------------------------------------------------------------------------

    def list_ids(self) -> Iterator[tuple[str, str]]:
        self._ids.clear()
        where, params = self._account_filter()
        rows = self.conn.execute(
            f"""SELECT m.id, s.source_type, s.identifier, m.source_message_id,
                       COALESCE(m.{self._version_expr}, m.sent_at) AS version
                FROM messages m JOIN sources s ON s.id = m.source_id
                WHERE m.deleted_at IS NULL AND m.deleted_from_source_at IS NULL{where}""",
            params,
        )
        for r in rows:
            external_id = (
                f"msg/{r['source_type']}/{r['identifier']}/{r['source_message_id'] or r['id']}"
            )
            self._ids[external_id] = r["id"]
            yield external_id, str(r["version"])
        if self.contacts and self._has_persons:
            yield from self._list_contacts()

    def _list_contacts(self) -> Iterator[tuple[str, str]]:
        self._contacts.clear()
        raw = (
            f"source IN ({_in(list(_RAW_SOURCES))}) "
            "AND confidence IS NULL AND superseded_at IS NULL"
        )
        people: dict[int, dict] = {}

        def person(pid: int) -> dict:
            return people.setdefault(pid, {"names": [], "emails": [], "phones": [], "bday": None})

        for r in self.conn.execute(
            f"SELECT person_id, formatted, given_name, family_name, name_kind FROM person_names "
            f"WHERE {raw} ORDER BY person_id, ordinal",
            _RAW_SOURCES,
        ):
            name = r["formatted"] or " ".join(x for x in (r["given_name"], r["family_name"]) if x)
            if name:
                person(r["person_id"])["names"].append((r["name_kind"], name))
        for r in self.conn.execute(
            f"SELECT person_id, address_kind, original_value FROM person_contact_points "
            f"WHERE {raw} ORDER BY person_id, ordinal",
            _RAW_SOURCES,
        ):
            kind = (r["address_kind"] or "").lower()
            key = "emails" if "mail" in kind else "phones" if kind in ("tel", "phone") else None
            if key:
                person(r["person_id"])[key].append(r["original_value"])
        for r in self.conn.execute(
            f"SELECT person_id, date_year, date_month, date_day FROM person_dates "
            f"WHERE {raw} AND date_kind = 'birthday' AND date_month IS NOT NULL",
            _RAW_SOURCES,
        ):
            y = f"{r['date_year']:04d}-" if r["date_year"] else "--"
            person(r["person_id"])["bday"] = norm_bday(
                f"{y}{r['date_month']:02d}-{r['date_day'] or 1:02d}"
            )
        if not people:
            return
        uids = dict(
            self.conn.execute(
                f"SELECT id, vcard_uid FROM persons WHERE id IN ({_in(list(people))})", list(people)
            ).fetchall()
        )
        for pid, data in people.items():
            names = [n for kind, n in data["names"] if kind not in ("nickname",)]
            nicknames = [n for kind, n in data["names"] if kind == "nickname"]
            contact = Contact(
                name=names[0] if names else None,
                nicknames=nicknames,
                emails=data["emails"],
                phones=data["phones"],
                birthday=data["bday"],
            )
            version = hashlib.sha256(contact.model_dump_json().encode()).hexdigest()[:32]
            external_id = f"contact/{uids.get(pid, pid)}"
            self._contacts[external_id] = (contact, version)
            yield external_id, version

    # -- fetch ----------------------------------------------------------------------------

    def fetch(self, external_ids: Iterable[str]) -> Iterator[Item]:
        wanted = list(external_ids)
        for external_id in wanted:
            if external_id in self._contacts:
                contact, version = self._contacts[external_id]
                yield self._contact_item(external_id, contact, version)
        ids = [self._ids[e] for e in wanted if e in self._ids]
        by_id = {self._ids[e]: e for e in wanted if e in self._ids}
        for start in range(0, len(ids), _BATCH):
            yield from self._messages(ids[start : start + _BATCH], by_id)

    def _contact_item(self, external_id: str, contact: Contact, version: str) -> Item:
        return Item(
            external_id=external_id,
            version=version,
            kind="contact",
            title=contact.name or (contact.emails + contact.phones + ["(contact)"])[0],
            uri=f"msgvault:{external_id}",
            text=contact_text(contact),
            content_hash=version,
            trust="self",
            author=Party(
                name=contact.name,
                email=contact.emails[0] if contact.emails else None,
                phone=contact.phones[0] if contact.phones else None,
            ),
            contact=contact,
        )

    def _known_participants(self) -> set[int]:
        """Participants the user wrote to (recipients of sent mail, members of chats used)."""
        if self._known is None:
            self._known = {
                r[0]
                for r in self.conn.execute(
                    """SELECT mr.participant_id FROM message_recipients mr
                       JOIN messages m ON m.id = mr.message_id
                       WHERE m.is_from_me = 1 AND mr.recipient_type IN ('to', 'cc', 'bcc')
                       UNION
                       SELECT cp.participant_id FROM conversation_participants cp
                       JOIN messages m ON m.conversation_id = cp.conversation_id
                       WHERE m.is_from_me = 1"""
                )
            }
        return self._known

    def _messages(self, ids: list[int], by_id: dict[int, str]) -> Iterator[Item]:
        rows = self.conn.execute(
            f"""SELECT m.id, m.conversation_id, m.message_type, m.sent_at, m.sender_id,
                       m.is_from_me, m.subject, m.snippet,
                       COALESCE(m.{self._version_expr}, m.sent_at) AS version,
                       s.source_type, s.identifier,
                       c.conversation_type, c.title AS conv_title, c.source_conversation_id,
                       sp.email_address AS s_email, sp.phone_number AS s_phone,
                       sp.display_name AS s_name, b.body_text, b.body_html
                FROM messages m
                JOIN sources s ON s.id = m.source_id
                JOIN conversations c ON c.id = m.conversation_id
                LEFT JOIN participants sp ON sp.id = m.sender_id
                LEFT JOIN message_bodies b ON b.message_id = m.id
                WHERE m.id IN ({_in(ids)})""",
            ids,
        ).fetchall()

        recipients: dict[int, list[tuple[int, Party]]] = {}
        for r in self.conn.execute(
            f"""SELECT mr.message_id, mr.participant_id,
                       COALESCE(NULLIF(mr.display_name, ''), p.display_name) AS name,
                       p.email_address, p.phone_number
                FROM message_recipients mr JOIN participants p ON p.id = mr.participant_id
                WHERE mr.message_id IN ({_in(ids)}) AND mr.recipient_type IN ('to', 'cc', 'bcc')""",
            ids,
        ):
            recipients.setdefault(r["message_id"], []).append(
                (
                    r["participant_id"],
                    Party(name=r["name"], email=r["email_address"], phone=r["phone_number"]),
                )
            )

        conv_ids = sorted({r["conversation_id"] for r in rows})
        members: dict[int, list[tuple[int, Party]]] = {}
        for r in self.conn.execute(
            f"""SELECT cp.conversation_id, p.id, p.display_name, p.email_address, p.phone_number
                FROM conversation_participants cp JOIN participants p ON p.id = cp.participant_id
                WHERE cp.conversation_id IN ({_in(conv_ids)})""",
            conv_ids,
        ):
            members.setdefault(r["conversation_id"], []).append(
                (
                    r["id"],
                    Party(
                        name=r["display_name"], email=r["email_address"], phone=r["phone_number"]
                    ),
                )
            )

        role_filter = (
            f" AND attachment_role NOT IN ({_in(list(_SKIPPED_ATTACHMENT_ROLES))})"
            if self._has_role
            else ""
        )
        attachments: dict[int, list[Attachment]] = {}
        for r in self.conn.execute(
            f"""SELECT message_id, filename, mime_type, content_hash FROM attachments
                WHERE message_id IN ({_in(ids)}){role_filter}""",
            [*ids, *(_SKIPPED_ATTACHMENT_ROLES if self._has_role else ())],
        ):
            attachments.setdefault(r["message_id"], []).append(
                Attachment(
                    content_hash=r["content_hash"], filename=r["filename"], mime_type=r["mime_type"]
                )
            )

        known = self._known_participants()
        for r in rows:
            to = recipients.get(r["id"]) or [
                (pid, party) for pid, party in members.get(r["conversation_id"], [])
                if pid != r["sender_id"]
            ]  # fmt: skip
            text = r["body_text"] or (html_to_text(r["body_html"]) if r["body_html"] else "")
            text = text or r["snippet"] or ""
            is_email = r["message_type"] == "email"
            author = Party(name=r["s_name"] or None, email=r["s_email"], phone=r["s_phone"])
            yield Item(
                external_id=by_id[r["id"]],
                version=str(r["version"]),
                kind="email" if is_email else "message",
                title=self._title(r, text, author, [p for _, p in to]),
                uri=f"msgvault:message/{r['id']}",
                thread_id=f"{r['source_type']}/{r['identifier']}/{r['source_conversation_id']}",
                created_at=_parse_ts(r["sent_at"]),
                modified_at=_parse_ts(r["sent_at"]),
                author=author,
                recipients=[p for _, p in to],
                attachments=attachments.get(r["id"], []),
                text=text,
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
                is_from_me=bool(r["is_from_me"]),
                trust="self"
                if r["is_from_me"]
                else "known"
                if r["sender_id"] in known
                else "untrusted",
                extra={
                    "msgvault_id": r["id"],
                    "source_type": r["source_type"],
                    "account": r["identifier"],
                    "message_type": r["message_type"],
                    "conversation_type": r["conversation_type"],
                },
            )

    @staticmethod
    def _title(r: sqlite3.Row, text: str, author: Party, to: list[Party]) -> str:
        if r["subject"]:
            return r["subject"]
        other = to[0] if r["is_from_me"] and to else author
        who = r["conv_title"] or other.name or other.phone or other.email or r["source_type"]
        first = " ".join(text.split())[:60]
        return f"{who}: {first}" if first else who
