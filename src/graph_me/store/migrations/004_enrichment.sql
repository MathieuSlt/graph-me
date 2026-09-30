-- v4: Tier 1 enrichment.
-- Chunks written by enrichment (keywords, topics) are marked with their tier, so they can be
-- told apart from text extracted from the item itself. Vectors live in `chunks_vec`, created by
-- the embeddings step only when the sqlite-vec extension is available.
ALTER TABLE chunks ADD COLUMN tier INTEGER NOT NULL DEFAULT 0;
