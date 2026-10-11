"""SQLite connection and schema migrations for ``graph.db``."""

from __future__ import annotations

import contextlib
import fcntl
import functools
import os
import sqlite3
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from urllib.parse import quote

LOCK_NAME = ".run.lock"


class RunLocked(RuntimeError):
    """Another scan, sync, enrich or ingest is writing to this index."""


def _migrations() -> dict[int, str]:
    """Version 1 is the base schema; later versions are migrations/NNN_*.sql."""
    store = resources.files("graph_me.store")
    scripts = {1: store.joinpath("schema.sql").read_text(encoding="utf-8")}
    for entry in store.joinpath("migrations").iterdir():
        if entry.name.endswith(".sql"):
            scripts[int(entry.name.split("_", 1)[0])] = entry.read_text(encoding="utf-8")
    return scripts


SCHEMA_VERSION = max(_migrations())  # the newest migration file


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Open ``graph.db``, applying pending migrations unless read-only."""
    target = f"file:{quote(str(path))}" + ("?mode=ro" if readonly else "")
    conn = sqlite3.connect(target, uri=True)
    conn.row_factory = sqlite3.Row
    load_vec(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        migrate(conn)
    return conn


COMPACT_MIN_FREE_BYTES = 8 * 1024 * 1024
COMPACT_MIN_FREE_SHARE = 0.25


def compact(conn: sqlite3.Connection) -> bool:
    """VACUUM when forgetting left a lot of empty pages (SQLite never shrinks the file itself).

    Runs only when at least 8 MB and a quarter of the file are free, so small syncs stay fast.
    """
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    size = conn.execute("PRAGMA page_size").fetchone()[0]
    if free * size < COMPACT_MIN_FREE_BYTES or free < COMPACT_MIN_FREE_SHARE * pages:
        return False
    conn.commit()
    conn.execute("VACUUM")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return True


def load_vec(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec (Tier 1 vectors) when installed and this Python allows extensions."""
    try:
        import sqlite_vec
    except ImportError:
        return False
    if not hasattr(conn, "enable_load_extension"):
        return False
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
    except sqlite3.Error:
        return False
    finally:
        conn.enable_load_extension(False)
    return True


@contextlib.contextmanager
def run_lock(conn: sqlite3.Connection) -> Iterator[None]:
    """Hold ``.run.lock`` next to graph.db, so a scheduled sync and a manual one never overlap.

    Fails at once (``RunLocked``) instead of waiting. In-memory databases need no lock.
    """
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    if not path:
        yield
        return
    fd = os.open(Path(path).with_name(LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunLocked(
                "Another scan, sync or enrich is running on this index. Try again when it ends."
            ) from None
        yield
    finally:
        os.close(fd)  # closing releases the lock


def locked(fn):
    """Run ``fn(conn, ...)`` under ``run_lock(conn)``."""

    @functools.wraps(fn)
    def wrapper(conn: sqlite3.Connection, *args, **kwargs):
        with run_lock(conn):
            return fn(conn, *args, **kwargs)

    return wrapper


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every migration newer than the database's version. Returns the final version."""
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"graph.db has schema version {current}, newer than this graph-me "
            f"({SCHEMA_VERSION}). Upgrade graph-me."
        )
    for version, script in sorted(_migrations().items()):
        if version <= current:
            continue
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(version),)
        )
        conn.commit()
    return schema_version(conn)


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = ("sources", "items", "chunks", "entities", "relations", "facts")
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
