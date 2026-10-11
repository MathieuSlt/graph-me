"""Install hints keep the extras you have, and say why meaning-based search is off."""

import sys

import pytest

from graph_me import config, doctor, env
from graph_me.config import Config, SourceConfig
from graph_me.pipeline import enrich, parse, run
from graph_me.pipeline.tier1 import embed
from graph_me.store import db


def have(monkeypatch, *modules):
    monkeypatch.setattr(env, "_importable", lambda module: module in modules)


def test_install_command_keeps_the_extras_you_have(monkeypatch):
    have(monkeypatch, "uvicorn", "starlette", "jinja2", "rapidocr", "EventKit")
    assert env.installed_extras() == ["ui", "ocr", "macos"]
    # Adding [medium] must not drop ui, ocr and macos (uv tool install replaces extras).
    assert env.install_command("medium") == (
        'uv tool install --managed-python "graph-me[medium,ui,ocr,macos]"'
    )
    have(monkeypatch)
    assert env.install_command("ocr") == 'uv tool install --managed-python "graph-me[ocr]"'
    assert env.install_command() == 'uv tool install --managed-python "graph-me"'
    have(monkeypatch, "sqlite_vec")  # half of [medium] is not [medium]
    assert env.installed_extras() == []


def test_vec_problem_says_what_to_do(monkeypatch):
    have(monkeypatch, "uvicorn", "starlette", "jinja2")
    problem = env.vec_problem()
    assert "[medium] extra is missing" in problem and '"graph-me[medium,ui]"' in problem

    pytest.importorskip("sqlite_vec")
    have(monkeypatch, "sqlite_vec", "fastembed")
    monkeypatch.setattr(env, "loads_extensions", lambda: False)
    assert "can't load SQLite extensions" in env.vec_problem()
    monkeypatch.setattr(env, "loads_extensions", lambda: True)
    assert env.vec_problem() is None


def test_enrich_reports_why_embeddings_were_skipped(monkeypatch, conn):
    monkeypatch.setattr(embed, "vec_loaded", lambda c: False)
    monkeypatch.setattr(env, "vec_problem", lambda: "the [medium] extra is missing. Install ...")
    status, done = enrich._embed(conn, "local", "", (), None)
    assert done == 0 and "the [medium] extra is missing" in status
    assert "search stays on words" in status


def test_ocr_hint_keeps_the_extras_you_have(monkeypatch, tmp_path):
    import pypdfium2 as pdfium

    path = tmp_path / "scan.pdf"
    pdf = pdfium.PdfDocument.new()
    pdf.new_page(200, 200)
    pdf.save(path)
    pdf.close()
    monkeypatch.setattr(parse, "_engine", None)
    monkeypatch.setitem(sys.modules, "rapidocr", None)  # not installed
    have(monkeypatch, "uvicorn", "starlette", "jinja2")
    with pytest.raises(parse.ParseError, match=r"graph-me\[ui,ocr\]"):
        parse.parse_file(path, ocr=True)


@pytest.fixture
def index(tmp_path, docs):
    cfg = Config(sources={"docs": SourceConfig(type="filesystem", paths=[str(docs)])})
    out = config.ensure_output(tmp_path / "graph-out")
    conn = db.connect(out / config.DB_NAME)
    run.scan(conn, cfg, workers=1)
    return cfg, out, conn


def test_doctor_on_tier0_says_nothing_about_embeddings(index, tmp_path, monkeypatch):
    cfg, out, conn = index
    conn.close()
    have(monkeypatch)
    labels = {c.label for c in doctor.run(cfg, tmp_path / "config.yaml", out, home=tmp_path)}
    assert "embeddings" not in labels  # Tier 0 by choice: no nagging


def test_doctor_warns_when_an_enriched_index_lost_its_embeddings(index, tmp_path, monkeypatch):
    cfg, out, conn = index
    conn.execute("UPDATE items SET tier = 1 WHERE rowid = (SELECT min(rowid) FROM items)")
    conn.commit()
    conn.close()
    have(monkeypatch, "uvicorn", "starlette", "jinja2", "rapidocr")
    checks = {c.label: c for c in doctor.run(cfg, tmp_path / "config.yaml", out, home=tmp_path)}
    check = checks["embeddings"]
    assert check.status == "warn" and "meaning-based search is off" in check.message
    assert '"graph-me[medium,ui,ocr]"' in check.hint and "graph-me enrich" in check.hint

    cfg.extraction.medium.embeddings = "none"  # turned off on purpose: nothing to say
    labels = {c.label for c in doctor.run(cfg, tmp_path / "config.yaml", out, home=tmp_path)}
    assert "embeddings" not in labels
