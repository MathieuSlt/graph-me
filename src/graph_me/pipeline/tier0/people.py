"""Tier 0 people, built from any connector's items (mail, chats, contacts). No model.

- Every author and recipient with an email, phone or handle becomes a ``person`` entity whose
  aliases are those identifiers. Entities sharing an identifier are merged (resolve.py).
- The user's own identities (sent messages, ``people.me`` in config) all carry the alias
  ``role:me``, so they merge into one "me" entity.
- ``author -wrote_to-> recipient`` relations are cited by each message.
- Contact cards also give facts (birthday, nickname, organization) through facts.py.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from graph_me.config import PeopleConfig
from graph_me.connectors.base import Item, Party
from graph_me.pipeline import resolve
from graph_me.pipeline.tier0 import facts
from graph_me.pipeline.tier0.entities import entity_id, mention, relate
from graph_me.pipeline.tier0.identity import norm_email, norm_name, norm_phone

ME = ("role", "me")


@dataclass
class PeopleContext:
    country_code: str | None = None
    me: set[tuple[str, str]] = field(default_factory=set)  # normalized (kind, value)

    @classmethod
    def from_config(cls, cfg: PeopleConfig) -> PeopleContext:
        ctx = cls(country_code=cfg.phone_country_code)
        for value in cfg.me:
            ctx.me.update(ctx.keys(Party(email=value, phone=value)))
        return ctx

    def keys(self, party: Party | None) -> list[tuple[str, str]]:
        if party is None:
            return []
        keys = []
        if email := norm_email(party.email):
            keys.append(("email", email))
        if phone := norm_phone(party.phone, self.country_code):
            keys.append(("phone", phone))
        if party.handle:
            keys.append(("handle", party.handle.strip().casefold()))
        return keys


def person_for(
    conn: sqlite3.Connection,
    keys: list[tuple[str, str]],
    name: str | None,
    *,
    authoritative_name: bool = False,
) -> str | None:
    """The person entity owning ``keys``, created or merged as needed."""
    if not keys:
        return None
    owners: list[str] = []
    for kind, value in keys:
        row = conn.execute(
            "SELECT entity_id FROM aliases WHERE kind = ? AND value = ?", (kind, value)
        ).fetchone()
        if row and row[0] not in owners:
            owners.append(row[0])
    if owners:
        keep = owners[0]
        for other in owners[1:]:
            resolve.merge(conn, keep, other)
    else:
        kind, value = keys[0]
        keep = entity_id("person", f"{kind}:{value}")
        conn.execute(
            "INSERT OR IGNORE INTO entities(id, kind, name, tier) VALUES (?, 'person', NULL, 0)",
            (keep,),
        )
    conn.executemany(
        "INSERT OR IGNORE INTO aliases(entity_id, kind, value) VALUES (?, ?, ?)",
        [(keep, k, v) for k, v in keys],
    )
    clean = norm_name(name)
    if clean:
        if authoritative_name:
            conn.execute("UPDATE entities SET name = ? WHERE id = ?", (clean, keep))
        else:
            conn.execute(
                "UPDATE entities SET name = ? WHERE id = ? AND name IS NULL", (clean, keep)
            )
    return keep


def item_people(conn: sqlite3.Connection, item_id: str, item: Item, ctx: PeopleContext) -> None:
    if item.kind == "contact" and item.contact:
        _contact(conn, item_id, item, ctx)
        return

    author_keys = ctx.keys(item.author)
    is_me = item.is_from_me or any(k in ctx.me for k in author_keys)
    if is_me:
        author_keys = [*author_keys, ME]
    author = person_for(conn, author_keys, item.author.name if item.author else None)
    if author:
        mention(conn, author, item_id, "author")

    recipients = []
    for party in item.recipients:
        keys = ctx.keys(party)
        if any(k in ctx.me for k in keys):
            keys.append(ME)
        pid = person_for(conn, keys, party.name)
        if pid and pid != author:
            mention(conn, pid, item_id, "recipient")
            recipients.append(pid)
    if author:
        for pid in dict.fromkeys(recipients):
            relate(conn, author, pid, "wrote_to", item_id, "tier0:message")

    facts.greeting_facts(conn, item_id, item, recipients)


def _contact(conn: sqlite3.Connection, item_id: str, item: Item, ctx: PeopleContext) -> None:
    c = item.contact
    keys: list[tuple[str, str]] = []
    for email in c.emails:
        if e := norm_email(email):
            keys.append(("email", e))
    for phone in c.phones:
        if p := norm_phone(phone, ctx.country_code):
            keys.append(("phone", p))
    if any(k in ctx.me for k in keys):
        keys.append(ME)
    keys = list(dict.fromkeys(keys))
    if not keys:
        # A card with a name only still describes someone: key it by the card itself.
        keys = [("contact", item.external_id)]
    pid = person_for(conn, keys, c.name, authoritative_name=True)
    mention(conn, pid, item_id, "contact")
    facts.contact_facts(conn, item_id, pid, c)
