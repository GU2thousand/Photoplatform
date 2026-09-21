CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE media_embeddings (
 media_id bigint NOT NULL REFERENCES image_assets,
 asset_version integer NOT NULL, model_version varchar(80) NOT NULL,
 embedding vector(512) NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(media_id,asset_version,model_version)
);
-- Exact search is intentional for the initial permission-filtered corpus.
-- Add an ANN index only after measuring filtered recall against this baseline.
