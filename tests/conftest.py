from pathlib import Path

import pytest
from factory import make_docs

from graph_me.config import BlacklistConfig, Config, SourceConfig
from graph_me.store import db


@pytest.fixture(scope="session")
def docs_template(tmp_path_factory) -> Path:
    return make_docs(tmp_path_factory.mktemp("fixtures"))


@pytest.fixture
def docs(tmp_path, docs_template) -> Path:
    """A private copy of the documents fixture that a test may modify."""
    import shutil

    target = tmp_path / "docs"
    shutil.copytree(docs_template, target, symlinks=True)
    return target


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "graph.db")
    yield c
    c.close()


def docs_config(docs: Path, **source_extra) -> Config:
    return Config(
        sources={"docs": SourceConfig(type="filesystem", paths=[str(docs)], **source_extra)},
        blacklist=BlacklistConfig(paths=[str(docs / "Medical")]),
    )


needs_msgvault = pytest.mark.skipif(
    __import__("shutil").which("msgvault") is None,
    reason="msgvault is not installed (scripts/install_msgvault.sh)",
)


@pytest.fixture(scope="session")
def msgvault_template(tmp_path_factory, docs_template):
    import shutil

    from factory import build_msgvault

    if shutil.which("msgvault") is None:
        pytest.skip("msgvault is not installed (scripts/install_msgvault.sh)")
    root = tmp_path_factory.mktemp("mv")
    lease = (docs_template / "Logement/Contrat_bail_2025.pdf").read_bytes()
    home = build_msgvault(root, lease)
    return root, home


@pytest.fixture
def mv(tmp_path, msgvault_template):
    """A private copy of the msgvault fixture: (msgvault home, contacts folder)."""
    import shutil

    root, home = msgvault_template
    target = tmp_path / "mv"
    shutil.copytree(home, target / "home", ignore=shutil.ignore_patterns("*.lock", "daemon*"))
    shutil.copytree(root / "contacts", target / "contacts")
    return target / "home", target / "contacts"


def full_config(docs: Path, mv_home: Path, contacts: Path, **people) -> Config:
    from graph_me.config import PeopleConfig

    return Config(
        # The fixture's messages are timed in Paris; pin it so CI (UTC) sees the same days.
        people=PeopleConfig(phone_country_code="33", timezone="Europe/Paris", **people),
        sources={
            "docs": SourceConfig(type="filesystem", paths=[str(docs)]),
            "messages": SourceConfig(type="msgvault", db=str(mv_home)),
            "contacts": SourceConfig(type="vcard", paths=[str(contacts)]),
        },
        blacklist=BlacklistConfig(paths=[str(docs / "Medical")]),
    )


def assert_consistent(conn) -> None:
    """Graph invariants that must hold after any scan or sync."""
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    assert (
        one("SELECT count(*) FROM entities WHERE id NOT IN (SELECT entity_id FROM mentions)") == 0
    )
    assert (
        one(
            "SELECT count(*) FROM facts WHERE id NOT IN "
            "(SELECT fact_id FROM evidence WHERE fact_id IS NOT NULL)"
        )
        == 0
    )
    assert (
        one(
            "SELECT count(*) FROM relations WHERE id NOT IN "
            "(SELECT relation_id FROM evidence WHERE relation_id IS NOT NULL)"
        )
        == 0
    )
    assert (
        one(
            "SELECT count(*) FROM relations r WHERE weight != "
            "(SELECT count(*) FROM evidence e WHERE e.relation_id = r.id)"
        )
        == 0
    )
    # FTS indexes match their tables (raises if not)
    conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('integrity-check')")
    conn.execute("INSERT INTO items_fts(items_fts) VALUES ('integrity-check')")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
