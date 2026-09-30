-- v3: attachments of messages, to link a file on disk to the email it came with.

CREATE TABLE item_attachments (
  item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  content_hash TEXT,                         -- sha256, same as items.content_hash of files
  filename TEXT,
  mime_type TEXT
);
CREATE INDEX item_attachments_item ON item_attachments(item_id);
CREATE INDEX item_attachments_hash ON item_attachments(content_hash);
CREATE INDEX items_content_hash ON items(content_hash);
