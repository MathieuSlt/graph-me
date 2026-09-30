"""Tier 0 facts from rules (no model). Every fact is cited by the item it came from.

- Contact cards: birthday (and birth year), nicknames, organization. Confidence 0.95.
- Birthday greetings: a message to exactly one person whose opening words contain a greeting
  from a rule pack ("joyeux anniv", "happy birthday"...) suggests that person's birthday on the
  message's local date. Confidence 0.5, rising by 0.15 for each extra year the same date is seen
  (max 0.9). Messages with an "exclude" word ("en retard", "belated") are ignored.
"""

from __future__ import annotations

import re
import sqlite3
from functools import cache

from graph_me import rules
from graph_me.connectors.base import Contact, Item
from graph_me.pipeline.sanitize import fold

CONTACT_CONFIDENCE = 0.95
GREETING_CONFIDENCE = 0.5
GREETING_YEAR_BONUS = 0.15
GREETING_MAX = 0.9
GREETING_WINDOW_WORDS = 40  # greetings open a message; this also skips quoted email history
_WORD = re.compile(r"\w+")


def upsert_fact(
    conn: sqlite3.Connection,
    entity: str,
    key: str,
    value: str,
    confidence: float,
    item_id: str,
    method: str,
    tier: int = 0,
) -> int:
    fact_id = conn.execute(
        """INSERT INTO facts(entity_id, key, value, confidence, tier) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(entity_id, key, value)
           DO UPDATE SET confidence = max(confidence, excluded.confidence)
           RETURNING id""",
        (entity, key, value, confidence, tier),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO evidence(fact_id, item_id, method) VALUES (?, ?, ?)",
        (fact_id, item_id, method),
    )
    return fact_id


def contact_facts(conn: sqlite3.Connection, item_id: str, pid: str, c: Contact) -> None:
    if c.birthday:
        month_day = c.birthday[-5:]
        upsert_fact(conn, pid, "birthday", month_day, CONTACT_CONFIDENCE, item_id, "contact:bday")
        if len(c.birthday) == 10:
            upsert_fact(
                conn, pid, "birth_year", c.birthday[:4], CONTACT_CONFIDENCE, item_id, "contact:bday"
            )
    for nickname in c.nicknames:
        upsert_fact(
            conn, pid, "nickname", nickname, CONTACT_CONFIDENCE, item_id, "contact:nickname"
        )
    if c.org:
        upsert_fact(conn, pid, "organization", c.org, CONTACT_CONFIDENCE, item_id, "contact:org")


@cache
def _greetings() -> tuple[list[tuple[str, tuple[str, ...]]], frozenset[str]]:
    patterns, exclude = [], set()
    for lang, pack in rules.load_all().items():
        patterns += [(lang, tuple(_WORD.findall(fold(g)))) for g in pack.birthday_greetings]
        exclude.update(fold(w) for w in pack.birthday_exclude)
    return patterns, frozenset(exclude)


def find_greeting(text: str) -> str | None:
    """Language of the birthday greeting opening ``text``, or None."""
    words = _WORD.findall(fold(text))[:GREETING_WINDOW_WORDS]
    patterns, exclude = _greetings()
    if exclude.intersection(words):
        return None
    for lang, phrase in patterns:
        n = len(phrase)
        if any(tuple(words[i : i + n]) == phrase for i in range(len(words) - n + 1)):
            return lang
    return None


def greeting_facts(
    conn: sqlite3.Connection, item_id: str, item: Item, recipients: list[str]
) -> None:
    targets = set(recipients)
    if item.kind not in ("email", "message") or not item.text or not item.created_at:
        return
    if len(targets) != 1:  # a greeting to a group can't tell whose birthday it is
        return
    lang = find_greeting(item.text)
    if not lang:
        return
    local = item.created_at.astimezone()  # the day as the user lived it, not UTC
    upsert_fact(
        conn,
        targets.pop(),
        "birthday",
        f"{local.month:02d}-{local.day:02d}",
        GREETING_CONFIDENCE,
        item_id,
        f"rule:{lang}.birthday_greeting",
    )


def recompute_confidence(conn: sqlite3.Connection) -> None:
    """Birthday confidence from all its evidence: contact card, or greetings over several years."""
    rows = conn.execute(
        """SELECT f.id,
                  max(e.method = 'contact:bday') AS from_contact,
                  count(DISTINCT CASE WHEN e.method LIKE 'rule:%'
                                      THEN substr(i.created_at, 1, 4) END) AS years
           FROM facts f
           JOIN evidence e ON e.fact_id = f.id
           JOIN items i ON i.id = e.item_id
           WHERE f.key = 'birthday'
           GROUP BY f.id"""
    ).fetchall()
    for fact_id, from_contact, years in rows:
        if from_contact:
            confidence = CONTACT_CONFIDENCE
        else:
            confidence = min(
                GREETING_MAX, GREETING_CONFIDENCE + GREETING_YEAR_BONUS * max(0, years - 1)
            )
        conn.execute("UPDATE facts SET confidence = ? WHERE id = ?", (confidence, fact_id))
