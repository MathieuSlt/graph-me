"""Tier 0 scan and sync: connectors -> parse -> blacklist -> sanitize -> chunks -> entities.

Both process new and changed items (by connector ``version``) and leave unchanged ones alone.
When the blacklist changed since the last run, stored items it now covers are forgotten first.

``sync`` also forgets what disappeared: items no longer listed by their source, and every item of
a source removed from config.yaml. Two safety nets keep an unreachable source from wiping the
index: connectors raise ``SourceUnavailable`` instead of listing nothing, and a sync refuses to
forget more than half of a source (above ``MASS_FORGET_MIN`` items) unless explicitly allowed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

from graph_me import overrides as corrections
from graph_me.config import BlacklistConfig, Config, SourceConfig
from graph_me.connectors import registry
from graph_me.connectors.base import Connector, Item, Party, SourceUnavailable
from graph_me.pipeline import chunk, lang, parse, sanitize
from graph_me.pipeline.tier0 import entities, extract, facts, people
from graph_me.pipeline.tier0 import overrides as user
from graph_me.store import db, forget

BATCH = 500
MAX_ERRORS_KEPT = 20
MASS_FORGET_MIN = 50  # a sync may always forget up to this many items of a source
MASS_FORGET_SHARE = 0.5  # beyond it, forgetting more than this share needs allow_mass_forget


@dataclass
class SourceStats:
    seen: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    blacklisted: int = 0
    parse_errors: int = 0
    flagged: int = 0  # items with a non-zero injection risk score
    forgotten: int = 0  # sync: items gone from the source, removed from the graph
    older: int = 0  # scan --since: items left for a later full scan
    errors: list[str] = field(default_factory=list)  # unreadable items
    failure: str | None = None  # the whole source was skipped (unavailable, guard...)


Progress = Callable[[str, int, int], None]  # (source, done, total)


def item_id(source: str, external_id: str) -> str:
    return hashlib.sha256(f"{source}\0{external_id}".encode()).hexdigest()[:32]


@dataclass
class Prepared:
    """Worker output for one item: sanitized text, its chunks and metadata."""

    text: str = ""
    risk: float = 0.0
    lang: str | None = None
    chunks: list[tuple[str, int]] = field(default_factory=list)
    values: list[tuple[str, str, str]] = field(default_factory=list)  # deadlines, amounts
    error: str | None = None


Job = tuple[str | None, str | None, str | None]  # (path, text, item date as YYYY-MM-DD)


def _prepare(job: Job) -> Prepared:
    """Worker entry point: parse (if needed), sanitize, detect language, chunk, extract values.

    ``job`` is ``(path, text, day)``: one of path and text is set; ``day`` is the item's own date
    (a deadline on that day is when it was written). Top-level so it can run in a process pool.
    """
    path, text, day = job
    error = None
    if text is None and path:
        try:
            text = parse.parse_file(Path(path))
        except parse.ParseError as exc:
            text, error = "", str(exc)
    clean = sanitize.sanitize(text or "")
    return Prepared(
        text=clean.text,
        risk=clean.risk_score,
        lang=lang.detect(clean.text) if clean.text else None,
        chunks=chunk.chunk(clean.text),
        values=extract.extract(clean.text, date.fromisoformat(day) if day else None),
        error=error,
    )


class _Workers:
    """Prepares items inline (workers=1) or in a process pool."""

    def __init__(self, workers: int) -> None:
        self.pool = ProcessPoolExecutor(workers) if workers > 1 else None

    def map(self, jobs: list[Job]) -> Iterator[Prepared]:
        if self.pool:
            return self.pool.map(_prepare, jobs, chunksize=8)
        return map(_prepare, jobs)

    def close(self) -> None:
        if self.pool:
            self.pool.shutdown()


class _Blacklist:
    def __init__(self, cfg: BlacklistConfig, ctx: people.PeopleContext) -> None:
        self.patterns = [re.compile(p, re.IGNORECASE) for p in cfg.patterns]
        self.ctx = ctx
        self.contacts = blacklisted_identities(cfg, ctx)

    def blocks(self, text: str | None) -> bool:
        return bool(text) and any(p.search(text) for p in self.patterns)

    def blocks_people(self, item: Item) -> bool:
        if not self.contacts:
            return False
        parties = [item.author, *item.recipients]
        if item.contact:
            parties += [Party(email=e) for e in item.contact.emails]
            parties += [Party(phone=p) for p in item.contact.phones]
        return any(k in self.contacts for party in parties for k in self.ctx.keys(party))


def blacklisted_identities(cfg: BlacklistConfig, ctx: people.PeopleContext) -> set:
    """Normalized (kind, value) identifiers of blacklisted contacts (emails or phone numbers)."""
    return {k for value in cfg.contacts for k in ctx.keys(Party(email=value, phone=value))}


def default_workers() -> int:
    return max(1, min(8, (os.cpu_count() or 2) - 1))


@dataclass
class ScanReport:
    sources: dict[str, SourceStats] = field(default_factory=dict)
    forgotten_blacklisted: int = 0  # stored items removed because the blacklist now covers them
    removed_sources: dict[str, int] = field(default_factory=dict)  # sync: source -> items forgotten
    failures: dict[str, str] = field(default_factory=dict)  # removed sources that were kept
    relinked: int = 0  # items re-read because overrides.yaml changed who is who
    backfilled: int = 0  # stored items given new extraction rules (from their stored text)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures and all(st.failure is None for st in self.sources.values())


def _too_many(missing: int, known: int) -> bool:
    return missing > MASS_FORGET_MIN and missing > MASS_FORGET_SHARE * known


def _guard_message(what: str, missing: int, known: int) -> str:
    return (
        f"{what} would forget {missing} of {known} items. Is a drive unplugged or the source "
        "moved? Nothing was forgotten. If this is intended, run `graph-me sync "
        "--allow-mass-forget`."
    )


def apply_blacklist(
    conn: sqlite3.Connection, cfg: BlacklistConfig, ctx: people.PeopleContext | None = None
) -> int:
    """Forget stored items the blacklist covers. Runs the full check only when it changed."""
    ctx = ctx or people.PeopleContext()
    digest = _config_hash({**cfg.model_dump(), "country_code": ctx.country_code})
    if db.get_meta(conn, "blacklist_hash") == digest:
        return 0
    ids = forget.blacklisted_item_ids(
        conn, cfg.paths, cfg.patterns, blacklisted_identities(cfg, ctx)
    )
    removed = forget.forget_items(conn, ids)
    db.set_meta(conn, "blacklist_hash", digest)
    conn.commit()
    return removed


@db.locked
def scan(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    only_source: str | None = None,
    workers: int | None = None,
    progress: Progress | None = None,
    since: datetime | None = None,
    reread: bool = False,
) -> ScanReport:
    """Add new items and update changed ones. Never forgets items missing from a source.

    ``reread``: read every item again, even unchanged ones (after an upgrade that learns more
    from the same items).

    ``since`` (aware datetime): read only items dated on or after it, where the connector can
    tell (``newer_than``), so a first scan gives answers fast. A later full scan reads the rest.
    """
    return _run(conn, cfg, only_source=only_source, workers=workers, progress=progress,
                since=since, reread=reread)  # fmt: skip


@db.locked
def sync(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    only_source: str | None = None,
    workers: int | None = None,
    progress: Progress | None = None,
    allow_mass_forget: bool = False,
) -> ScanReport:
    """Mirror the sources: add, update, and forget what they no longer have."""
    return _run(
        conn,
        cfg,
        only_source=only_source,
        workers=workers,
        progress=progress,
        forget_missing=True,
        allow_mass_forget=allow_mass_forget,
    )


def _run(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    only_source: str | None,
    workers: int | None,
    progress: Progress | None,
    forget_missing: bool = False,
    allow_mass_forget: bool = False,
    since: datetime | None = None,
    reread: bool = False,
) -> ScanReport:
    if only_source and only_source not in cfg.sources:
        raise ValueError(f"no source named {only_source!r} in config.yaml")
    fixes = _load_overrides(cfg)  # None when invalid: its source then fails and keeps them
    ctx = people.PeopleContext.from_config(cfg.people, fixes.not_same if fixes else ())
    report = ScanReport(forgotten_blacklisted=apply_blacklist(conn, cfg.blacklist, ctx))
    if fixes is not None:
        if not only_source:
            report.relinked = user.apply_changes(conn, fixes, ctx)
        elif user.changes_pending(conn, fixes, ctx):
            report.notes.append(
                "overrides.yaml changed who is who: run a scan or sync without --source to "
                "apply it fully."
            )
    report.backfilled = backfill_values(conn)
    sources = _sources(conn, cfg)
    if forget_missing and not only_source:
        _forget_removed_sources(conn, sources, report, allow_mass_forget)
    blacklist = _Blacklist(cfg.blacklist, ctx)
    workers_ = _Workers(workers if workers is not None else default_workers())
    try:
        for name, source in sources.items():
            # Corrections are tiny and apply to every source: read them on every run.
            if only_source and name not in (only_source, corrections.SOURCE):
                continue
            connector = registry.create(name, source, cfg.blacklist)
            conn.execute(
                """INSERT INTO sources(id, type, config_hash) VALUES (?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET type = excluded.type,
                                                 config_hash = excluded.config_hash""",
                (name, source.type, _config_hash(source.model_dump())),
            )
            stats = _scan_source(
                conn,
                name,
                connector,
                workers_,
                blacklist,
                progress,
                forget_missing=forget_missing,
                allow_mass_forget=allow_mass_forget,
                since=since if name != corrections.SOURCE else None,
                reread=reread,
            )
            report.sources[name] = stats
            if stats.failure is None:
                conn.execute(
                    "UPDATE sources SET last_sync_at = ? WHERE id = ?",
                    (datetime.now(UTC).isoformat(timespec="seconds"), name),
                )
            conn.commit()
    finally:
        workers_.close()
    _save_last_run(conn, report, "sync" if forget_missing else "scan")
    # Re-processed, forgotten or blacklisted items may have left entities, facts or relations.
    finish(conn, user.rejections(fixes, ctx) if fixes else ())
    prune_log(conn, cfg.log.keep_days)
    db.compact(conn)
    return report


_RULES_META = "tier0_rules"


def backfill_values(conn: sqlite3.Connection) -> int:
    """Apply new or changed deadline and amount rules to items already stored.

    Their text is in ``chunks``, so sources aren't read again. Contact cards, whose new fields
    (address, anniversary, job title) come from the card itself, are marked to be read again.
    Returns how many items were looked at; 0 when the rules didn't change.
    """
    current = extract.rules_version()
    stored = db.get_meta(conn, _RULES_META)
    if stored == current:
        return 0
    if stored is None and not conn.execute("SELECT 1 FROM items LIMIT 1").fetchone():
        db.set_meta(conn, _RULES_META, current)  # a new index: items get the rules as read
        conn.commit()
        return 0
    conn.execute(
        "DELETE FROM evidence WHERE method LIKE 'rule:%.deadline' OR method LIKE 'rule:%.amount'"
    )
    rows = conn.execute(
        """SELECT i.*, group_concat(c.text, ' ') AS body FROM items i
           JOIN (SELECT item_id, text FROM chunks WHERE tier = 0 ORDER BY item_id, ord) c
             ON c.item_id = i.id
           WHERE i.kind IN ('file', 'email', 'message') GROUP BY i.id"""
    ).fetchall()
    for row in rows:
        day = row["created_at"][:10] if row["created_at"] and row["kind"] != "file" else None
        values = extract.extract(row["body"] or "", date.fromisoformat(day) if day else None)
        if not values:
            continue
        doc = conn.execute(
            "SELECT entity_id FROM mentions WHERE item_id = ? AND role = 'file'", (row["id"],)
        ).fetchone()
        item = Item(external_id=row["external_id"], version=row["version"] or "",
                    kind=row["kind"], title=row["title"])  # fmt: skip
        entities.value_facts(conn, row["id"], item, doc[0] if doc else None, values)
    conn.execute("UPDATE items SET version = NULL WHERE kind = 'contact'")
    db.set_meta(conn, _RULES_META, current)
    conn.commit()
    return len(rows)


def prune_log(conn: sqlite3.Connection, keep_days: int) -> int:
    """Drop activity-log entries older than ``log.keep_days``."""
    cutoff = (datetime.now(UTC) - timedelta(days=keep_days)).isoformat(timespec="seconds")
    removed = conn.execute("DELETE FROM query_log WHERE ts < ?", (cutoff,)).rowcount
    conn.commit()
    return removed


def _load_overrides(cfg: Config) -> corrections.Overrides | None:
    if cfg.overrides is None:
        return corrections.Overrides()
    try:
        return corrections.load(cfg.overrides)
    except (OSError, ValueError, yaml.YAMLError):  # the _overrides source reports it
        return None


def _sources(conn: sqlite3.Connection, cfg: Config) -> dict[str, SourceConfig]:
    """config.yaml sources, plus ``_overrides`` when overrides.yaml exists or did exist."""
    sources = dict(cfg.sources)
    known = conn.execute("SELECT 1 FROM sources WHERE id = ?", (corrections.SOURCE,)).fetchone()
    if cfg.overrides is not None and (cfg.overrides.exists() or known):
        sources[corrections.SOURCE] = SourceConfig(type="overrides", path=str(cfg.overrides))
    return sources


def _save_last_run(conn: sqlite3.Connection, report: ScanReport, mode: str) -> None:
    """Keep each source's latest counts for the status page (blacklist hits, flagged items...)."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    last = json.loads(db.get_meta(conn, "last_run") or "{}")
    known = {r[0] for r in conn.execute("SELECT id FROM sources")}
    sources = {k: v for k, v in last.get("sources", {}).items() if k in known}
    for name, stats in report.sources.items():
        sources[name] = {**asdict(stats), "errors": len(stats.errors), "at": now, "mode": mode}
    last = {
        "at": now,
        "mode": mode,
        "forgotten_blacklisted": report.forgotten_blacklisted,
        "sources": sources,
    }
    db.set_meta(conn, "last_run", json.dumps(last))
    conn.commit()


def _forget_removed_sources(
    conn: sqlite3.Connection,
    sources: dict[str, SourceConfig],
    report: ScanReport,
    allow_mass_forget: bool,
) -> None:
    """Sources removed from config.yaml: forget their items (guarded like any mass forget)."""
    for (name,) in conn.execute("SELECT id FROM sources").fetchall():
        if name in sources:
            continue
        ids = [r[0] for r in conn.execute("SELECT id FROM items WHERE source_id = ?", (name,))]
        if len(ids) > MASS_FORGET_MIN and not allow_mass_forget:
            report.failures[name] = _guard_message(
                f"source {name!r} is no longer in config.yaml: removing it", len(ids), len(ids)
            )
            continue
        report.removed_sources[name] = forget.forget_items(conn, ids)
        conn.execute("DELETE FROM sources WHERE id = ?", (name,))
        conn.commit()


def finish(conn: sqlite3.Connection, rejected=()) -> None:
    """Whole-graph steps after items changed: rejected facts, orphans, relation weights,
    fact confidence. ``rejected``: overrides.yaml ``reject`` entries (see tier0/overrides.py).
    """
    user.apply_rejections(conn, rejected)
    forget.cleanup_orphans(conn)
    conn.execute(
        """UPDATE relations SET weight =
           (SELECT count(*) FROM evidence WHERE evidence.relation_id = relations.id)"""
    )
    facts.recompute_confidence(conn)
    conn.commit()


def _config_hash(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _scan_source(
    conn: sqlite3.Connection,
    source: str,
    connector: Connector,
    workers: _Workers,
    blacklist: _Blacklist,
    progress: Progress | None,
    *,
    forget_missing: bool = False,
    allow_mass_forget: bool = False,
    since: datetime | None = None,
    reread: bool = False,
) -> SourceStats:
    stats = SourceStats()
    newer_than = getattr(connector, "newer_than", None) if since else None
    known = dict(
        conn.execute("SELECT external_id, version FROM items WHERE source_id = ?", (source,))
    )
    todo, listed = [], set()
    try:
        # List everything first: if the source is unreachable, nothing has changed yet.
        for external_id, version in connector.list_ids():
            stats.seen += 1
            listed.add(external_id)
            if known.get(external_id) == version and not reread:
                stats.unchanged += 1
            elif newer_than and not newer_than(external_id, version, since):
                stats.older += 1
            else:
                todo.append(external_id)
    except SourceUnavailable as exc:
        return SourceStats(failure=str(exc))

    if forget_missing:
        missing = [eid for eid in known if eid not in listed]
        # Items the config now leaves out still exist: forgetting them is no sign of a lost source.
        excluded = getattr(connector, "excluded", None)
        gone = [eid for eid in missing if not excluded(eid)] if excluded else missing
        if gone and _too_many(len(gone), len(known)) and not allow_mass_forget:
            return SourceStats(
                seen=stats.seen,
                failure=_guard_message(f"source {source!r}", len(gone), len(known)),
            )
        stats.forgotten = forget.forget_items(conn, (item_id(source, eid) for eid in missing))
        conn.commit()

    for start in range(0, len(todo), BATCH):
        batch = list(connector.fetch(todo[start : start + BATCH]))
        jobs = [
            (
                str(i.path) if i.text is None and i.path else None,
                i.text,
                i.created_at.date().isoformat() if i.created_at and i.kind != "file" else None,
            )
            for i in batch
        ]
        for item, prepared in zip(batch, workers.map(jobs), strict=True):
            if prepared.error:
                stats.parse_errors += 1
                if len(stats.errors) < MAX_ERRORS_KEPT:
                    stats.errors.append(f"{item.uri or item.external_id}: {prepared.error}")
            _store(
                conn, source, item, prepared, blacklist, stats, existed=item.external_id in known
            )
        conn.commit()
        if progress:
            progress(source, min(start + BATCH, len(todo)), len(todo))
    return stats


def _store(
    conn: sqlite3.Connection,
    source: str,
    item: Item,
    prepared: Prepared,
    blacklist: _Blacklist,
    stats: SourceStats,
    *,
    existed: bool,
) -> None:
    iid = item_id(source, item.external_id)
    # Re-processing replaces the item; cascades drop its chunks, mentions and evidence.
    conn.execute("DELETE FROM items WHERE id = ?", (iid,))
    if (
        blacklist.blocks(prepared.text)
        or blacklist.blocks(item.title)
        or blacklist.blocks_people(item)
    ):
        stats.blacklisted += 1
        return

    clean_title = sanitize.sanitize(item.title or "")
    title = clean_title.text or None
    risk = max(prepared.risk, clean_title.risk_score)
    if risk:
        stats.flagged += 1

    conn.execute(
        """INSERT INTO items(id, source_id, external_id, version, kind, title, uri, thread_id,
                             created_at, modified_at, content_hash, lang, trust, risk_score, tier)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
        (
            iid,
            source,
            item.external_id,
            item.version,
            item.kind,
            title,
            item.uri,
            item.thread_id,
            item.created_at.isoformat() if item.created_at else None,
            item.modified_at.isoformat() if item.modified_at else None,
            item.content_hash,
            prepared.lang,
            item.trust,
            risk,
        ),
    )
    conn.executemany(
        "INSERT INTO chunks(item_id, ord, text, tokens) VALUES (?, ?, ?, ?)",
        [(iid, n, body, tokens) for n, (body, tokens) in enumerate(prepared.chunks)],
    )
    conn.executemany(
        "INSERT INTO item_attachments(item_id, content_hash, filename, mime_type) "
        "VALUES (?, ?, ?, ?)",
        [(iid, a.content_hash, a.filename, a.mime_type) for a in item.attachments],
    )
    if item.kind == "file":
        doc = entities.file_entities(conn, iid, item)
    else:
        people.item_people(conn, iid, item, blacklist.ctx)
        doc = None
    if prepared.values and item.kind in ("file", "email", "message"):
        entities.value_facts(conn, iid, item, doc, prepared.values)

    if existed:
        stats.updated += 1
    else:
        stats.added += 1
