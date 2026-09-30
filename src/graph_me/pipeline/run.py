"""Tier 0 scan: connectors -> parse -> blacklist -> sanitize -> language -> chunks -> entities.

A scan processes new and changed items (by connector ``version``) and leaves unchanged ones
alone. When the blacklist changed since the last scan, stored items it now covers are forgotten
first. Forgetting items that disappeared from their source is the job of ``sync`` (M3).
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
from graph_me.connectors.base import Connector, Item
from graph_me.pipeline import chunk, lang, parse, sanitize
from graph_me.pipeline.tier0 import entities
from graph_me.store import db, forget

BATCH = 500
MAX_ERRORS_KEPT = 20


@dataclass
class SourceStats:
    seen: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    blacklisted: int = 0
    parse_errors: int = 0
    flagged: int = 0  # items with a non-zero injection risk score
    errors: list[str] = field(default_factory=list)


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
    def __init__(self, cfg: BlacklistConfig) -> None:
        self.patterns = [re.compile(p, re.IGNORECASE) for p in cfg.patterns]

    def blocks(self, text: str | None) -> bool:
        return bool(text) and any(p.search(text) for p in self.patterns)


def default_workers() -> int:
    return max(1, min(8, (os.cpu_count() or 2) - 1))


@dataclass
class ScanReport:
    sources: dict[str, SourceStats] = field(default_factory=dict)
    forgotten_blacklisted: int = 0  # stored items removed because the blacklist now covers them


def apply_blacklist(conn: sqlite3.Connection, cfg: BlacklistConfig) -> int:
    """Forget stored items the blacklist covers. Runs the full check only when it changed."""
    digest = _config_hash(cfg.model_dump())
    if db.get_meta(conn, "blacklist_hash") == digest:
        return 0
    ids = forget.blacklisted_item_ids(conn, cfg.paths, cfg.patterns)
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
    if only_source and only_source not in cfg.sources:
        raise ValueError(f"no source named {only_source!r} in config.yaml")
    report = ScanReport(forgotten_blacklisted=apply_blacklist(conn, cfg.blacklist))
    blacklist = _Blacklist(cfg.blacklist)
    workers_ = _Workers(workers if workers is not None else default_workers())
    results = report.sources
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
            results[name] = _scan_source(conn, name, connector, workers_, blacklist, progress)
            conn.execute(
                "UPDATE sources SET last_sync_at = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(timespec="seconds"), name),
            )
            conn.commit()
    finally:
        workers_.close()
    # Re-processed or newly blacklisted items may have left entities, facts or relations behind.
    forget.cleanup_orphans(conn)
    conn.commit()
    return report


def _config_hash(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _scan_source(
    conn: sqlite3.Connection,
    source: str,
    connector: Connector,
    workers: _Workers,
    blacklist: _Blacklist,
    progress: Progress | None,
) -> SourceStats:
    stats = SourceStats()
    known = dict(
        conn.execute("SELECT external_id, version FROM items WHERE source_id = ?", (source,))
    )
    todo = []
    for external_id, version in connector.list_ids():
        stats.seen += 1
        if known.get(external_id) == version:
            stats.unchanged += 1
        else:
            todo.append(external_id)

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
    if blacklist.blocks(prepared.text) or blacklist.blocks(item.title):
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
    if item.kind == "file":
        entities.file_entities(conn, iid, item)

    if existed:
        stats.updated += 1
    else:
        stats.added += 1
