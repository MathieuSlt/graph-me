"""Entity resolution at Tier 0: merge entities that share a strong identifier.

Two people are the same person when they share an email address or a phone number (for
example, a contact card lists both the email seen in mail and the number seen on WhatsApp).
A shared *name* is never enough at Tier 0: two "Sophie" stay two people.
"""

from __future__ import annotations

import sqlite3


def merge(conn: sqlite3.Connection, keep: str, drop: str) -> None:
    """Fold entity ``drop`` into ``keep``: aliases, mentions, facts, relations, communities."""
    if keep == drop:
        return
    conn.execute(
        "UPDATE entities SET name = COALESCE(name, (SELECT name FROM entities WHERE id = ?)) "
        "WHERE id = ?",
        (drop, keep),
    )
    for table, column in (("aliases", "entity_id"), ("mentions", "entity_id"),
                          ("community_members", "entity_id")):  # fmt: skip
        conn.execute(f"UPDATE OR IGNORE {table} SET {column} = ? WHERE {column} = ?", (keep, drop))
        conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (drop,))

    # Facts: move each one; when ``keep`` already has the same fact, move its evidence instead.
    for fact_id, key, value in conn.execute(
        "SELECT id, key, value FROM facts WHERE entity_id = ?", (drop,)
    ).fetchall():
        twin = conn.execute(
            "SELECT id FROM facts WHERE entity_id = ? AND key = ? AND value = ?", (keep, key, value)
        ).fetchone()
        if twin:
            conn.execute("UPDATE evidence SET fact_id = ? WHERE fact_id = ?", (twin[0], fact_id))
            conn.execute(
                "UPDATE facts SET confidence = max(confidence, "
                "(SELECT confidence FROM facts WHERE id = ?)) WHERE id = ?",
                (fact_id, twin[0]),
            )
            conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
        else:
            conn.execute("UPDATE facts SET entity_id = ? WHERE id = ?", (keep, fact_id))

    # Relations: re-point both ends; drop self-loops; fold duplicates the same way as facts.
    for rel_id, src, dst, rel_type in conn.execute(
        "SELECT id, src, dst, type FROM relations WHERE src = ? OR dst = ?", (drop, drop)
    ).fetchall():
        new_src = keep if src == drop else src
        new_dst = keep if dst == drop else dst
        if new_src == new_dst:
            conn.execute("DELETE FROM relations WHERE id = ?", (rel_id,))
            continue
        twin = conn.execute(
            "SELECT id FROM relations WHERE src = ? AND dst = ? AND type = ?",
            (new_src, new_dst, rel_type),
        ).fetchone()
        if twin:
            conn.execute(
                "UPDATE evidence SET relation_id = ? WHERE relation_id = ?", (twin[0], rel_id)
            )
            conn.execute("DELETE FROM relations WHERE id = ?", (rel_id,))
        else:
            conn.execute(
                "UPDATE relations SET src = ?, dst = ? WHERE id = ?", (new_src, new_dst, rel_id)
            )
    conn.execute("DELETE FROM entities WHERE id = ?", (drop,))
