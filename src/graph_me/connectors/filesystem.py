"""Filesystem connector: text-like files under the configured paths.

Config keys (under ``sources.<name>``):

- ``paths``: folders (or single files) to index
- ``exclude``: glob patterns for noise; ``**`` crosses folders. Defaults to DEFAULT_EXCLUDE
  when omitted. Nothing personal is excluded by default: use the blacklist for that.
- ``trust``: ``self`` (default: your own files) or ``untrusted`` (e.g. a Downloads folder).
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from functools import cache
from pathlib import Path

from graph_me.config import BlacklistConfig, SourceConfig
from graph_me.connectors.base import Item
from graph_me.pipeline.parse import is_supported

DEFAULT_EXCLUDE = (
    "**/.git/**",
    "**/node_modules/**",
    "**/.venv/**",
    "**/venv/**",
    "**/__pycache__/**",
    "**/.cache/**",
    "**/.Trash/**",
    "**/graph-out/**",
)


@cache
def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a glob with ``**`` into a regex matched against a full POSIX path."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def _file_version(st: os.stat_result) -> str:
    return f"{st.st_mtime_ns}:{st.st_size}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class FilesystemConnector:
    type = "filesystem"

    def __init__(self, name: str, source: SourceConfig, blacklist: BlacklistConfig) -> None:
        extra = source.model_extra or {}
        self.name = name
        self.roots = [Path(p).expanduser().resolve() for p in extra.get("paths", [])]
        self.exclude = [glob_to_regex(p) for p in extra.get("exclude", DEFAULT_EXCLUDE)]
        self.blocked = [Path(p).expanduser().resolve() for p in blacklist.paths]
        self.trust = extra.get("trust", "self")
        if self.trust not in ("self", "known", "untrusted"):
            raise ValueError(f"source {name!r}: trust must be self, known or untrusted")

    def _excluded(self, path: Path, is_dir: bool = False) -> bool:
        posix = path.as_posix() + ("/" if is_dir else "")
        # a folder is excluded when a file inside it would be
        probe = posix + "x" if is_dir else posix
        if any(rx.fullmatch(probe) for rx in self.exclude):
            return True
        return any(path == b or path.is_relative_to(b) for b in self.blocked)

    def _walk(self) -> Iterator[Path]:
        seen: set[Path] = set()
        for root in self.roots:
            if root.is_file():
                candidates: Iterable[Path] = [root]
            elif root.is_dir():
                candidates = self._walk_dir(root)
            else:
                continue
            for path in candidates:
                if path not in seen and is_supported(path) and not self._excluded(path):
                    seen.add(path)
                    yield path

    def _walk_dir(self, root: Path) -> Iterator[Path]:
        # followlinks=False: no symlink loops, no escaping the configured folders
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            here = Path(dirpath)
            dirnames[:] = sorted(d for d in dirnames if not self._excluded(here / d, is_dir=True))
            for name in sorted(filenames):
                path = here / name
                if not path.is_symlink():
                    yield path

    def list_ids(self) -> Iterator[tuple[str, str]]:
        for path in self._walk():
            try:
                yield str(path), _file_version(path.stat())
            except OSError:
                continue

    def fetch(self, external_ids: Iterable[str]) -> Iterator[Item]:
        for external_id in external_ids:
            path = Path(external_id)
            try:
                st = path.stat()
                digest = sha256_file(path)
            except OSError:
                continue
            yield Item(
                external_id=external_id,
                version=_file_version(st),
                kind="file",
                title=path.name,
                uri=str(path),
                path=path,
                modified_at=datetime.fromtimestamp(st.st_mtime, tz=UTC),
                content_hash=digest,
                trust=self.trust,
                extra={"root": str(self._root_of(path)), "size": st.st_size},
            )

    def _root_of(self, path: Path) -> Path:
        for root in self.roots:
            if path == root or path.is_relative_to(root):
                return root if root.is_dir() else root.parent
        return path.parent
