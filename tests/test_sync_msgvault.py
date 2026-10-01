"""M3 gate: what msgvault deletes (or you delete on disk) disappears from graph-me on sync,
while facts that still have other evidence survive."""

import sqlite3
import subprocess

import pytest
from conftest import assert_consistent, full_config, needs_msgvault

from graph_me.pipeline import run
from graph_me.query import engine, graph

pytestmark = needs_msgvault


def msgvault(home, *args):
    try:
        subprocess.run(
            ["msgvault", "--home", str(home), "--no-log-file", *args],
            check=True, capture_output=True, text=True, timeout=300,
        )  # fmt: skip
    finally:
        subprocess.run(["msgvault", "--home", str(home), "--no-log-file", "daemon", "stop"],
                       capture_output=True, timeout=60)  # fmt: skip


def mark_deleted_at_source(home, subject):
    """What msgvault's Gmail sync records when a message is deleted in the mailbox."""
    raw = sqlite3.connect(home / "msgvault.db")
    raw.execute(
        "UPDATE messages SET deleted_from_source_at = CURRENT_TIMESTAMP WHERE subject = ?",
        (subject,),
    )
    raw.commit()
    raw.close()


@pytest.fixture
def synced(conn, docs, mv):
    home, contacts = mv
    cfg = full_config(docs, home, contacts)
    report = run.sync(conn, cfg, workers=1)
    assert report.ok
    return conn, cfg, home, docs


def count(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def test_gate_deleted_spam_disappears_then_gc_keeps_it_gone(synced):
    conn, cfg, home, _ = synced
    assert engine.search(conn, "cruise")
    mark_deleted_at_source(home, "WIN A FREE CRUISE")
    assert run.sync(conn, cfg, workers=1).sources["messages"].forgotten == 1
    assert engine.search(conn, "cruise") == []
    assert count(conn, "SELECT count(*) FROM aliases WHERE value = 'deals@spam.example'") == 0
    assert_consistent(conn)

    msgvault(home, "gc", "--yes", "--no-backup")  # msgvault purges the row for good
    st = run.sync(conn, cfg, workers=1).sources["messages"]
    assert (st.forgotten, st.added, st.updated) == (0, 0, 0)
    assert_consistent(conn)


def test_gate_removed_whatsapp_account_keeps_facts_with_other_evidence(synced):
    conn, cfg, home, _ = synced
    msgvault(home, "remove-account", "+33600000000", "--type", "whatsapp", "--yes")
    st = run.sync(conn, cfg, workers=1).sources["messages"]
    assert st.forgotten == 8  # every WhatsApp message
    assert count(conn, "SELECT count(*) FROM items WHERE external_id LIKE 'msg/whatsapp/%'") == 0

    [sophie] = graph.get_fact(conn, "Soeurette", "birthday", cfg.people)
    [birthday] = sophie["facts"]
    assert (birthday["value"], birthday["confidence"]) == ("03-12", 0.95)
    kinds = {e["kind"] for e in birthday["evidence"]}
    assert kinds == {"contact", "email"}  # the card and the 2022 email remain
    assert_consistent(conn)


def test_gate_deleted_file_loses_its_saved_copy_link(synced):
    conn, cfg, _, docs = synced
    email = conn.execute("SELECT id FROM items WHERE title = 'Votre contrat de bail'").fetchone()[0]
    assert graph.enrich_hits(conn, [email])[email].get("saved_as")
    (docs / "Logement/Contrat_bail_2025.pdf").unlink()
    assert run.sync(conn, cfg, workers=1).sources["docs"].forgotten == 1
    assert "saved_as" not in graph.enrich_hits(conn, [email]).get(email, {})
    assert_consistent(conn)


def test_missing_msgvault_database_is_skipped(synced):
    conn, cfg, home, _ = synced
    before = count(conn, "SELECT count(*) FROM items")
    (home / "msgvault.db").rename(home / "moved.db")
    report = run.sync(conn, cfg, workers=1)
    assert "msgvault database not found" in report.sources["messages"].failure
    assert count(conn, "SELECT count(*) FROM items") == before


def test_cli_sync(tmp_path, docs, mv, monkeypatch):
    from typer.testing import CliRunner

    from graph_me.cli import app

    monkeypatch.delenv("GRAPH_ME_OUT", raising=False)
    home, contacts = mv
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "people: {phone_country_code: '33', timezone: Europe/Paris}\n"
        "sources:\n"
        f"  docs: {{type: filesystem, paths: ['{docs}']}}\n"
        f"  messages: {{type: msgvault, db: '{home}'}}\n"
    )
    base = ["--config", str(cfg), "--out", str(tmp_path / "out")]
    runner = CliRunner()
    runner.invoke(app, [*base, "init"])
    result = runner.invoke(app, [*base, "sync", "--workers", "1"])
    assert result.exit_code == 0, result.output
    assert "forgotten" not in result.output  # nothing to forget on a first sync

    mark_deleted_at_source(home, "WIN A FREE CRUISE")
    result = runner.invoke(app, [*base, "sync", "--workers", "1"])
    assert "messages: 132 items" in result.output and "1 forgotten" in result.output

    docs.rename(docs.with_name("unplugged"))
    result = runner.invoke(app, [*base, "sync", "--workers", "1"])
    assert result.exit_code == 1
    assert "docs: skipped" in result.output and "not found" in result.output
