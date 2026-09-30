"""Graph queries: people, facts with their evidence, relations, timelines, file origins."""

from __future__ import annotations

import re
import sqlite3

from graph_me import rules
from graph_me.config import PeopleConfig
from graph_me.connectors.base import Party
from graph_me.pipeline.sanitize import fold
from graph_me.pipeline.tier0.people import ME, PeopleContext

_WORD = re.compile(r"\w+")
_ITEM_DATE = "COALESCE(i.created_at, i.modified_at)"


def _item(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "uri": row["uri"],
        "source": row["source_id"],
        "date": row["date"],
        "trust": row["trust"],
    }


def find_people(
    conn: sqlite3.Connection, text: str, people_cfg: PeopleConfig | None = None, limit: int = 5
) -> list[str]:
    """Person entity ids matching ``text``: an email/phone, "me", a name or a nickname."""
    ctx = PeopleContext.from_config(people_cfg or PeopleConfig())
    scores: dict[str, float] = {}
    for kind, value in ctx.keys(Party(email=text.strip(), phone=text.strip())):
        for (eid,) in conn.execute(
            "SELECT entity_id FROM aliases WHERE kind = ? AND value = ?", (kind, value)
        ):
            scores[eid] = scores.get(eid, 0) + 10
    words = set(_WORD.findall(fold(text)))
    relations = {rel for word, rel in rules.relation_words().items() if word in words}
    if relations:  # "ma soeur", "my landlord": people Tier 1 labelled with that relation
        marks = ",".join("?" * len(relations))
        for (eid,) in conn.execute(
            f"SELECT entity_id FROM facts WHERE key = 'relation_to_user' AND value IN ({marks})",
            sorted(relations),
        ):
            scores[eid] = scores.get(eid, 0) + 6
    if words & {"me", "moi", "myself"}:
        for (eid,) in conn.execute(
            "SELECT entity_id FROM aliases WHERE kind = ? AND value = ?", ME
        ):
            scores[eid] = scores.get(eid, 0) + 5
    if words:
        for eid, name in conn.execute(
            "SELECT id, name FROM entities WHERE kind = 'person' AND name IS NOT NULL"
        ):
            parts = set(_WORD.findall(fold(name)))
            hits = len(words & parts)
            if hits:
                scores[eid] = scores.get(eid, 0) + hits + (2 if parts <= words else 0)
        for eid, value in conn.execute("SELECT entity_id, value FROM facts WHERE key = 'nickname'"):
            if set(_WORD.findall(fold(value))) <= words:
                scores[eid] = scores.get(eid, 0) + 3
    if not scores:
        return []
    # "Sophie Bernard" names one person fully: drop weak partial matches ("Sophie Martin").
    best = max(scores.values())
    scores = {e: v for e, v in scores.items() if v >= best / 2}
    # Tie-break on how much the graph knows about each person.
    ranked = sorted(
        scores,
        key=lambda e: (
            scores[e],
            conn.execute("SELECT count(*) FROM mentions WHERE entity_id = ?", (e,)).fetchone()[0],
        ),
        reverse=True,
    )
    return ranked[:limit]


def evidence(conn: sqlite3.Connection, *, fact_id: int | None = None,
             relation_id: int | None = None, limit: int = 10) -> list[dict]:  # fmt: skip
    column, value = ("fact_id", fact_id) if fact_id is not None else ("relation_id", relation_id)
    rows = conn.execute(
        f"""SELECT i.*, {_ITEM_DATE} AS date, e.method
            FROM evidence e JOIN items i ON i.id = e.item_id
            WHERE e.{column} = ?
            ORDER BY e.method LIKE 'contact:%' DESC, date DESC LIMIT ?""",
        (value, limit),
    ).fetchall()
    return [{**_item(r), "method": r["method"]} for r in rows]


def facts_of(conn: sqlite3.Connection, entity: str, key: str | None = None) -> list[dict]:
    rows = conn.execute(
        "SELECT id, key, value, confidence, tier FROM facts WHERE entity_id = ?"
        + (" AND key = ?" if key else "")
        + " ORDER BY key, confidence DESC",
        (entity, key) if key else (entity,),
    ).fetchall()
    return [
        {
            "key": r["key"],
            "value": r["value"],
            "confidence": round(r["confidence"], 2),
            "tier": r["tier"],
            "evidence": evidence(conn, fact_id=r["id"]),
        }
        for r in rows
    ]


def entity(conn: sqlite3.Connection, eid: str) -> dict | None:
    row = conn.execute("SELECT id, kind, name FROM entities WHERE id = ?", (eid,)).fetchone()
    if not row:
        return None
    aliases = conn.execute(
        "SELECT kind, value FROM aliases WHERE entity_id = ? ORDER BY kind, value", (eid,)
    ).fetchall()
    return {
        "id": row["id"],
        "kind": row["kind"],
        "name": row["name"],
        "is_me": any((a["kind"], a["value"]) == ME for a in aliases),
        "aliases": [
            {"kind": a["kind"], "value": a["value"]}
            for a in aliases
            if (a["kind"], a["value"]) != ME and a["kind"] != "contact"
        ],
        "mentions": conn.execute(
            "SELECT count(*) FROM mentions WHERE entity_id = ?", (eid,)
        ).fetchone()[0],
    }


def related(conn: sqlite3.Connection, eid: str, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        """SELECT r.id, r.type, r.weight, r.src = ? AS outgoing, e.id AS other, e.kind, e.name
           FROM relations r JOIN entities e ON e.id = CASE WHEN r.src = ? THEN r.dst ELSE r.src END
           WHERE r.src = ? OR r.dst = ?
           ORDER BY r.weight DESC LIMIT ?""",
        (eid, eid, eid, eid, limit),
    ).fetchall()
    return [
        {
            "type": r["type"],
            "direction": "out" if r["outgoing"] else "in",
            "weight": r["weight"],
            "entity": {"id": r["other"], "kind": r["kind"], "name": r["name"]},
        }
        for r in rows
    ]


def who_is(
    conn: sqlite3.Connection, text: str, people_cfg: PeopleConfig | None = None, limit: int = 3
) -> list[dict]:
    out = []
    for eid in find_people(conn, text, people_cfg, limit):
        info = entity(conn, eid)
        info["facts"] = facts_of(conn, eid)
        info["related"] = related(conn, eid, limit=5)
        out.append(info)
    return out


def get_fact(
    conn: sqlite3.Connection, who: str, key: str, people_cfg: PeopleConfig | None = None
) -> list[dict]:
    """``key`` facts ("birthday", "nickname"...) for the people matching ``who``."""
    out = []
    for eid in find_people(conn, who, people_cfg):
        found = facts_of(conn, eid, key)
        if found:
            out.append({"entity": entity(conn, eid), "facts": found})
    return out


def timeline(
    conn: sqlite3.Connection,
    eid: str,
    *,
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
) -> list[dict]:
    where, params = ["m.entity_id = ?"], [eid]
    if since:
        where.append(f"{_ITEM_DATE} >= ?")
        params.append(since)
    if until:
        where.append(f"{_ITEM_DATE} <= ?")
        params.append(until)
    rows = conn.execute(
        f"""SELECT DISTINCT i.*, {_ITEM_DATE} AS date FROM items i
            JOIN mentions m ON m.item_id = i.id
            WHERE {" AND ".join(where)} ORDER BY date DESC LIMIT ?""",
        [*params, limit],
    ).fetchall()
    return [_item(r) for r in rows]


def origins(conn: sqlite3.Connection, item_id: str) -> list[dict]:
    """Messages that carried this file as an attachment (same sha256), newest first."""
    rows = conn.execute(
        f"""SELECT DISTINCT i.*, {_ITEM_DATE} AS date, a.filename
            FROM items f
            JOIN item_attachments a ON a.content_hash = f.content_hash
            JOIN items i ON i.id = a.item_id
            WHERE f.id = ? AND f.content_hash IS NOT NULL
            ORDER BY date DESC LIMIT 5""",
        (item_id,),
    ).fetchall()
    out = []
    for r in rows:
        author = conn.execute(
            """SELECT e.name, a.value FROM mentions m JOIN entities e ON e.id = m.entity_id
               LEFT JOIN aliases a ON a.entity_id = e.id AND a.kind IN ('email', 'phone')
               WHERE m.item_id = ? AND m.role = 'author' LIMIT 1""",
            (r["id"],),
        ).fetchone()
        out.append(
            {
                **_item(r),
                "attachment": r["filename"],
                "from": (author["name"] or author["value"]) if author else None,
            }
        )
    return out


def saved_copies(conn: sqlite3.Connection, item_id: str) -> list[dict]:
    """Files on disk with the same bytes as this message's attachments."""
    rows = conn.execute(
        f"""SELECT DISTINCT i.*, {_ITEM_DATE} AS date, a.filename
            FROM item_attachments a
            JOIN items i ON i.content_hash = a.content_hash AND i.kind = 'file'
            WHERE a.item_id = ? AND a.content_hash IS NOT NULL""",
        (item_id,),
    ).fetchall()
    return [{**_item(r), "attachment": r["filename"], "path": r["uri"]} for r in rows]


def enrich_hits(conn: sqlite3.Connection, item_ids: list[str]) -> dict[str, dict]:
    """Extra context per search hit: where a file came from, where attachments were saved."""
    extras: dict[str, dict] = {}
    for iid in item_ids:
        kind = conn.execute("SELECT kind FROM items WHERE id = ?", (iid,)).fetchone()
        if not kind:
            continue
        if kind[0] == "file":
            if found := origins(conn, iid):
                extras[iid] = {"origin": found}
        else:
            people = _people_of(conn, iid)
            extra: dict = {k: v for k, v in people.items() if v}
            attachments = [
                dict(r)
                for r in conn.execute(
                    "SELECT filename, mime_type FROM item_attachments WHERE item_id = ?", (iid,)
                )
            ]
            if attachments:
                extra["attachments"] = attachments
            if copies := saved_copies(conn, iid):
                extra["saved_as"] = copies
            if extra:
                extras[iid] = extra
    return extras


def _people_of(conn: sqlite3.Connection, item_id: str) -> dict:
    """Author and recipients of a message, by their best known name."""
    rows = conn.execute(
        """SELECT m.role, e.name,
                  (SELECT value FROM aliases a WHERE a.entity_id = e.id
                   AND a.kind IN ('email', 'phone', 'handle') LIMIT 1) AS ident
           FROM mentions m JOIN entities e ON e.id = m.entity_id
           WHERE m.item_id = ? AND m.role IN ('author', 'recipient')""",
        (item_id,),
    ).fetchall()
    label = lambda r: r["name"] or r["ident"]  # noqa: E731
    return {
        "from": next((label(r) for r in rows if r["role"] == "author"), None),
        "to": [label(r) for r in rows if r["role"] == "recipient"],
    }


def facts_for_query(
    conn: sqlite3.Connection, text: str, people_cfg: PeopleConfig | None = None, limit: int = 3
) -> list[dict]:
    """Facts about the people a query names ("anniversaire Sophie" -> Sophie's facts)."""
    out = []
    for eid in find_people(conn, text, people_cfg, limit):
        found = facts_of(conn, eid)
        if found:
            out.append({"entity": entity(conn, eid), "facts": found})
    return out
