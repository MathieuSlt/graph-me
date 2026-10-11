-- v5: who asked (agent activity) and where each identifier was seen (profiles).

-- The app that asked (MCP client name) and the profile it was limited to.
ALTER TABLE query_log ADD COLUMN client TEXT;
ALTER TABLE query_log ADD COLUMN profile TEXT;
CREATE INDEX query_log_ts ON query_log(ts);

-- The emails, phone numbers and handles each item showed for its people. A profile shows a
-- person's identifiers only when an item it can see showed them.
CREATE TABLE item_identifiers (
  item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  value TEXT NOT NULL,
  UNIQUE (item_id, kind, value)
);
CREATE INDEX item_identifiers_value ON item_identifiers(kind, value);
