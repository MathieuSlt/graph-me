-- graph-me schema, version 1.
-- Everything derived hangs off `items` through `mentions` and `evidence`, so deleting an
-- item cascades cleanly and every fact or relation stays cited.

-- sources and raw items ------------------------------------------------------------------

CREATE TABLE sources (
  id TEXT PRIMARY KEY,                       -- name from config.yaml
  type TEXT NOT NULL,                        -- filesystem | msgvault | ...
  config_hash TEXT,
  cursor TEXT,
  last_sync_at TEXT
);

CREATE TABLE items (
  id TEXT PRIMARY KEY,                       -- hash(source_id, external_id)
  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  external_id TEXT NOT NULL,                 -- path, message id, ...
  kind TEXT NOT NULL,                        -- file | email | message | contact | thread
  title TEXT,
  uri TEXT,
  thread_id TEXT,
  created_at TEXT,
  modified_at TEXT,
  content_hash TEXT,
  lang TEXT,
  trust TEXT NOT NULL DEFAULT 'untrusted',   -- self | known | untrusted
  risk_score REAL NOT NULL DEFAULT 0,        -- injection score from sanitize.py
  tier INTEGER NOT NULL DEFAULT 0,           -- highest tier that processed this item
  UNIQUE (source_id, external_id)
);
CREATE INDEX items_source ON items(source_id);
CREATE INDEX items_thread ON items(thread_id);

CREATE TABLE chunks (
  id INTEGER PRIMARY KEY,
  item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  ord INTEGER NOT NULL,
  text TEXT NOT NULL,
  tokens INTEGER NOT NULL
);
CREATE INDEX chunks_item ON chunks(item_id);

-- External-content FTS index kept in sync by triggers (they also fire on cascade deletes).
CREATE VIRTUAL TABLE chunks_fts USING fts5(
  text, content='chunks', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
-- Tier 1 adds: CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[384]);

-- graph ----------------------------------------------------------------------------------

CREATE TABLE entities (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                        -- person | org | place | project | document
  name TEXT,
  tier INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX entities_kind ON entities(kind);

CREATE TABLE aliases (                       -- email, phone, name, handle, path, hash
  entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  value TEXT NOT NULL,
  UNIQUE (kind, value)
);
CREATE INDEX aliases_entity ON aliases(entity_id);

CREATE TABLE mentions (                      -- entity appears in item
  entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  role TEXT NOT NULL,                        -- author | recipient | mentioned | attachment | file | folder
  tier INTEGER NOT NULL DEFAULT 0,
  UNIQUE (entity_id, item_id, role)
);
CREATE INDEX mentions_item ON mentions(item_id);

CREATE TABLE relations (
  id INTEGER PRIMARY KEY,
  src TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  dst TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  type TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 1,
  confidence REAL NOT NULL DEFAULT 1,
  tier INTEGER NOT NULL DEFAULT 0,
  UNIQUE (src, dst, type)
);
CREATE INDEX relations_dst ON relations(dst);

CREATE TABLE facts (                         -- Sophie.birthday = 03-12
  id INTEGER PRIMARY KEY,
  entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  confidence REAL NOT NULL,
  tier INTEGER NOT NULL DEFAULT 0,
  UNIQUE (entity_id, key, value)
);

CREATE TABLE evidence (                      -- why we believe a relation or a fact
  relation_id INTEGER REFERENCES relations(id) ON DELETE CASCADE,
  fact_id INTEGER REFERENCES facts(id) ON DELETE CASCADE,
  item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  chunk_id INTEGER,
  method TEXT NOT NULL,                      -- rule:fr.birthday | ner | llm:agent | ...
  CHECK ((relation_id IS NULL) <> (fact_id IS NULL))
);
CREATE INDEX evidence_item ON evidence(item_id);
CREATE INDEX evidence_fact ON evidence(fact_id);
CREATE INDEX evidence_relation ON evidence(relation_id);

CREATE TABLE communities (
  id INTEGER PRIMARY KEY,
  label TEXT,
  tier INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE community_members (
  community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
  entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  UNIQUE (community_id, entity_id)
);

-- ops ------------------------------------------------------------------------------------

CREATE TABLE query_log (
  ts TEXT NOT NULL,
  interface TEXT NOT NULL,                   -- cli | mcp | ui
  query TEXT NOT NULL,
  result_ids TEXT
);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
