-- v2: cheap change detection and full-text search over item titles and paths.

-- Opaque per-source version (for files: "<mtime_ns>:<size>"). Unchanged version = skip.
ALTER TABLE items ADD COLUMN version TEXT;

-- Search over titles and paths, so "bail" finds bail_2025.pdf even before reading it.
CREATE VIRTUAL TABLE items_fts USING fts5(
  title, uri, content='items', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER items_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, title, uri) VALUES (new.rowid, new.title, new.uri);
END;
CREATE TRIGGER items_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, uri) VALUES ('delete', old.rowid, old.title, old.uri);
END;
CREATE TRIGGER items_au AFTER UPDATE OF title, uri ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, uri) VALUES ('delete', old.rowid, old.title, old.uri);
  INSERT INTO items_fts(rowid, title, uri) VALUES (new.rowid, new.title, new.uri);
END;

-- Index items that existed before this migration.
INSERT INTO items_fts(items_fts) VALUES ('rebuild');
