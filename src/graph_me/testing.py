"""Checks for connector authors: does your connector keep graph-me's promises?

In your plugin's tests::

    from graph_me.testing import check_connector

    def test_contract(tmp_path):
        make_some_data(tmp_path)
        check_connector(lambda: MyConnector("mine", SourceConfig(type="mine", path=...),
                                            BlacklistConfig()),
                        unreachable=lambda: MyConnector(..., path=tmp_path / "missing"))

``check_connector`` raises ``AssertionError`` with a plain explanation when a rule is broken. It
needs no test framework. The rules (docs/contributing-connectors.md):

1. ``list_ids`` yields unique ``(external_id, version)`` string pairs, the same ones twice in a row
   when nothing changed (graph-me re-reads an item only when its version changes).
2. ``fetch`` returns valid items for listed ids, with the listed version, and skips ids it
   doesn't know (an item deleted between the two calls).
3. A source that can't be reached raises ``SourceUnavailable`` instead of listing nothing, so
   ``sync`` never forgets everything because a drive is unplugged.
4. Items need a ``text`` or a ``path`` to read, a kind graph-me knows, and aware dates.
"""

from __future__ import annotations

from collections.abc import Callable

from graph_me.connectors.base import Connector, Item, SourceUnavailable


def check_connector(
    make: Callable[[], Connector],
    *,
    unreachable: Callable[[], Connector] | None = None,
    min_items: int = 1,
) -> list[Item]:
    """Run the connector contract. Returns the fetched items for your own extra checks."""
    connector = make()
    for attr in ("type", "list_ids", "fetch"):
        assert hasattr(connector, attr), f"a connector needs `{attr}`"
    listed = list(connector.list_ids())
    assert len(listed) >= min_items, f"expected at least {min_items} item(s), listed {len(listed)}"
    for pair in listed:
        assert isinstance(pair, tuple) and len(pair) == 2, f"list_ids yields pairs, not {pair!r}"
        external_id, version = pair
        assert isinstance(external_id, str) and external_id, f"bad external id {external_id!r}"
        assert isinstance(version, str) and version, f"{external_id}: version must be a string"
    ids = [i for i, _ in listed]
    assert len(set(ids)) == len(ids), "list_ids yielded the same external id twice"
    versions = dict(listed)

    again = dict(make().list_ids())
    assert again == versions, (
        "listing twice gave different ids or versions: versions must only change when the "
        "item changes, or graph-me re-reads everything on every sync"
    )

    items = list(connector.fetch(ids))
    assert {i.external_id for i in items} <= set(ids), "fetch returned ids it wasn't asked for"
    assert len(items) == len(ids), f"fetch returned {len(items)} of {len(ids)} listed items"
    for item in items:
        Item.model_validate(item.model_dump())  # still valid after a round trip
        assert item.version == versions[item.external_id], (
            f"{item.external_id}: fetch says version {item.version!r}, "
            f"list_ids said {versions[item.external_id]!r}"
        )
        assert item.text is not None or item.path is not None, (
            f"{item.external_id}: give the item a text, or a path for graph-me to parse"
        )
        for name in ("created_at", "modified_at"):
            when = getattr(item, name)
            assert when is None or when.tzinfo is not None, (
                f"{item.external_id}: {name} must be timezone-aware"
            )
    assert list(make().fetch(["no-such-id-graph-me-contract"])) == [], (
        "fetch must skip ids it doesn't know (deleted since they were listed)"
    )

    if unreachable is not None:
        try:
            gone = list(unreachable().list_ids())
        except SourceUnavailable:
            pass
        else:
            raise AssertionError(
                f"an unreachable source listed {len(gone)} item(s) instead of raising "
                "SourceUnavailable: sync would forget everything it had indexed"
            )
    return items
