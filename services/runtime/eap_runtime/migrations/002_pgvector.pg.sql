-- Postgres only: pgvector similarity search. Platform embedding dimension is 768
-- (text-embedding-005, or gemini-embedding-001 with outputDimensionality=768).
CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE knowledge_chunks ADD COLUMN embedding_vec vector(768);
CREATE INDEX knowledge_chunks_hnsw ON knowledge_chunks USING hnsw (embedding_vec vector_cosine_ops);
