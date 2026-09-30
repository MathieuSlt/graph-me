"""The read-only query service shared by the CLI and the MCP server.

Every answer is a context pack: cited, trust-tagged, redacted, marked as data (query/pack.py).
Every query is logged in ``query_log`` with the interface it came from.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from graph_me.config import Config
from graph_me.query import engine, graph, pack
from graph_me.store import db

ITEM_MAX_WORDS = 4000  # get_item returns at most this much text


class StoreMissing(RuntimeError):
    pass


class Service:
    def __init__(self, cfg: Config, db_path: Path, interface: str = "cli") -> None:
        self.cfg = cfg
        self.db_path = db_path
        self.interface = interface

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        if not self.db_path.exists():
            raise StoreMissing(
                f"No graph at {self.db_path.parent}. Run `graph-me init` and `scan`."
            )
        conn = db.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _embedder(self, conn: sqlite3.Connection):
        """The model that built this graph's vectors, if any (queries must use the same one)."""
        from graph_me.pipeline.tier1 import embed

        stored = db.get_meta(conn, "embeddings_model")
        if not stored or not embed.has_vectors(conn) or not embed.vec_loaded(conn):
            return None
        try:
            return embed.get(stored.rsplit(":", 1)[0])
        except ImportError:
            return None

    def _log(self, conn: sqlite3.Connection, query: str, result_ids: list[str]) -> None:
        conn.execute(
            "INSERT INTO query_log(ts, interface, query, result_ids) VALUES (?, ?, ?, ?)",
            (datetime.now(UTC).isoformat(timespec="seconds"), self.interface, query,
             json.dumps(result_ids)),
        )  # fmt: skip
        conn.commit()

    # -- queries ---------------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        source: str | None = None,
        kind: str | None = None,
        since: str | None = None,
        until: str | None = None,
        reveal: bool = False,
    ) -> dict:
        with self._conn() as conn:
            hits = engine.search(
                conn, query, limit=limit, source=source, kind=kind, since=since, until=until,
                embedder=self._embedder(conn),
            )  # fmt: skip
            result = pack.build(
                query,
                hits,
                facts=graph.facts_for_query(conn, query, self.cfg.people),
                extras=graph.enrich_hits(conn, [h.item_id for h in hits]),
                reveal=reveal,
            )
            self._log(conn, query, [i["id"] for i in result["answer_items"]])
        return result

    def find_document(
        self, description: str, *, since: str | None = None, until: str | None = None,
        limit: int = 5,
    ) -> dict:  # fmt: skip
        """Files matching a description, each with the message it came with (if any)."""
        result = self.search(description, kind="file", since=since, until=until, limit=limit)
        if not result["answer_items"]:
            # The file may only exist as an attachment: fall back to messages carrying one.
            messages = self.search(description, since=since, until=until, limit=limit)
            result["answer_items"] = [i for i in messages["answer_items"] if i.get("attachments")]
        return result

    def who_is(self, who: str) -> dict:
        with self._conn() as conn:
            people = graph.who_is(conn, who, self.cfg.people)
            self._log(conn, f"who_is:{who}", [p["id"] for p in people])
        return {"query": who, "notice": pack.NOTICE, "people": _redact_people(people)}

    def get_fact(self, who: str, key: str) -> dict:
        with self._conn() as conn:
            found = graph.get_fact(conn, who, key, self.cfg.people)
            self._log(conn, f"get_fact:{who}:{key}", [p["entity"]["id"] for p in found])
        _redact_people(found)
        return {"query": f"{who} {key}", "notice": pack.NOTICE, "results": found}

    def related(self, who: str, limit: int = 10) -> dict:
        with self._conn() as conn:
            people = graph.find_people(conn, who, self.cfg.people, limit=1)
            out = []
            for eid in people:
                out.append(
                    {"entity": graph.entity(conn, eid), "related": graph.related(conn, eid, limit)}
                )
            self._log(conn, f"related:{who}", people)
        return {"query": who, "notice": pack.NOTICE, "results": out}

    def timeline(
        self, who: str, *, since: str | None = None, until: str | None = None, limit: int = 20
    ) -> dict:
        with self._conn() as conn:
            people = graph.find_people(conn, who, self.cfg.people, limit=1)
            out = []
            for eid in people:
                out.append(
                    {
                        "entity": graph.entity(conn, eid),
                        "items": graph.timeline(conn, eid, since=since, until=until, limit=limit),
                    }
                )
            self._log(conn, f"timeline:{who}", people)
        return {"query": who, "notice": pack.NOTICE, "results": out}

    def get_item(self, item_id: str, *, reveal: bool = False) -> dict:
        """One item's full text (redacted, trust-tagged, capped at ITEM_MAX_WORDS)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT *, COALESCE(created_at, modified_at) AS date FROM items WHERE id = ?",
                (item_id,),
            ).fetchone()
            if not row:
                return {"error": f"no item {item_id!r}"}
            text = "\n\n".join(
                r[0] for r in conn.execute(
                    "SELECT text FROM chunks WHERE item_id = ? ORDER BY ord", (item_id,)
                )
            )  # fmt: skip
            extras = graph.enrich_hits(conn, [item_id]).get(item_id, {})
            self._log(conn, f"get_item:{item_id}", [item_id])
        words = text.split(" ")
        truncated = len(words) > ITEM_MAX_WORDS
        text = " ".join(words[:ITEM_MAX_WORDS]) if truncated else text
        redacted: list[str] = []
        if not reveal:
            text, redacted = pack.redact(text)
        out = {
            "notice": pack.NOTICE,
            "id": row["id"],
            "kind": row["kind"],
            "title": row["title"],
            "uri": row["uri"],
            "path": row["uri"] if row["kind"] == "file" else None,
            "source": row["source_id"],
            "date": row["date"],
            "lang": row["lang"],
            "trust": row["trust"],
            "risk": round(row["risk_score"], 2),
            "flagged": row["risk_score"] >= pack.RISK_FLAG_AT,
            "text": text,
            "truncated": truncated,
            **extras,
        }
        if redacted:
            out["redacted"] = sorted(set(redacted))
        return out

    def status(self) -> dict:
        with self._conn() as conn:
            sources = [
                dict(r)
                for r in conn.execute(
                    """SELECT s.id AS name, s.type, s.last_sync_at,
                              (SELECT count(*) FROM items i WHERE i.source_id = s.id) AS items
                       FROM sources s ORDER BY s.id"""
                )
            ]
            counts = db.counts(conn)
            tier = conn.execute("SELECT max(tier) FROM items").fetchone()[0] or 0
        return {
            "store": str(self.db_path.parent),
            "sources": sources,
            "counts": counts,
            "tier": tier,
            "note": "Tier 0: no AI; results match words, not meaning." if tier == 0 else None,
        }


def _redact_people(people: list[dict]) -> list[dict]:
    """Evidence titles can quote message text: redact secrets there too."""
    for person in people:
        for fact in person.get("facts", []):
            for ev in fact.get("evidence", []):
                ev["title"] = pack.redact(ev["title"] or "")[0]
    return people
