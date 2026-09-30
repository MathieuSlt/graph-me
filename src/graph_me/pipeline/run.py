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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from graph_me.config import BlacklistConfig, Config
from graph_me.connectors import registry
from graph_me.connectors.base import Connector, Item, Party, SourceUnavailable
from graph_me.pipeline import chunk, lang, parse, sanitize
from graph_me.pipeline.tier0 import entities, facts, people
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
    error: str | None = None


def _prepare(job: tuple[str | None, str | None]) -> Prepared:
    """Worker entry point: parse (if needed), sanitize, detect language, chunk.

    ``job`` is ``(path, text)``: exactly one is set. Top-level so it can run in a process pool.
    """
    path, text = job
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
        error=error,
    )


class _Workers:
    """Prepares items inline (workers=1) or in a process pool."""

    def __init__(self, workers: int) -> None:
        self.pool = ProcessPoolExecutor(workers) if workers > 1 else None

    def map(self, jobs: list[tuple[str | None, str | None]]) -> Iterator[Prepared]:
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


def scan(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    only_source: str | None = None,
    workers: int | None = None,
    progress: Progress | None = None,
) -> ScanReport:
    """Add new items and update changed ones. Never forgets items missing from a source."""
    return _run(conn, cfg, only_source=only_source, workers=workers, progress=progress)


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
) -> ScanReport:
    if only_source and only_source not in cfg.sources:
        raise ValueError(f"no source named {only_source!r} in config.yaml")
    ctx = people.PeopleContext.from_config(cfg.people)
    report = ScanReport(forgotten_blacklisted=apply_blacklist(conn, cfg.blacklist, ctx))
    if forget_missing and not only_source:
        _forget_removed_sources(conn, cfg, report, allow_mass_forget)
    blacklist = _Blacklist(cfg.blacklist, ctx)
    workers_ = _Workers(workers if workers is not None else default_workers())
    try:
        for name, source in cfg.sources.items():
            if only_source and name != only_source:
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
    # Re-processed, forgotten or blacklisted items may have left entities, facts or relations.
    finish(conn)
    return report


def _forget_removed_sources(
    conn: sqlite3.Connection, cfg: Config, report: ScanReport, allow_mass_forget: bool
) -> None:
    """Sources removed from config.yaml: forget their items (guarded like any mass forget)."""
    for (name,) in conn.execute("SELECT id FROM sources").fetchall():
        if name in cfg.sources:
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


def finish(conn: sqlite3.Connection) -> None:
    """Whole-graph steps after items changed: orphans, relation weights, fact confidence."""
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
) -> SourceStats:
    stats = SourceStats()
    known = dict(
        conn.execute("SELECT external_id, version FROM items WHERE source_id = ?", (source,))
    )
    todo, listed = [], set()
    try:
        # List everything first: if the source is unreachable, nothing has changed yet.
        for external_id, version in connector.list_ids():
            stats.seen += 1
            listed.add(external_id)
            if known.get(external_id) == version:
                stats.unchanged += 1
            else:
                todo.append(external_id)
    except SourceUnavailable as exc:
        return SourceStats(failure=str(exc))

    if forget_missing:
        missing = [eid for eid in known if eid not in listed]
        if missing and _too_many(len(missing), len(known)) and not allow_mass_forget:
            return SourceStats(
                seen=stats.seen,
                failure=_guard_message(f"source {source!r}", len(missing), len(known)),
            )
        stats.forgotten = forget.forget_items(conn, (item_id(source, eid) for eid in missing))
        conn.commit()

    for start in range(0, len(todo), BATCH):
        batch = list(connector.fetch(todo[start : start + BATCH]))
        jobs = [(str(i.path) if i.text is None and i.path else None, i.text) for i in batch]
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
        entities.file_entities(conn, iid, item)
    else:
        people.item_people(conn, iid, item, blacklist.ctx)

    if existed:
        stats.updated += 1
    else:
        stats.added += 1
