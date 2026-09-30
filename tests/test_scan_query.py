"""M1 gate: scanning the fixture and querying "bail" / "lease" returns the lease PDF first."""

import json

import pytest
from conftest import docs_config
from typer.testing import CliRunner

from graph_me.cli import app
from graph_me.config import BlacklistConfig
from graph_me.pipeline import run
from graph_me.query import engine

LEASE = "Logement/Contrat_bail_2025.pdf"


@pytest.fixture
def scanned(conn, docs):
    stats = run.scan(conn, docs_config(docs), workers=1)
    return conn, docs, stats["docs"]


@pytest.mark.parametrize(
    "query", ["bail", "lease", "where is my lease contract", "contrat de bail"]
)
def test_gate_lease_pdf_comes_first(scanned, query):
    conn, docs, _ = scanned
    hits = engine.search(conn, query)
    assert hits and hits[0].uri == str(docs / LEASE), [h.title for h in hits]


def test_scan_stats(scanned):
    _, _, st = scanned
    # 14 files - node_modules - .jpg - Medical (blacklisted) = 11
    assert (st.seen, st.added, st.parse_errors, st.flagged) == (11, 11, 1, 1)
    assert "corrompu.pdf" in st.errors[0]


def test_unreadable_file_is_still_findable_by_name(scanned):
    conn, _, _ = scanned
    assert engine.search(conn, "corrompu")[0].title == "corrompu.pdf"


def test_rescan_skips_unchanged_and_updates_changed(scanned):
    conn, docs, _ = scanned
    cfg = docs_config(docs)
    assert run.scan(conn, cfg, workers=1)["docs"].unchanged == 11

    (docs / "notes/meeting_notes.md").write_text("Nouvelle note sur la piscine municipale.")
    st = run.scan(conn, cfg, workers=1)["docs"]
    assert (st.updated, st.unchanged) == (1, 10)
    assert engine.search(conn, "piscine")[0].title == "meeting_notes.md"
    assert all(h.title != "meeting_notes.md" for h in engine.search(conn, "weekly sync"))


def test_blacklist_pattern_drops_items(conn, docs):
    cfg = docs_config(docs)
    cfg.blacklist = BlacklistConfig(paths=[str(docs / "Medical")], patterns=[r"crêpes?"])
    st = run.scan(conn, cfg, workers=1)["docs"]
    assert st.blacklisted == 1
    assert engine.search(conn, "farine") == []


def test_file_entities_and_cited_relation(scanned):
    conn, _, _ = scanned
    row = conn.execute(
        """SELECT d.name AS doc, p.name AS project, e.method
           FROM relations r
           JOIN entities d ON d.id = r.src JOIN entities p ON p.id = r.dst
           JOIN evidence e ON e.relation_id = r.id
           WHERE d.name = 'Contrat_bail_2025.pdf'"""
    ).fetchone()
    assert (row["project"], row["method"]) == ("Logement", "tier0:path")
    uncited = conn.execute(
        "SELECT count(*) FROM relations WHERE id NOT IN (SELECT relation_id FROM evidence "
        "WHERE relation_id IS NOT NULL)"
    ).fetchone()[0]
    assert uncited == 0


def test_filters(scanned):
    conn, _, _ = scanned
    assert engine.search(conn, "bail", kind="email") == []
    assert engine.search(conn, "bail", source="other") == []
    assert engine.search(conn, "bail", since="2030-01-01") == []
    assert engine.search(conn, "bail", until="2030-01-01")


def test_injection_is_flagged_and_language_detected(scanned):
    conn, _, _ = scanned
    [hit] = engine.search(conn, "invoice", limit=1)
    assert hit.risk_score >= 0.5
    assert hit.lang == "en"
    assert engine.search(conn, "crêpes")[0].lang == "fr"


def test_process_pool_gives_same_result(conn, docs):
    st = run.scan(conn, docs_config(docs), workers=2)["docs"]
    assert (st.added, st.parse_errors) == (11, 1)


def test_unknown_source_type_is_reported(conn, docs):
    cfg = docs_config(docs)
    cfg.sources["docs"].type = "carrier-pigeon"
    with pytest.raises(ValueError, match="unknown type"):
        run.scan(conn, cfg, workers=1)


def test_cli_scan_and_query(tmp_path, docs, monkeypatch):
    monkeypatch.delenv("GRAPH_ME_OUT", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"sources:\n  docs:\n    type: filesystem\n    paths: [{docs}]\n"
        f"blacklist:\n  paths: [{docs / 'Medical'}]\n"
    )
    out = tmp_path / "out"
    base = ["--config", str(cfg), "--out", str(out)]
    runner = CliRunner()
    assert runner.invoke(app, [*base, "query", "bail"]).exit_code == 1  # no store yet
    assert runner.invoke(app, [*base, "init"]).exit_code == 0

    result = runner.invoke(app, [*base, "scan", "--workers", "1"])
    assert result.exit_code == 0, result.output
    assert "11 added" in result.output

    result = runner.invoke(app, [*base, "query", "bail", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["answer_items"][0]["path"] == str(docs / LEASE)
    assert data["notice"]

    result = runner.invoke(app, [*base, "query", "wifi"])
    assert "[REDACTED:password]" in result.output and "hunter2" not in result.output

    import sqlite3

    logged = sqlite3.connect(out / "graph.db").execute("SELECT count(*) FROM query_log").fetchone()
    assert logged[0] == 2

    assert runner.invoke(app, [*base, "scan", "--tier", "medium"]).exit_code == 2
