"""Find connector classes: built-ins plus the ``graph_me.connectors`` entry-point group."""

from __future__ import annotations

from importlib.metadata import entry_points

from graph_me.config import BlacklistConfig, SourceConfig
from graph_me.connectors.base import Connector


def _builtins() -> dict[str, type]:
    from graph_me.connectors.filesystem import FilesystemConnector
    from graph_me.connectors.msgvault import MsgvaultConnector
    from graph_me.connectors.vcard import VcardConnector

    return {
        "filesystem": FilesystemConnector,
        "msgvault": MsgvaultConnector,
        "vcard": VcardConnector,
    }


_registered: dict[str, type] = {}


def register(type_name: str, cls: type) -> None:
    """Register a connector class in-process (tests, embedding). Plugins use entry points."""
    _registered[type_name] = cls


def available() -> dict[str, type]:
    found = {**_builtins(), **_registered}
    for ep in entry_points(group="graph_me.connectors"):
        found.setdefault(ep.name, ep.load())
    return found


def create(name: str, source: SourceConfig, blacklist: BlacklistConfig) -> Connector:
    classes = available()
    if source.type not in classes:
        known = ", ".join(sorted(classes))
        raise ValueError(f"source {name!r}: unknown type {source.type!r} (known: {known})")
    return classes[source.type](name, source, blacklist)
