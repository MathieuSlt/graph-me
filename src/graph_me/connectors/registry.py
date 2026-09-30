"""Find connector classes: built-ins plus the ``graph_me.connectors`` entry-point group."""

from __future__ import annotations

from importlib.metadata import entry_points

from graph_me.config import BlacklistConfig, SourceConfig
from graph_me.connectors.base import Connector


def _builtins() -> dict[str, type]:
    from graph_me.connectors.filesystem import FilesystemConnector

    return {"filesystem": FilesystemConnector}


def available() -> dict[str, type]:
    found = _builtins()
    for ep in entry_points(group="graph_me.connectors"):
        found.setdefault(ep.name, ep.load())
    return found


def create(name: str, source: SourceConfig, blacklist: BlacklistConfig) -> Connector:
    classes = available()
    if source.type not in classes:
        known = ", ".join(sorted(classes))
        raise ValueError(f"source {name!r}: unknown type {source.type!r} (known: {known})")
    return classes[source.type](name, source, blacklist)
