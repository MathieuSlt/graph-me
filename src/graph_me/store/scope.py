"""Profiles: limit one connection to some sources, so an agent only sees what it was given.

``limit(conn, sources, kinds)`` creates TEMP views named like the tables they hide. SQLite looks
names up in ``temp`` before ``main``, so every query in query/engine.py and query/graph.py sees
only the profile's data without being changed:

- ``items``: the profile's sources (and kinds); ``chunks``, ``mentions``, ``evidence`` and
  attachments: of those items only;
- ``facts`` and ``relations``: those cited by a visible item;
- ``entities``: those a visible item mentions; ``aliases``: their emails, phone numbers and
  handles that a visible item showed (``item_identifiers``), so a work profile doesn't learn a
  colleague's personal number from a family chat;
- ``sources``: the profile's; ``communities``: unlabelled (labels name people).

Writes must name ``main.`` explicitly (only the query log writes). A scoped connection is for
reading: the scan pipeline never uses one.
"""

from __future__ import annotations

import sqlite3

_SHOWN = "('email', 'phone', 'handle')"


def _quoted(values: list[str]) -> str:
    return ",".join("'" + v.replace("'", "''") + "'" for v in values)


def limit(conn: sqlite3.Connection, sources: list[str], kinds: list[str] | None = None) -> None:
    where = f"source_id IN ({_quoted(sources)})"
    if kinds:
        where += f" AND kind IN ({_quoted(kinds)})"
    by_item = "item_id IN (SELECT id FROM temp.items)"
    views = {
        "items": f"SELECT * FROM main.items WHERE {where}",
        "chunks": f"SELECT * FROM main.chunks WHERE {by_item}",
        "mentions": f"SELECT * FROM main.mentions WHERE {by_item}",
        "evidence": f"SELECT * FROM main.evidence WHERE {by_item}",
        "item_attachments": f"SELECT * FROM main.item_attachments WHERE {by_item}",
        "item_identifiers": f"SELECT * FROM main.item_identifiers WHERE {by_item}",
        "facts": "SELECT * FROM main.facts WHERE id IN "
        "(SELECT fact_id FROM temp.evidence WHERE fact_id IS NOT NULL)",
        "relations": "SELECT * FROM main.relations WHERE id IN "
        "(SELECT relation_id FROM temp.evidence WHERE relation_id IS NOT NULL)",
        "entities": "SELECT * FROM main.entities WHERE id IN (SELECT entity_id FROM temp.mentions)",
        "aliases": "SELECT * FROM main.aliases a WHERE a.entity_id IN (SELECT id FROM "
        f"temp.entities) AND (a.kind NOT IN {_SHOWN} OR EXISTS (SELECT 1 FROM "
        "temp.item_identifiers s WHERE s.kind = a.kind AND s.value = a.value))",
        "community_members": "SELECT * FROM main.community_members WHERE entity_id IN "
        "(SELECT id FROM temp.entities)",
        "communities": "SELECT id, NULL AS label, tier FROM main.communities",
        "sources": f"SELECT * FROM main.sources WHERE id IN ({_quoted(sources)})",
    }
    for name, body in views.items():
        conn.execute(f"CREATE TEMP VIEW {name} AS {body}")
