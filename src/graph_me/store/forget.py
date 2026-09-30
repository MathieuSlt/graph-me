"""Forgetting: remove items and everything that was learned only from them.

Deleting an item cascades to its chunks, mentions and evidence. ``cleanup_orphans`` then removes
what no longer has a source:

- facts and relations with no evidence left;
- entities with no mentions left (an entity exists only because some item mentions it; its
  aliases go with it).

Facts, relations and entities still backed by another item survive.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path

_BATCH = 500


def forget_items(conn: sqlite3.Connection, item_ids: Iterable[str]) -> int:
    """Delete items and their orphans. Returns the number of items deleted."""
    ids = list(dict.fromkeys(item_ids))
    deleted = 0
    for start in range(0, len(ids), _BATCH):
        batch = ids[start : start + _BATCH]
        marks = ",".join("?" * len(batch))
        deleted += conn.execute(f"DELETE FROM items WHERE id IN ({marks})", batch).rowcount
    if deleted:
        cleanup_orphans(conn)
    return deleted


def cleanup_orphans(conn: sqlite3.Connection) -> dict[str, int]:
    removed = {
        "facts": conn.execute(
            "DELETE FROM facts WHERE id NOT IN "
            "(SELECT fact_id FROM evidence WHERE fact_id IS NOT NULL)"
        ).rowcount,
        "relations": conn.execute(
            "DELETE FROM relations WHERE id NOT IN "
            "(SELECT relation_id FROM evidence WHERE relation_id IS NOT NULL)"
        ).rowcount,
        "entities": conn.execute(
            "DELETE FROM entities WHERE id NOT IN (SELECT entity_id FROM mentions)"
        ).rowcount,
    }
    conn.execute(
        "DELETE FROM communities WHERE id NOT IN (SELECT community_id FROM community_members)"
    )
    return removed


def _like_prefix(path: str) -> str:
    escaped = path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped.rstrip("/") + "/%"


def blacklisted_item_ids(
    conn: sqlite3.Connection, paths: Iterable[str], patterns: Iterable[str]
) -> set[str]:
    """Items already stored that the blacklist now covers (by path, or by text pattern)."""
    ids: set[str] = set()
    for raw in paths:
        path = str(Path(raw).expanduser().resolve())
        ids.update(
            row[0]
            for row in conn.execute(
                "SELECT id FROM items WHERE uri = ? OR uri LIKE ? ESCAPE '\\'",
                (path, _like_prefix(path)),
            )
        )
    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
    if compiled:
        conn.create_function(
            "gm_blacklisted",
            1,
            lambda text: int(bool(text) and any(rx.search(text) for rx in compiled)),
            deterministic=True,
        )
        ids.update(
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT item_id FROM chunks WHERE gm_blacklisted(text) "
                "UNION SELECT id FROM items WHERE gm_blacklisted(title)"
            )
        )
    return ids
