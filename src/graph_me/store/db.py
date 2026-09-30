"""SQLite connection and schema migrations for ``graph.db``."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 2


def _migrations() -> dict[int, str]:
    """Version 1 is the base schema; later versions are migrations/NNN_*.sql."""
    store = resources.files("graph_me.store")
    scripts = {1: store.joinpath("schema.sql").read_text(encoding="utf-8")}
    for entry in store.joinpath("migrations").iterdir():
        if entry.name.endswith(".sql"):
            scripts[int(entry.name.split("_", 1)[0])] = entry.read_text(encoding="utf-8")
    return scripts


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Open ``graph.db``, applying pending migrations unless read-only."""
    target = f"file:{path}?mode=ro" if readonly else f"file:{path}"
    conn = sqlite3.connect(target, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        migrate(conn)
    return conn


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
