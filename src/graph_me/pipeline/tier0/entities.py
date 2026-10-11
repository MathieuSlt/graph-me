"""Tier 0 entities, built deterministically from metadata (no model).

Files: each file is a ``document`` entity; its parent folder (below the source root) is a
``project`` entity; ``document -in_folder-> project`` is a relation cited by the file item.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from graph_me.connectors.base import Item


def entity_id(kind: str, key: str) -> str:
    return "e_" + hashlib.sha256(f"{kind}\0{key}".encode()).hexdigest()[:24]


def upsert_entity(
    conn: sqlite3.Connection, kind: str, name: str | None, alias_kind: str, alias_value: str
) -> str:
    """Return the entity owning ``(alias_kind, alias_value)``, creating it if needed."""
    row = conn.execute(
        "SELECT entity_id FROM aliases WHERE kind = ? AND value = ?", (alias_kind, alias_value)
    ).fetchone()
    if row:
        return row[0]
    eid = entity_id(kind, f"{alias_kind}:{alias_value}")
    conn.execute(
        "INSERT OR IGNORE INTO entities(id, kind, name, tier) VALUES (?, ?, ?, 0)",
        (eid, kind, name),
    )
    conn.execute(
        "INSERT INTO aliases(entity_id, kind, value) VALUES (?, ?, ?)",
        (eid, alias_kind, alias_value),
    )
    return eid


def mention(conn: sqlite3.Connection, eid: str, item_id: str, role: str, tier: int = 0) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO mentions(entity_id, item_id, role, tier) VALUES (?, ?, ?, ?)",
        (eid, item_id, role, tier),
    )


def relate(
    conn: sqlite3.Connection,
    src: str,
    dst: str,
    rel_type: str,
    item_id: str,
    method: str,
    tier: int = 0,
) -> int:
    """Create (or reuse) a relation and cite ``item_id`` as its evidence."""
    rel_id = conn.execute(
        """INSERT INTO relations(src, dst, type, tier) VALUES (?, ?, ?, ?)
           ON CONFLICT(src, dst, type) DO UPDATE SET tier = tier
           RETURNING id""",
        (src, dst, rel_type, tier),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO evidence(relation_id, item_id, method) VALUES (?, ?, ?)",
        (rel_id, item_id, method),
    )
    return rel_id


def file_entities(conn: sqlite3.Connection, item_id: str, item: Item) -> str:
    """The file's ``document`` entity (and its folder's ``project``). Returns the document."""
    path = Path(item.uri or item.external_id)
    doc = upsert_entity(conn, "document", item.title or path.name, "path", str(path))
    mention(conn, doc, item_id, "file")

    root = Path(item.extra.get("root", path.parent))
    folder = path.parent
    if folder != root and folder.is_relative_to(root):
        project = upsert_entity(conn, "project", folder.name, "folder", str(folder))
        mention(conn, project, item_id, "folder")
        relate(conn, doc, project, "in_folder", item_id, "tier0:path")
    return doc


VALUE_CONFIDENCE = 0.6


def value_facts(
    conn: sqlite3.Connection,
    item_id: str,
    item: Item,
    doc: str | None,
    values: list[tuple[str, str, str]],
) -> None:
    """Deadlines and amounts found by extract.py, as facts cited by the item.

    Deadlines describe the document (a file, or the email or message itself). An amount in a
    message is what its author quoted or billed, so it goes on the author when there is one.
    """
    from graph_me.pipeline.tier0.facts import upsert_fact

    def document() -> str:
        nonlocal doc
        if doc is None:  # an email or message with a deadline becomes a document of its own
            doc = upsert_entity(conn, "document", item.title or "(untitled)", "item", item_id)
            mention(conn, doc, item_id, "document")
        return doc

    author = conn.execute(
        "SELECT entity_id FROM mentions WHERE item_id = ? AND role = 'author'", (item_id,)
    ).fetchone()
    for key, value, method in values:
        target = author[0] if key.endswith("_amount") and author else document()
        upsert_fact(conn, target, key, value, VALUE_CONFIDENCE, item_id, method)
