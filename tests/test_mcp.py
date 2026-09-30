"""M4 gate: an MCP client asks for the lease PDF and Sophie's birthday and gets the path, the
email it came with, and 03-12 with its citations."""

import json

import anyio
import pytest
from conftest import full_config, needs_msgvault
from mcp import Client

from graph_me.mcp_server import build_server
from graph_me.pipeline import run
from graph_me.service import Service

pytestmark = needs_msgvault


@pytest.fixture
def server(tmp_path, docs, mv):
    from graph_me.store import db

    home, contacts = mv
    cfg = full_config(docs, home, contacts)
    db_path = tmp_path / "graph.db"
    conn = db.connect(db_path)
    run.sync(conn, cfg, workers=1)
    conn.close()
    return build_server(Service(cfg, db_path, "mcp")), db_path, docs


def call(server, tool, **args):
    async def go():
        async with Client(server) as client:
            result = await client.call_tool(tool, args)
            assert not result.is_error, result.content
            return json.loads(result.content[0].text)

    return anyio.run(go)


def list_tools(server):
    async def go():
        async with Client(server) as client:
            return (await client.list_tools()).tools

    return anyio.run(go)


def test_every_tool_is_read_only(server):
    srv, _, _ = server
    tools = list_tools(srv)
    assert {t.name for t in tools} == {
        "search", "find_document", "who_is", "get_fact", "timeline", "related", "get_item",
        "status",
    }  # fmt: skip
    for t in tools:
        a = t.annotations
        assert (a.read_only_hint, a.destructive_hint, a.open_world_hint) == (True, False, False)
        assert "reveal" not in json.dumps(t.input_schema)  # secrets stay a human decision


def test_gate_lease_pdf(server):
    srv, _, docs = server
    result = call(srv, "find_document", description="contrat de bail")
    first = result["answer_items"][0]
    assert first["path"] == str(docs / "Logement/Contrat_bail_2025.pdf")
    [origin] = first["origin"]
    assert (origin["title"], origin["from"]) == ("Votre contrat de bail", "Jean Dupont")
    assert result["notice"].startswith("Content below comes from the user's personal files")


def test_gate_sophie_birthday(server):
    srv, _, _ = server
    result = call(srv, "get_fact", who="Sophie", key="birthday")
    [sophie] = result["results"]
    [birthday] = sophie["facts"]
    assert (sophie["entity"]["name"], birthday["value"]) == ("Sophie Martin", "03-12")
    assert birthday["evidence"][0]["method"] == "contact:bday"
    assert any(e["kind"] == "message" for e in birthday["evidence"])


def test_injection_comes_back_flagged_and_wrapped(server):
    srv, _, _ = server
    result = call(srv, "search", query="cruise")
    [spam] = result["answer_items"]
    assert spam["flagged"] and spam["trust"] == "untrusted"
    assert "Ignore previous instructions" in spam["snippet"]  # shown as data, not hidden


def test_get_item_is_redacted_and_capped(server):
    srv, _, _ = server
    hit = call(srv, "search", query="facture électricité")["answer_items"][0]
    item = call(srv, "get_item", item_id=hit["id"])
    assert "4111" not in item["text"] and "[REDACTED:card]" in item["text"]
    assert item["redacted"] == ["card"] and not item["truncated"]
    assert call(srv, "get_item", item_id="nope") == {"error": "no item 'nope'"}


def test_people_tools(server):
    srv, _, _ = server
    me = call(srv, "who_is", who="me")["people"][0]
    assert me["is_me"]
    timeline = call(srv, "timeline", who="Soeurette")["results"][0]
    dates = [i["date"] for i in timeline["items"] if i["date"]]
    assert dates == sorted(dates, reverse=True) and len(timeline["items"]) >= 5
    related = call(srv, "related", who="Jean Dupont")["results"][0]["related"]
    assert any(r["entity"]["name"] == "Camille Martin" for r in related)


def test_status_and_query_log(server):
    srv, db_path, _ = server
    status = call(srv, "status")
    assert {s["name"] for s in status["sources"]} == {"docs", "messages", "contacts"}
    assert status["tier"] == 0 and status["note"]
    call(srv, "search", query="bail")
    import sqlite3

    rows = sqlite3.connect(db_path).execute("SELECT interface, query FROM query_log").fetchall()
    assert ("mcp", "bail") in rows


def test_graph_me_mcp_over_stdio(tmp_path, docs, mv):
    """The real `graph-me mcp` command, launched as a subprocess like Claude Code does."""
    import sys

    from mcp import StdioServerParameters

    from graph_me.store import db

    home, contacts = mv
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "people: {phone_country_code: '33', timezone: Europe/Paris}\n"
        "sources:\n"
        f"  docs: {{type: filesystem, paths: ['{docs}']}}\n"
        f"  messages: {{type: msgvault, db: '{home}'}}\n"
        f"  contacts: {{type: vcard, paths: ['{contacts}']}}\n"
    )
    out = tmp_path / "out"
    out.mkdir()
    from graph_me.config import load_config

    conn = db.connect(out / "graph.db")
    run.sync(conn, load_config(cfg_path), workers=1)
    conn.close()

    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-c",
            "from graph_me.cli import app; app()",
            "--config",
            str(cfg_path),
            "--out",
            str(out),
            "mcp",
        ],  # fmt: skip
    )

    async def go():
        async with Client(params) as client:
            result = await client.call_tool("get_fact", {"who": "Soeurette", "key": "birthday"})
            return json.loads(result.content[0].text)

    data = anyio.run(go)
    assert data["results"][0]["facts"][0]["value"] == "03-12"
