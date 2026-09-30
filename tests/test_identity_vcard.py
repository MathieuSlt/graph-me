import pytest

from graph_me.config import BlacklistConfig, SourceConfig
from graph_me.connectors.vcard import VcardConnector, card_to_contact, parse_vcards
from graph_me.pipeline.tier0.identity import norm_bday, norm_email, norm_name, norm_phone


@pytest.mark.parametrize(
    ("raw", "cc", "expected"),
    [
        ("06 12 34 56 78", "33", "+33612345678"),
        ("06 12 34 56 78", None, None),  # national number without a country code: unusable
        ("+33 (0)6 12 34 56 78", None, "+33612345678"),
        ("0033 6 12 34 56 78", None, "+33612345678"),
        ("33612345678@s.whatsapp.net", None, "+33612345678"),
        ("tel:+1-555-010-9999", None, "+15550109999"),
        ("12", None, None),
    ],
)
def test_norm_phone(raw, cc, expected):
    assert norm_phone(raw, cc) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1995-03-12", "1995-03-12"), ("19950312", "1995-03-12"), ("--0312", "03-12"),
     ("--03-12", "03-12"), ("1604-03-12", "03-12"), ("1995-13-01", None), ("soon", None)],
)  # fmt: skip
def test_norm_bday(raw, expected):
    assert norm_bday(raw) == expected


def test_norm_email_and_name():
    assert norm_email(" <Sophie.Martin@Example.COM> ") == "sophie.martin@example.com"
    assert norm_email("not an email") is None
    assert norm_name("sophie@example.com") is None
    assert norm_name("+33 6 12") is None
    assert norm_name("  Sophie   Martin ") == "Sophie Martin"


VCF_21 = (
    "BEGIN:VCARD\r\nVERSION:2.1\r\nN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:L=C3=A9a;Zo=C3=A9;;;\r\n"
    "TEL;CELL:+33 6 11 22 33 44\r\nNOTE:line one\\nline two\\, with comma\r\nEND:VCARD\r\n"
)
VCF_FOLDED = (
    "BEGIN:VCARD\nVERSION:3.0\nFN:Marie-Claire\n  Dupuis\nitem1.EMAIL;type=INTERNET:mc@example.com\n"
    "NICKNAME:Mimi,MC\nBDAY:--0521\nEND:VCARD\n"
)


def test_vcard_21_quoted_printable_and_escapes():
    [card] = parse_vcards(VCF_21)
    c = card_to_contact(card)
    assert c.name == "Zoé Léa"
    assert c.phones == ["+33 6 11 22 33 44"]
    assert c.note == "line one\nline two, with comma"


def test_vcard_folding_groups_and_lists():
    [card] = parse_vcards(VCF_FOLDED)
    c = card_to_contact(card)
    assert c.name == "Marie-Claire Dupuis"
    assert c.emails == ["mc@example.com"]
    assert c.nicknames == ["Mimi", "MC"]
    assert c.birthday == "05-21"


def test_vcard_connector_versions_follow_card_content(tmp_path):
    path = tmp_path / "book.vcf"
    path.write_text(VCF_FOLDED + VCF_21)
    conn = VcardConnector("c", SourceConfig(type="vcard", paths=[str(tmp_path)]), BlacklistConfig())
    first = dict(conn.list_ids())
    assert len(first) == 2
    items = list(conn.fetch(first))
    assert {i.kind for i in items} == {"contact"}
    assert any("Anniversaire / birthday: 05-21" in i.text for i in items)

    path.write_text(VCF_FOLDED.replace("--0521", "--0522") + VCF_21)
    second = dict(conn.list_ids())
    changed = [k for k in second if second[k] != first.get(k)]
    assert len(changed) == 1
