"""Apply your corrections (overrides.yaml) to the graph. No model.

- ``person_facts``: relations and facts you set, cited by your correction (confidence 1.0).
- ``apply_changes``: when ``not_same`` changed, or a link or rejection was removed, the people
  holding those identifiers are rebuilt from their items, so a wrong merge splits, an undone
  link splits back, and an undone rejection is learned again.
- ``apply_rejections``: facts you rejected are dropped after every run, even when an item that
  suggested them is read again.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

from graph_me.connectors.base import Party
from graph_me.pipeline.tier0.entities import relate
from graph_me.pipeline.tier0.facts import upsert_fact
from graph_me.pipeline.tier0.people import ME, PeopleContext
from graph_me.store import db

USER_CONFIDENCE = 1.0
_STATE_META = "overrides_state"
_EMPTY = json.dumps({"apart": [], "links": [], "reject": []})
_BATCH = 500

Key = tuple[str, str]


def person_facts(conn: sqlite3.Connection, item_id: str, pid: str, override: dict) -> None:
    me = conn.execute("SELECT entity_id FROM aliases WHERE kind = ? AND value = ?", ME).fetchone()
    if relation := override.get("relation"):
        upsert_fact(conn, pid, "relation_to_user", relation, USER_CONFIDENCE, item_id,
                    "user:relation")  # fmt: skip
        if me and me[0] != pid:
            relate(conn, me[0], pid, relation, item_id, "user:relation")
    for key, value in (override.get("facts") or {}).items():
        upsert_fact(conn, pid, key, value, USER_CONFIDENCE, item_id, "user:fact")


def state(fixes, ctx: PeopleContext) -> dict:
    """What, in overrides.yaml, decides who is who: ``not_same``, links, rejections."""
    links = [sorted({k for i in p.identifiers for k in _keys(ctx, i)}) for p in fixes.people]
    return {
        "apart": ctx.apart_groups(),
        "links": sorted(sorted(map(list, g)) for g in links if len(g) > 1),
        "reject": sorted([sorted(map(list, keys)), fact, value]
                         for keys, fact, value in rejections(fixes, ctx)),
    }  # fmt: skip


def changes_pending(conn: sqlite3.Connection, fixes, ctx: PeopleContext) -> bool:
    return json.dumps(state(fixes, ctx)) != (db.get_meta(conn, _STATE_META) or _EMPTY)


def apply_changes(conn: sqlite3.Connection, fixes, ctx: PeopleContext) -> int:
    """Rebuild the people a correction no longer describes. Returns items to re-read.

    Merges are kept even when the item that caused them is gone, and a rejected fact is not
    learned again until its item is re-read. So when ``not_same`` changes, or a link or a
    rejection is removed, the people holding those identifiers are deleted (with what was
    learned about them) and their items marked as changed: the same run reads them again.
    """
    current = state(fixes, ctx)
    stored = db.get_meta(conn, _STATE_META) or _EMPTY
    if json.dumps(current) == stored:
        return 0
    previous = json.loads(stored)
    keys: set[Key] = set()
    if current["apart"] != previous["apart"]:
        keys |= {tuple(k) for g in [*current["apart"], *previous["apart"]] for k in g}
    for group in previous["links"]:
        if group not in current["links"]:
            keys |= {tuple(k) for k in group}
    for rejected in previous["reject"]:
        if rejected not in current["reject"]:
            keys |= {tuple(k) for k in rejected[0]}
    people: set[str] = set()
    for kind, value in keys:
        people.update(
            r[0]
            for r in conn.execute(
                "SELECT entity_id FROM aliases WHERE kind = ? AND value = ?", (kind, value)
            )
        )
    items: set[str] = set()
    for eid in people:
        items.update(
            r[0] for r in conn.execute("SELECT item_id FROM mentions WHERE entity_id = ?", (eid,))
        )
    _in_batches(conn, "DELETE FROM entities WHERE id IN ({})", people)
    _in_batches(conn, "UPDATE items SET version = NULL WHERE id IN ({})", items)
    db.set_meta(conn, _STATE_META, json.dumps(current))
    conn.commit()
    return len(items)


def _keys(ctx: PeopleContext, identifier: str) -> list[Key]:
    return ctx.keys(Party(email=identifier, phone=identifier))


def rejections(overrides, ctx: PeopleContext) -> list[tuple[list[Key], str, str]]:
    """``reject`` entries as (identifier keys, fact, value)."""
    return [(_keys(ctx, r.who), r.fact, r.value) for r in overrides.reject]


def apply_rejections(
    conn: sqlite3.Connection, rejected: Iterable[tuple[list[Key], str, str]]
) -> int:
    """Delete rejected facts (and the matching relation to you). Returns facts removed."""
    removed = 0
    me = conn.execute("SELECT entity_id FROM aliases WHERE kind = ? AND value = ?", ME).fetchone()
    for keys, fact, value in rejected:
        for kind, alias in keys:
            for (eid,) in conn.execute(
                "SELECT entity_id FROM aliases WHERE kind = ? AND value = ?", (kind, alias)
            ).fetchall():
                removed += conn.execute(
                    "DELETE FROM facts WHERE entity_id = ? AND key = ? AND value = ?",
                    (eid, fact, value),
                ).rowcount
                if fact == "relation_to_user" and me:
                    conn.execute(
                        "DELETE FROM relations WHERE src = ? AND dst = ? AND type = ?",
                        (me[0], eid, value),
                    )
    return removed


def _in_batches(conn: sqlite3.Connection, sql: str, ids: Iterable[str]) -> None:
    ids = list(ids)
    for start in range(0, len(ids), _BATCH):
        batch = ids[start : start + _BATCH]
        conn.execute(sql.format(",".join("?" * len(batch))), batch)
