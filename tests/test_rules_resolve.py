from datetime import UTC, datetime

import pytest

from graph_me.connectors.base import Item
from graph_me.pipeline import resolve
from graph_me.pipeline.tier0 import facts
from graph_me.pipeline.tier0.entities import relate


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        ("Joyeux anniv Soeurette !!", "fr"),
        ("JOYEUX ANNIVERSAIRE !", "fr"),
        ("Happy birthday mate", "en"),
        ("Joyeux anniversaire en retard !", None),
        ("Sorry for the belated happy birthday", None),
        ("Quel est le programme pour l'anniversaire de Paul ?", None),
        (" ".join(["mot"] * 60) + " joyeux anniversaire", None),  # quoted history, far down
    ],
)
def test_find_greeting(text, lang):
    assert facts.find_greeting(text) == lang


def test_greeting_uses_local_date_and_needs_one_recipient(conn):
    from zoneinfo import ZoneInfo

    conn.execute("INSERT INTO sources(id, type) VALUES ('s', 'x')")
    conn.execute(
        "INSERT INTO items(id, source_id, external_id, kind) VALUES ('i', 's', 'e', 'message')"
    )
    for eid in ("p1", "p2"):
        conn.execute("INSERT INTO entities(id, kind) VALUES (?, 'person')", (eid,))
    # 23:30 UTC on March 11 is already March 12 in Paris (UTC+1): the rule uses local time.
    item = Item(external_id="e", version="1", kind="message", text="Joyeux anniversaire !",
                created_at=datetime(2024, 3, 11, 23, 30, tzinfo=UTC))  # fmt: skip
    paris = ZoneInfo("Europe/Paris")
    facts.greeting_facts(conn, "i", item, ["p1", "p2"], paris)
    assert conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 0
    facts.greeting_facts(conn, "i", item, ["p1"], paris)
    facts.greeting_facts(conn, "i", item, ["p2"], ZoneInfo("UTC"))
    rows = dict(conn.execute("SELECT entity_id, value FROM facts").fetchall())
    assert rows == {"p1": "03-12", "p2": "03-11"}  # same instant, two time zones


def test_merge_folds_facts_relations_and_aliases(conn):
    conn.execute("INSERT INTO sources(id, type) VALUES ('s', 'x')")
    for iid in ("i1", "i2"):
        conn.execute(
            "INSERT INTO items(id, source_id, external_id, kind) VALUES (?, 's', ?, 'message')",
            (iid, iid),
        )
    for eid, name in (("a", None), ("b", "Sophie"), ("c", "Jean")):
        conn.execute("INSERT INTO entities(id, kind, name) VALUES (?, 'person', ?)", (eid, name))
    conn.execute("INSERT INTO aliases VALUES ('a', 'phone', '+33612345678')")
    conn.execute("INSERT INTO aliases VALUES ('b', 'email', 'sophie@example.com')")
    facts.upsert_fact(conn, "a", "birthday", "03-12", 0.5, "i1", "rule:fr.birthday_greeting")
    facts.upsert_fact(conn, "b", "birthday", "03-12", 0.95, "i2", "contact:bday")
    relate(conn, "c", "a", "wrote_to", "i1", "tier0:message")
    relate(conn, "c", "b", "wrote_to", "i2", "tier0:message")
    relate(conn, "a", "b", "wrote_to", "i1", "tier0:message")  # becomes a self-loop

    resolve.merge(conn, "a", "b")

    assert conn.execute("SELECT name FROM entities WHERE id = 'a'").fetchone()[0] == "Sophie"
    assert conn.execute("SELECT count(*) FROM entities WHERE id = 'b'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM aliases WHERE entity_id = 'a'").fetchone()[0] == 2
    [(fact_id, confidence)] = conn.execute("SELECT id, confidence FROM facts").fetchall()
    assert confidence == 0.95
    assert (
        conn.execute("SELECT count(*) FROM evidence WHERE fact_id = ?", (fact_id,)).fetchone()[0]
        == 2
    )
    rels = conn.execute("SELECT src, dst FROM relations").fetchall()
    assert [tuple(r) for r in rels] == [("c", "a")]
    assert (
        conn.execute("SELECT count(*) FROM evidence WHERE relation_id IS NOT NULL").fetchone()[0]
        == 2
    )
