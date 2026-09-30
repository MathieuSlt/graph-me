from pathlib import Path

import pytest

from graph_me.pipeline import chunk, lang, parse


def text_of(docs: Path, rel: str) -> str:
    return parse.parse_file(docs / rel)


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("Logement/Contrat_bail_2025.pdf", "CONTRAT DE BAIL"),
        ("Logement/etat_des_lieux.docx", "État des lieux d'entrée"),
        ("Travail/presentation_atlas.pptx", "Projet Atlas"),
        ("Finances/budget_2025.xlsx", "Loyer\t950"),
        ("notes/recette_crepes.md", "4 œufs"),
        ("mail/facture_electricite.eml", "Subject: Votre facture d'électricité de mai"),
        ("web/article.html", "Randonnée au Mont Blanc"),
    ],
)
def test_formats(docs_template, rel, expected):
    assert expected in text_of(docs_template, rel)


def test_pptx_slides_in_order(docs_template):
    text = text_of(docs_template, "Travail/presentation_atlas.pptx")
    assert text.index("Projet Atlas") < text.index("Roadmap")


def test_latin1_text_is_decoded(docs_template):
    assert "fenêtre" in text_of(docs_template, "notes/todo_maison.txt")


def test_html_drops_scripts_and_styles(docs_template):
    text = text_of(docs_template, "web/article.html")
    assert "alert" not in text and "color:red" not in text


def test_corrupt_and_unsupported_files_raise(docs_template):
    with pytest.raises(parse.ParseError):
        text_of(docs_template, "broken/corrompu.pdf")
    with pytest.raises(parse.ParseError):
        text_of(docs_template, "photos/vacances.jpg")


def test_oversized_office_member_is_refused(tmp_path, monkeypatch):
    from factory import make_docx

    path = tmp_path / "big.docx"
    path.write_bytes(make_docx(["x" * 1000]))
    monkeypatch.setattr(parse, "MAX_XML_BYTES", 100)
    with pytest.raises(parse.ParseError, match="too large"):
        parse.parse_file(path)


def test_chunk_respects_budget_and_paragraphs():
    text = "\n\n".join(" ".join(["mot"] * 120) for _ in range(10))
    parts = chunk.chunk(text, max_tokens=500)
    assert all(tokens <= 500 for _, tokens in parts)
    assert sum(tokens for _, tokens in parts) == 1200
    huge = " ".join(["w"] * 1234)
    assert [t for _, t in chunk.chunk(huge, max_tokens=500)] == [500, 500, 234]
    assert chunk.chunk("   \n\n  ") == []


def test_language_detection():
    assert (
        lang.detect("Le contrat de bail est signé pour une durée de trois ans avec le locataire.")
        == "fr"
    )
    assert (
        lang.detect("The team agreed to move the launch to January with the finance team.") == "en"
    )
    assert lang.detect("12345 67890") is None
