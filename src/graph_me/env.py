"""Runtime environment checks reported by ``graph-me where`` and ``graph-me doctor``, and the
install commands graph-me suggests."""

from __future__ import annotations

import importlib.util
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


# Optional parts and a module each one brings (installed means importable here).
EXTRAS: dict[str, tuple[str, ...]] = {
    "medium": ("sqlite_vec", "fastembed"),
    "ui": ("uvicorn", "starlette", "jinja2"),
    "ocr": ("rapidocr",),
    "macos": ("EventKit",),
    "ner": ("gliner",),
}


def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def installed_extras() -> list[str]:
    return [name for name, modules in EXTRAS.items() if all(map(_importable, modules))]


def install_command(*add: str) -> str:
    """The command that adds ``add`` while keeping the extras already installed.

    ``uv tool install`` replaces the extras: suggesting ``graph-me[ocr]`` to someone who has
    ``[medium,ui]`` would remove those. So every hint lists them all.
    """
    wanted = set(installed_extras()) | set(add)
    extras = [name for name in EXTRAS if name in wanted]
    spec = f"graph-me[{','.join(extras)}]" if extras else "graph-me"
    return f'uv tool install --managed-python "{spec}"'


def loads_extensions() -> bool:
    """Some Pythons (often a system one) are built without SQLite extension loading."""
    return hasattr(sqlite3.Connection, "enable_load_extension")


def vec_problem() -> str | None:
    """Why sqlite-vec (meaning-based search) can't be used here, or None when it can."""
    if not _importable("sqlite_vec"):
        return f"the [medium] extra is missing. Install it with: {install_command('medium')}"
    if not loads_extensions():
        return (
            "this Python can't load SQLite extensions, which sqlite-vec needs. Install graph-me "
            f"on a Python managed by uv: {install_command('medium')}"
        )
    import sqlite_vec

    conn = sqlite3.connect(":memory:")
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
    except (sqlite3.Error, AttributeError, OSError) as exc:
        return f"sqlite-vec is installed but could not be loaded ({exc})"
    finally:
        conn.close()
    return None
