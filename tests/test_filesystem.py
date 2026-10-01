import os

import pytest

from graph_me.config import BlacklistConfig, SourceConfig
from graph_me.connectors.filesystem import IGNORE_MARKER, FilesystemConnector, glob_to_regex


def connector(docs, blocked=(), **extra):
    return FilesystemConnector(
        "docs",
        SourceConfig(type="filesystem", paths=[str(docs)], **extra),
        BlacklistConfig(paths=[str(p) for p in blocked]),
    )


def listed(c):
    return {os.path.relpath(eid, c.roots[0]) for eid, _ in c.list_ids()}


def test_default_excludes_and_supported_types(docs):
    files = listed(connector(docs))
    assert "Logement/Contrat_bail_2025.pdf" in files
    assert "node_modules/leftpad/index.js" not in files  # noise
    assert "photos/vacances.jpg" not in files  # unsupported, not indexed
    assert "Medical/ordonnance.txt" in files  # personal data is never excluded by default


def test_blacklisted_folder_is_skipped(docs):
    files = listed(connector(docs, blocked=[docs / "Medical"]))
    assert not any(f.startswith("Medical/") for f in files)


def test_custom_exclude_replaces_defaults(docs):
    files = listed(connector(docs, exclude=["**/notes/**"]))
    assert not any(f.startswith("notes/") for f in files)
    assert "node_modules/leftpad/index.js" in files


def test_build_output_is_noise(docs):
    for rel in (".next/server/chunk.js", "app/.next/dev/page.js", "lib/.mypy_cache/x.json"):
        (docs / rel).parent.mkdir(parents=True, exist_ok=True)
        (docs / rel).write_text("const BAILOUT_TO_CSR = 1\n")
    files = listed(connector(docs))
    assert not any(".next/" in f or ".mypy_cache/" in f for f in files)


def test_marked_folder_is_skipped_unless_configured(docs):
    fixtures = docs / "graph-me" / "tests" / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "Contrat_bail_2025.txt").write_text("fake lease")
    (fixtures / IGNORE_MARKER).write_text("")
    assert not any(f.startswith("graph-me/") for f in listed(connector(docs)))
    # pointed at directly, the marked folder is indexed (the test suite scans its fixtures)
    assert "Contrat_bail_2025.txt" in listed(connector(fixtures))


def test_excluded_tells_skipped_files_from_gone_ones(docs, tmp_path):
    marked = docs / "fixtures"
    marked.mkdir()
    (marked / IGNORE_MARKER).write_text("")
    (marked / "fake.txt").write_text("fake")
    (docs / ".next").mkdir()
    (docs / ".next" / "chunk.js").write_text("x")
    c = connector(docs)
    assert c.excluded(str(marked / "fake.txt"))
    assert c.excluded(str(docs / ".next" / "chunk.js"))
    assert c.excluded(str(docs / "photos/vacances.jpg"))  # unsupported type
    assert not c.excluded(str(docs / "Logement/Contrat_bail_2025.pdf"))  # still listed
    assert not c.excluded(str(docs / "deleted.txt"))  # gone from disk
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x")
    assert not c.excluded(str(outside))  # not under a configured path


def test_symlinks_are_not_followed(docs, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside the configured folder")
    (docs / "link").symlink_to(outside)
    (docs / "link.txt").symlink_to(outside / "secret.txt")
    files = listed(connector(docs))
    assert not any("secret" in f or f.startswith("link") for f in files)


def test_version_changes_when_file_changes(docs):
    c = connector(docs)
    target = docs / "notes/meeting_notes.md"
    before = dict(c.list_ids())[str(target)]
    target.write_text("changed")
    assert dict(c.list_ids())[str(target)] != before


def test_fetch_builds_items(docs):
    c = connector(docs, trust="untrusted")
    path = str(docs / "Logement/Contrat_bail_2025.pdf")
    [item] = list(c.fetch([path, str(docs / "gone.txt")]))
    assert item.kind == "file" and item.title == "Contrat_bail_2025.pdf"
    assert item.trust == "untrusted"
    assert len(item.content_hash) == 64
    assert item.extra["root"] == str(docs)


def test_invalid_trust_is_rejected(docs):
    with pytest.raises(ValueError, match="trust"):
        connector(docs, trust="friends")


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("**/node_modules/**", "/a/b/node_modules/x/y.js", True),
        ("**/node_modules/**", "/a/node_modules_old/y.js", False),
        ("*.log", "/a/b.log", False),  # * does not cross folders
        ("**/*.log", "/a/b/c.log", True),
        ("/home/me/tmp/**", "/home/me/tmp/x", True),
    ],
)
def test_glob_to_regex(pattern, path, matches):
    assert bool(glob_to_regex(pattern).fullmatch(path)) is matches
