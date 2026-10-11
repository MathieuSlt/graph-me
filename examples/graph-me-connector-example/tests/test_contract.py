import json

from graph_me_notes import NotesConnector

from graph_me.config import BlacklistConfig, SourceConfig
from graph_me.testing import check_connector


def connector(path):
    return NotesConnector("notes", SourceConfig(type="notes-jsonl", path=str(path)),
                          BlacklistConfig())  # fmt: skip


def test_contract(tmp_path):
    path = tmp_path / "notes.jsonl"
    notes = [
        {"id": "n1", "title": "Boiler", "text": "Serviced in May", "updated": "2026-05-02T10:00Z"},
        {"id": "n2", "title": "Car", "text": "Plates AB-123"},
    ]
    path.write_text("\n".join(json.dumps(n) for n in notes))
    items = check_connector(lambda: connector(path), unreachable=lambda: connector(tmp_path / "x"))
    assert [i.title for i in items] == ["Boiler", "Car"]
