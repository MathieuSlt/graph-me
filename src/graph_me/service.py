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
from graph_me.overrides import SOURCE as OVERRIDES_SOURCE
from graph_me.query import engine, graph, pack
from graph_me.store import db, scope

ITEM_MAX_WORDS = 4000  # get_item returns at most this much text


class StoreMissing(RuntimeError):
    pass


def freshness(conn: sqlite3.Connection, stale_after_days: int) -> dict:
    """When the index was last synced: the oldest source counts (one stale source is enough)."""
    oldest = conn.execute(
        "SELECT min(last_sync_at) FROM sources WHERE id != ?", (OVERRIDES_SOURCE,)
    ).fetchone()[0]
    if not oldest:
        return {"last_sync_at": None, "age_days": None, "stale": False}
    age = (datetime.now(UTC) - datetime.fromisoformat(oldest)).days
    return {"last_sync_at": oldest, "age_days": age, "stale": age > stale_after_days}


class UnknownProfile(ValueError):
    pass


class Service:
    """``profile``: a name under ``profiles:`` in config.yaml. Every query then sees only that
    profile's sources (store/scope.py). ``client``: the app asking, for the activity log."""

    def __init__(
        self,
        cfg: Config,
        db_path: Path,
        interface: str = "cli",
        *,
        profile: str | None = None,
        client: str | None = None,
    ) -> None:
        if profile is not None and profile not in cfg.profiles:
            known = ", ".join(sorted(cfg.profiles)) or "none in config.yaml"
            raise UnknownProfile(f"unknown profile {profile!r} (known: {known})")
        self.cfg = cfg
        self.db_path = db_path
        self.interface = interface
        self.profile = profile
        self.client = client

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        if not self.db_path.exists():
            raise StoreMissing(
                f"No graph at {self.db_path.parent}. Run `graph-me init` and `scan`."
            )
        conn = db.connect(self.db_path)
        if self.profile is not None:
            chosen = self.cfg.profiles[self.profile]
            scope.limit(conn, chosen.sources, chosen.kinds)
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

    def _freshness(self, conn: sqlite3.Connection) -> dict:
        return freshness(conn, self.cfg.index.stale_after_days)

    def _log(self, conn: sqlite3.Connection, query: str, result_ids: list[str]) -> None:
        conn.execute(
            "INSERT INTO main.query_log(ts, interface, query, result_ids, client, profile) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now(UTC).isoformat(timespec="seconds"), self.interface, query,
             json.dumps(result_ids), self.client, self.profile),
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
            result["index"] = self._freshness(conn)
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
            index = self._freshness(conn)
            self._log(conn, f"who_is:{who}", [p["id"] for p in people])
        return {"query": who, "notice": pack.NOTICE, "people": _redact_people(people),
                "index": index}  # fmt: skip

    def get_fact(self, who: str, key: str) -> dict:
        with self._conn() as conn:
            found = graph.get_fact(conn, who, key, self.cfg.people)
            index = self._freshness(conn)
            self._log(conn, f"get_fact:{who}:{key}", [p["entity"]["id"] for p in found])
        _redact_people(found)
        return {"query": f"{who} {key}", "notice": pack.NOTICE, "results": found, "index": index}

    def related(self, who: str, limit: int = 10) -> dict:
        with self._conn() as conn:
            people = graph.find_people(conn, who, self.cfg.people, limit=1)
            out = []
            for eid in people:
                out.append(
                    {"entity": graph.entity(conn, eid), "related": graph.related(conn, eid, limit)}
                )
            index = self._freshness(conn)
            self._log(conn, f"related:{who}", people)
        return {"query": who, "notice": pack.NOTICE, "results": out, "index": index}

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
            index = self._freshness(conn)
            self._log(conn, f"timeline:{who}", people)
        return {"query": who, "notice": pack.NOTICE, "results": out, "index": index}

    def upcoming(self, *, days: int = 60, kind: str | None = None, today=None) -> dict:
        """Birthdays, anniversaries and deadlines (expires, renews, due, ends) coming up."""
        from datetime import date

        today = today or date.today()
        kinds = None
        if kind:
            kinds = graph.ANNUAL_KEYS if kind == "dates" else (
                graph.DEADLINE_KEYS if kind == "deadlines" else (kind,))  # fmt: skip
        with self._conn() as conn:
            found = graph.upcoming(conn, today, days=days, kinds=kinds)
            index = self._freshness(conn)
            self._log(conn, f"upcoming:{days}:{kind or ''}", [u["entity"]["id"] for u in found])
        for u in found:
            u["entity"]["name"] = _redact_name(u["entity"]["name"])
            for ev in u["evidence"]:
                ev["title"] = _redact_name(ev["title"])
        return {"query": f"upcoming {days} days", "notice": pack.NOTICE, "today": today.isoformat(),
                "results": found, "index": index}  # fmt: skip

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
            index = self._freshness(conn)
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
            "index": index,
        }
        if redacted:
            out["redacted"] = sorted(set(redacted))
        return out

    # -- activity (for you, not for agents: there is no MCP tool for it) --------------------

    def activity(
        self,
        *,
        since: str | None = None,
        interface: str | None = None,
        client: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """The latest queries, newest first, with the items each one returned.

        Under a profile, only that profile's queries. Items forgotten since (or that the
        profile can't see) show as gone. Not logged itself.
        """
        where, params = [], []
        for column, value in (("interface", interface), ("client", client),
                              ("profile", self.profile)):  # fmt: skip
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        if since:
            where.append("ts >= ?")
            params.append(since)
        sql = "SELECT rowid AS n, * FROM main.query_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._conn() as conn:
            rows = conn.execute(sql + " ORDER BY ts DESC, n DESC LIMIT ?", (*params, limit))
            out = []
            for r in rows.fetchall():
                ids = json.loads(r["result_ids"] or "[]")
                out.append(
                    {
                        "ts": r["ts"],
                        "interface": r["interface"],
                        "client": r["client"],
                        "profile": r["profile"],
                        "query": pack.redact(r["query"])[0],
                        "results": [_result(conn, i) for i in ids[:20]],
                        "result_count": len(ids),
                    }
                )
        return out

    def clear_activity(self, before: str | None = None) -> int:
        with self._conn() as conn:
            sql, params = "DELETE FROM main.query_log", ()
            if before:
                sql, params = sql + " WHERE ts < ?", (before,)
            if self.profile is not None:
                sql += (" AND" if before else " WHERE") + " profile = ?"
                params = (*params, self.profile)
            removed = conn.execute(sql, params).rowcount
            conn.commit()
        return removed

    # -- web UI ----------------------------------------------------------------------------

    def entity_page(self, eid: str) -> dict | None:
        """One entity with its facts (and their evidence), relations and latest items."""
        with self._conn() as conn:
            info = graph.entity(conn, eid)
            if info is None:
                return None
            info["facts"] = graph.facts_of(conn, eid)
            info["related"] = graph.related(conn, eid, limit=30)
            info["items"] = graph.timeline(conn, eid, limit=50)
            info["community"] = conn.execute(
                """SELECT c.label FROM community_members cm JOIN communities c
                   ON c.id = cm.community_id WHERE cm.entity_id = ?""",
                (eid,),
            ).fetchone()
            info["community"] = info["community"][0] if info["community"] else None
            self._log(conn, f"entity:{eid}", [eid])
        _redact_people([info])
        _redact_titles(info["items"])
        info["name"] = _redact_name(info["name"])
        for rel in info["related"]:
            rel["entity"]["name"] = _redact_name(rel["entity"]["name"])
        return info

    def item_page(self, item_id: str) -> dict:
        """get_item plus what the item mentions and the facts learned from it."""
        out = self.get_item(item_id)
        if "error" in out:
            return out
        with self._conn() as conn:
            out["entities"] = graph.item_entities(conn, item_id)
            out["facts"] = graph.item_facts(conn, item_id)
        out["title"] = _redact_name(out["title"])
        for ent in out["entities"]:
            ent["name"] = _redact_name(ent["name"])
        for fact in out["facts"]:
            fact["value"] = _redact_name(fact["value"])
        return out

    def graph_view(
        self,
        *,
        limit: int = 2000,
        kinds: list[str] | None = None,
        source: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> dict:
        with self._conn() as conn:
            data = graph.subgraph(
                conn, limit=limit, kinds=kinds, source=source, since=since, until=until
            )
            self._log(conn, "graph", [])
        for node in data["nodes"]:
            node["name"] = _redact_name(node["name"])
        return data

    def neighbourhood(self, eid: str, limit: int = 200) -> dict:
        with self._conn() as conn:
            data = graph.neighbourhood(conn, eid, limit)
            self._log(conn, f"graph:{eid}", [n["id"] for n in data["nodes"]])
        for node in data["nodes"]:
            node["name"] = _redact_name(node["name"])
        return data

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
            by_tier = dict(conn.execute("SELECT tier, count(*) FROM items GROUP BY tier"))
            flagged = [
                dict(r)
                for r in conn.execute(
                    """SELECT id, kind, title, source_id AS source, round(risk_score, 2) AS risk
                       FROM items WHERE risk_score >= ? ORDER BY risk_score DESC, id LIMIT 50""",
                    (pack.RISK_FLAG_AT,),
                )
            ]
            flagged_total = conn.execute(
                "SELECT count(*) FROM items WHERE risk_score >= ?", (pack.RISK_FLAG_AT,)
            ).fetchone()[0]
            last_run = json.loads(db.get_meta(conn, "last_run") or "null")
            if last_run and self.profile is not None:  # meta isn't scoped: drop other sources
                allowed = set(self.cfg.profiles[self.profile].sources)
                last_run["sources"] = {
                    k: v for k, v in last_run.get("sources", {}).items() if k in allowed
                }
                last_run["forgotten_blacklisted"] = None
            index = self._freshness(conn)
        _redact_titles(flagged)
        bl = self.cfg.blacklist
        return {
            "store": str(self.db_path.parent),
            "profile": self.profile,
            "sources": sources,
            "counts": counts,
            "tier": tier,
            "note": "Tier 0: no AI; results match words, not meaning." if tier == 0 else None,
            "items_by_tier": by_tier,
            "flagged": flagged,
            "flagged_total": flagged_total,
            "last_run": last_run,
            "index": index,
            "blacklist": {
                "paths": len(bl.paths),
                "contacts": len(bl.contacts),
                "patterns": len(bl.patterns),
            },
        }


def _result(conn: sqlite3.Connection, ident: str) -> dict:
    """One id from the query log: an item or an entity, or gone."""
    row = conn.execute("SELECT id, kind, title FROM items WHERE id = ?", (ident,)).fetchone()
    if row:
        return {"id": row["id"], "type": "item", "kind": row["kind"],
                "title": _redact_name(row["title"])}  # fmt: skip
    row = conn.execute("SELECT id, kind, name FROM entities WHERE id = ?", (ident,)).fetchone()
    if row:
        return {"id": row["id"], "type": "entity", "kind": row["kind"],
                "title": _redact_name(row["name"])}  # fmt: skip
    return {"id": ident, "type": "gone", "kind": None, "title": None}


def _redact_people(people: list[dict]) -> list[dict]:
    """Evidence titles can quote message text: redact secrets there too."""
    for person in people:
        for fact in person.get("facts", []):
            for ev in fact.get("evidence", []):
                ev["title"] = pack.redact(ev["title"] or "")[0]
    return people


def _redact_name(text: str | None) -> str | None:
    return pack.redact(text)[0] if text else text


def _redact_titles(items: list[dict]) -> None:
    """Message subjects can carry secrets too."""
    for item in items:
        item["title"] = _redact_name(item.get("title"))
