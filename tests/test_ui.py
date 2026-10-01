"""M6: the local web UI. Read-only, token-protected, escaped, and it answers the three jobs."""

import json
import re

import pytest
from conftest import assert_consistent, docs_config, full_config, needs_msgvault

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from graph_me.pipeline import run  # noqa: E402
from graph_me.service import Service  # noqa: E402
from graph_me.store import db  # noqa: E402
from graph_me.ui.app import COOKIE, build_app  # noqa: E402

TOKEN = "test-token-0123456789"
BASE = "http://127.0.0.1"


def make_client(cfg, db_path, *, scan=True):
    if scan:
        conn = db.connect(db_path)
        run.sync(conn, cfg, workers=1)
        assert_consistent(conn)
        conn.close()
    return TestClient(build_app(Service(cfg, db_path, "ui"), TOKEN), base_url=BASE)


def logged_in(client):
    client.cookies.set(COOKIE, TOKEN)
    return client


@pytest.fixture
def anon(tmp_path, docs):
    return make_client(docs_config(docs), tmp_path / "graph.db")


@pytest.fixture
def ui(anon):
    return logged_in(anon)


@pytest.fixture
def full(tmp_path, docs, mv):
    home, contacts = mv
    return logged_in(make_client(full_config(docs, home, contacts), tmp_path / "graph.db"))


# --- safety -----------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/search?q=bail", "/graph", "/api/graph", "/status",
                                  "/static/app.css", "/static/vendor/sigma.min.js"])  # fmt: skip
def test_requests_without_token_fail(anon, path):
    assert anon.get(path).status_code == 403
    anon.cookies.set(COOKIE, "wrong")
    assert anon.get(path).status_code == 403
    assert anon.get(path + ("&" if "?" in path else "?") + "token=wrong").status_code == 403


def test_token_url_becomes_a_strict_cookie(anon):
    res = anon.get(f"/search?q=bail&token={TOKEN}", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/search?q=bail"  # the token leaves the address bar
    cookie = res.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert anon.get("/search?q=bail").status_code == 200  # the cookie is enough from now on


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_nothing_can_be_written(ui, method):
    for path in ("/", "/search", "/api/graph", "/status"):
        assert ui.request(method, path).status_code == 405


def test_foreign_host_is_rejected(ui):
    # DNS rebinding: a page on evil.example resolving to 127.0.0.1 must not reach the UI.
    assert ui.get("/", headers={"host": "evil.example"}).status_code == 400
    assert ui.get("/", headers={"host": "localhost:8000"}).status_code == 200


def test_security_headers(ui, anon):
    for res in (ui.get("/"), anon.get("/")):
        csp = res.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "unsafe-inline" not in csp
        assert res.headers["referrer-policy"] == "no-referrer"
        assert res.headers["x-frame-options"] == "DENY"
        assert res.headers["cache-control"] == "no-store"


def test_no_inline_script_or_style(ui):
    # The CSP forbids them: a page relying on one would silently break.
    for path in ("/", "/search?q=bail", "/graph", "/status"):
        html = ui.get(path).text
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), path
        assert "<style" not in html and " style=" not in html, path


def test_content_is_escaped(tmp_path, docs):
    (docs / "notes/<img src=x onerror=alert(1)>.txt").write_text(
        "piège <script>alert('xss')</script> fin\n"
    )
    client = logged_in(make_client(docs_config(docs), tmp_path / "graph.db"))
    html = client.get("/search", params={"q": "piège"}).text
    assert "<script>alert" not in html and "<img src=x" not in html
    assert "&lt;script&gt;" in html and "&lt;img src=x" in html
    item_id = re.search(r'href="/item/([0-9a-f]+)"', html).group(1)
    page = client.get(f"/item/{item_id}").text
    assert "<script>alert" not in page and "&lt;script&gt;alert" in page


def test_ui_queries_are_logged(ui, tmp_path):
    ui.get("/search", params={"q": "bail"})
    conn = db.connect(tmp_path / "graph.db")
    row = conn.execute("SELECT interface, query FROM query_log ORDER BY rowid DESC").fetchone()
    conn.close()
    assert tuple(row) == ("ui", "bail")


def test_missing_store(tmp_path, docs):
    client = logged_in(make_client(docs_config(docs), tmp_path / "none.db", scan=False))
    res = client.get("/search", params={"q": "bail"})
    assert res.status_code == 503 and "graph-me init" in res.text


# --- pages ------------------------------------------------------------------------------------


def test_search_page_and_htmx_partial(ui):
    page = ui.get("/search", params={"q": "bail"})
    assert page.status_code == 200 and "<html" in page.text
    assert "Contrat_bail_2025.pdf" in page.text
    partial = ui.get("/search", params={"q": "bail"}, headers={"HX-Request": "true"})
    assert "<html" not in partial.text and "Contrat_bail_2025.pdf" in partial.text
    assert "Nothing found" in ui.get("/search", params={"q": "zzzqqq"}).text
    filtered = ui.get("/search", params={"q": "bail", "kind": "email"}).text
    assert "Contrat_bail_2025.pdf" not in filtered


def test_injection_is_flagged(ui):
    html = ui.get("/search", params={"q": "invoice 4471"}).text
    assert "possible prompt injection" in html
    status = ui.get("/status").text
    assert "Possible prompt injections (1)" in status and "invoice_urgent.txt" in status


def test_unknown_pages_are_404(ui):
    assert ui.get("/item/nope").status_code == 404
    assert ui.get("/entity/nope").status_code == 404


@needs_msgvault
def test_gate_lease_with_the_email_it_came_with(full):
    html = full.get("/search", params={"q": "bail", "kind": "file"}).text
    assert "Contrat_bail_2025.pdf" in html
    assert "came with" in html and "Votre contrat de bail" in html


@needs_msgvault
def test_gate_sophie_birthday_with_evidence(full):
    html = full.get("/search", params={"q": "anniversaire Sophie Martin"}).text
    entity_id = re.search(r'href="/entity/([^"]+)"[^>]*>Sophie Martin<', html).group(1)
    page = full.get(f"/entity/{entity_id}").text
    assert "03-12" in page and "Why do I know this?" in page
    evidence = re.findall(r'href="/item/([0-9a-f]+)"', page)
    assert evidence
    item = full.get(f"/item/{evidence[0]}").text
    assert "Learned from this item" in item and "Sophie Martin" in item


@needs_msgvault
def test_secrets_stay_redacted(full):
    html = full.get("/search", params={"q": "facture électricité"}).text
    assert "4111 1111 1111 1111" not in html and "[REDACTED:card]" in html
    item_id = re.search(r'href="/item/([0-9a-f]+)"', html).group(1)
    page = full.get(f"/item/{item_id}", params={"reveal": "true"}).text
    assert "4111 1111 1111 1111" not in page and "graph-me query --reveal" in page


@needs_msgvault
def test_graph_api(full):
    data = full.get("/api/graph").json()
    ids = {n["id"] for n in data["nodes"]}
    assert data["nodes"] and data["edges"]
    assert all(e["source"] in ids and e["target"] in ids for e in data["edges"])
    assert any(n["is_me"] for n in data["nodes"])
    mentions = [n["mentions"] for n in data["nodes"]]
    assert mentions == sorted(mentions, reverse=True)  # most mentioned first

    people = full.get("/api/graph", params={"kind": "person"}).json()["nodes"]
    assert people and {n["kind"] for n in people} == {"person"}
    small = full.get("/api/graph", params={"limit": "3"}).json()
    assert len(small["nodes"]) == 3 and small["truncated"]
    assert full.get("/api/graph", params={"limit": "nope"}).status_code == 200
    none = full.get("/api/graph", params={"since": "2099-01-01"}).json()
    assert none["nodes"] == [] and none["edges"] == []

    sophie = next(n for n in people if n["name"] == "Sophie Martin")
    around = full.get(f"/api/graph/{sophie['id']}").json()
    assert sophie["id"] in {n["id"] for n in around["nodes"]} and around["edges"]


@needs_msgvault
def test_status_page(full):
    html = full.get("/status").text
    for name in ("docs", "messages", "contacts"):
        assert f"<td>{name}</td>" in html
    assert "sync:" in html and "blacklisted" in html


@needs_msgvault
def test_graph_page_loads_its_scripts(full):
    html = full.get("/graph").text
    for src in re.findall(r'src="(/static/[^"]+)"', html):
        assert full.get(src).status_code == 200, src
    module = full.get("/static/graph.js").text
    for imported in re.findall(r'from "(/static/[^"]+)"', module):
        assert full.get(imported).status_code == 200, imported
    assert json.loads(full.get("/api/graph").text)["communities"] is not None
