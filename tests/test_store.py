import sqlite3

import pytest

from graph_me.store import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "graph.db")
    yield c
    c.close()


def _item(conn, item_id="i1", source="docs"):
    conn.execute("INSERT OR IGNORE INTO sources(id, type) VALUES (?, 'filesystem')", (source,))
    conn.execute(
        "INSERT INTO items(id, source_id, external_id, kind) VALUES (?, ?, ?, 'file')",
        (item_id, source, f"/tmp/{item_id}"),
    )


def test_schema_version_recorded(conn):
    assert db.schema_version(conn) == db.SCHEMA_VERSION
    assert db.get_meta(conn, "schema_version") == str(db.SCHEMA_VERSION)


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "graph.db"
    db.connect(path).close()
    c = db.connect(path)
    assert db.migrate(c) == db.SCHEMA_VERSION
    c.close()


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "graph.db"
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    raw.close()
    with pytest.raises(RuntimeError, match="newer"):
        db.connect(path)


def test_fts_search_and_cascade_delete(conn):
    _item(conn)
    conn.execute(
        "INSERT INTO chunks(item_id, ord, text, tokens) VALUES ('i1', 0, 'Contrat de bail signé', 4)"
    )
    hits = conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'bail'").fetchall()
    assert len(hits) == 1
    # accents are folded: "signe" finds "signé"
    assert (
        conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'signe'").fetchone()[0]
        == 1
    )

    conn.execute("DELETE FROM items WHERE id = 'i1'")
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
    assert (
        conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'bail'").fetchone()[0]
        == 0
    )


def test_evidence_cascades_but_fact_row_remains_for_cleanup(conn):
    _item(conn)
    conn.execute("INSERT INTO entities(id, kind, name) VALUES ('p1', 'person', 'Sophie')")
    conn.execute(
        "INSERT INTO facts(id, entity_id, key, value, confidence) VALUES (1, 'p1', 'birthday', '03-12', 0.6)"
    )
    conn.execute(
        "INSERT INTO evidence(fact_id, item_id, method) VALUES (1, 'i1', 'rule:fr.birthday')"
    )
    conn.execute("DELETE FROM items WHERE id = 'i1'")
    assert conn.execute("SELECT count(*) FROM evidence").fetchone()[0] == 0
    # forget.py (M3) removes facts left without evidence
    assert conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 1


def test_evidence_must_target_exactly_one_of_fact_or_relation(conn):
    _item(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO evidence(item_id, method) VALUES ('i1', 'x')")


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO items(id, source_id, external_id, kind) VALUES ('x', 'missing', 'x', 'file')"
        )


def test_readonly_connection_cannot_write(tmp_path):
    path = tmp_path / "graph.db"
    db.connect(path).close()
    ro = db.connect(path, readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO meta(key, value) VALUES ('a', 'b')")
    ro.close()


def test_upgrade_from_v1_indexes_existing_items(tmp_path):
    from importlib import resources

    path = tmp_path / "graph.db"
    raw = sqlite3.connect(path)
    v1 = resources.files("graph_me.store").joinpath("schema.sql").read_text()
    raw.executescript(v1 + "\nPRAGMA user_version = 1;")
    raw.execute("INSERT INTO sources(id, type) VALUES ('docs', 'filesystem')")
    raw.execute(
        "INSERT INTO items(id, source_id, external_id, kind, title, uri) "
        "VALUES ('i1', 'docs', '/d/bail.pdf', 'file', 'bail.pdf', '/d/bail.pdf')"
    )
    raw.commit()
    raw.close()

    conn = db.connect(path)
    assert db.schema_version(conn) == 2
    hits = conn.execute("SELECT rowid FROM items_fts WHERE items_fts MATCH 'bail'").fetchall()
    assert len(hits) == 1
    conn.close()
