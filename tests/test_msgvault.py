"""msgvault connector against a real msgvault archive built from synthetic data."""

import hashlib
import sqlite3

import pytest
from conftest import needs_msgvault

from graph_me.config import BlacklistConfig, SourceConfig
from graph_me.connectors.base import SourceUnavailable
from graph_me.connectors.msgvault import MsgvaultConnector, MsgvaultSchemaError

pytestmark = needs_msgvault


def connector(home, **extra):
    return MsgvaultConnector("messages", SourceConfig(type="msgvault", db=str(home), **extra),
                             BlacklistConfig())  # fmt: skip


def items(home, **extra):
    c = connector(home, **extra)
    return {i.external_id: i for i in c.fetch(dict(c.list_ids()))}


def by_text(found, needle):
    return next(i for i in found.values() if needle in (i.text or ""))


def test_lists_mail_and_whatsapp(mv):
    home, _ = mv
    found = items(home)
    kinds = [i.kind for i in found.values()]
    assert kinds.count("email") == 125 and kinds.count("message") == 8


def test_email_mapping_and_attachment_hash(mv, docs_template):
    home, _ = mv
    lease = by_text(items(home), "ci-joint le contrat de bail")
    assert lease.kind == "email" and lease.title == "Votre contrat de bail"
    assert (lease.author.name, lease.author.email) == ("Jean Dupont", "jean.dupont@example.org")
    assert [p.email for p in lease.recipients] == ["camille@example.com"]
    pdf = (docs_template / "Logement/Contrat_bail_2025.pdf").read_bytes()
    [att] = lease.attachments
    assert att.filename == "Contrat_bail_2025.pdf"
    assert att.content_hash == hashlib.sha256(pdf).hexdigest()


def test_whatsapp_recipients_and_direction(mv):
    home, _ = mv
    found = items(home)
    greeting = by_text(found, "Joyeux anniv Soeurette")
    assert greeting.is_from_me and greeting.trust == "self"
    assert [p.phone for p in greeting.recipients] == ["+33612345678"]
    group = by_text(found, "à toi aussi")
    assert not group.is_from_me
    assert len(group.recipients) >= 2  # everyone else in the group


def test_trust_levels(mv):
    home, _ = mv
    found = items(home)
    assert by_text(found, "ci-joint le contrat").trust == "known"  # I replied to Jean
    assert by_text(found, "prizes@spam.example").trust == "untrusted"


def test_deleted_messages_are_gone(mv):
    home, _ = mv
    raw = sqlite3.connect(home / "msgvault.db")
    raw.execute(
        "UPDATE messages SET deleted_from_source_at = CURRENT_TIMESTAMP WHERE subject = 'WIN A FREE CRUISE'"
    )
    raw.execute(
        "UPDATE messages SET deleted_at = CURRENT_TIMESTAMP WHERE subject LIKE 'newsletter%'"
    )
    raw.commit()
    raw.close()
    found = items(home)
    assert not any(i.title == "WIN A FREE CRUISE" for i in found.values())
    assert not any((i.title or "").startswith("newsletter") for i in found.values())


def test_accounts_filter(mv):
    home, _ = mv
    only_whatsapp = items(home, accounts=["+33600000000"])
    assert {i.kind for i in only_whatsapp.values()} == {"message"}


def test_version_changes_with_content(mv):
    home, _ = mv
    c = connector(home)
    before = dict(c.list_ids())
    raw = sqlite3.connect(home / "msgvault.db")
    raw.execute("UPDATE messages SET subject = 'edited' WHERE subject = 'Atlas: kick-off'")
    raw.commit()
    raw.close()
    after = dict(c.list_ids())
    assert sum(before[k] != after[k] for k in before) == 1


def test_raw_address_book_entries_become_contacts(mv):
    home, _ = mv
    raw = sqlite3.connect(home / "msgvault.db")
    raw.execute("INSERT INTO persons(id, vcard_uid, display_name) VALUES (900, 'uid-lea', 'Léa')")
    raw.execute(
        "INSERT INTO person_names(person_id, name_kind, formatted, original_value, source) "
        "VALUES (900, 'formatted', 'Léa Petit', 'Léa Petit', 'carddav_import')"
    )
    raw.execute(
        "INSERT INTO person_contact_points(person_id, address_kind, original_value, normalized_value, source) "
        "VALUES (900, 'email', 'lea@example.com', 'lea@example.com', 'carddav_import')"
    )
    raw.execute(
        "INSERT INTO person_dates(person_id, date_kind, date_month, date_day, original_value, source) "
        "VALUES (900, 'birthday', 7, 4, '--0704', 'carddav_import')"
    )
    # an inferred (non-raw) date must be ignored: graph-me builds its own facts
    raw.execute(
        "INSERT INTO person_dates(person_id, date_kind, date_month, date_day, original_value, source, confidence) "
        "VALUES (900, 'birthday', 1, 1, '--0101', 'inference', 0.4)"
    )
    raw.commit()
    raw.close()
    found = items(home)
    contact = found["contact/uid-lea"]
    assert contact.kind == "contact" and contact.contact.name == "Léa Petit"
    assert contact.contact.emails == ["lea@example.com"]
    assert contact.contact.birthday == "07-04"


def test_missing_database_and_schema_change_are_clear(mv, tmp_path):
    home, _ = mv
    with pytest.raises(SourceUnavailable, match="msgvault database not found"):
        list(connector(tmp_path / "nowhere").list_ids())
    raw = sqlite3.connect(home / "msgvault.db")
    raw.execute("ALTER TABLE attachments RENAME COLUMN content_hash TO sha")
    raw.commit()
    raw.close()
    with pytest.raises(MsgvaultSchemaError, match="attachments: content_hash"):
        list(connector(home).list_ids())


def test_graph_me_never_writes_to_msgvault(mv):
    home, _ = mv
    db = home / "msgvault.db"
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    c = connector(home)
    list(c.fetch(dict(c.list_ids())))
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
