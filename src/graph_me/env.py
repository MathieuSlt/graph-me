"""Runtime environment checks reported by ``graph-me where``."""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Environment:
    python: str
    python_version: str
    uv_managed: bool
    sqlite_version: str
    fts5: bool
    load_extension: bool


def _uv_python_dirs() -> list[Path]:
    dirs = []
    if custom := os.environ.get("UV_PYTHON_INSTALL_DIR"):
        dirs.append(Path(custom))
    xdg = os.environ.get("XDG_DATA_HOME")
    dirs.append(Path(xdg) / "uv" / "python" if xdg else Path("~/.local/share/uv/python"))
    return [d.expanduser().resolve() for d in dirs]


def is_uv_managed(base_prefix: str | None = None) -> bool:
    """True when the interpreter behind this process (venv or not) was installed by uv."""
    prefix = Path(base_prefix or sys.base_prefix).resolve()
    # uv installs each interpreter as <data dir>/uv/python/<cpython-...>/
    if prefix.parent.name == "python" and prefix.parent.parent.name == "uv":
        return True
    return any(prefix.is_relative_to(d) for d in _uv_python_dirs())


def check() -> Environment:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        fts5 = True
    except sqlite3.OperationalError:
        fts5 = False
    load_extension = hasattr(conn, "enable_load_extension")
    conn.close()
    return Environment(
        python=sys.executable,
        python_version=sys.version.split()[0],
        uv_managed=is_uv_managed(),
        sqlite_version=sqlite3.sqlite_version,
        fts5=fts5,
        load_extension=load_extension,
    )
