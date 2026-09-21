ALTER TABLE image_assets ALTER COLUMN stored_file_name DROP NOT NULL;
ALTER TABLE image_assets ALTER COLUMN thumbnail_file_name DROP NOT NULL;
-- Legacy media retains its old path and access semantics. No objects are rewritten by migration.
ALTER TABLE image_assets ADD COLUMN processing_status varchar(20) NOT NULL DEFAULT 'READY';
ALTER TABLE image_assets ADD COLUMN embedding_status varchar(20) NOT NULL DEFAULT 'NOT_REQUESTED';
ALTER TABLE image_assets ADD COLUMN storage_layout varchar(20) NOT NULL DEFAULT 'LEGACY';
ALTER TABLE image_assets ADD COLUMN asset_version integer NOT NULL DEFAULT 1;
ALTER TABLE image_assets ADD COLUMN deleted_at timestamptz;
ALTER TABLE image_assets ADD COLUMN content_sha256 varchar(64);
ALTER TABLE image_assets ADD COLUMN perceptual_hash varchar(16);
ALTER TABLE image_assets ADD COLUMN width integer;
ALTER TABLE image_assets ADD COLUMN height integer;
ALTER TABLE image_assets ADD COLUMN safe_metadata jsonb NOT NULL DEFAULT '{}';
CREATE INDEX image_public_feed ON image_assets(created_at DESC,id DESC)
 WHERE visibility='PUBLIC' AND moderation_status='APPROVED' AND processing_status='READY' AND deleted_at IS NULL;
CREATE INDEX image_owner ON image_assets(uploader_id,created_at DESC);
CREATE INDEX image_team ON image_assets(team_id,created_at DESC);
CREATE INDEX image_content_hash ON image_assets(uploader_id,content_sha256);
CREATE INDEX image_text_search ON image_assets USING gin
 (to_tsvector('english',title || ' ' || description || ' ' || category || ' ' || tags));

CREATE TABLE upload_sessions (
 id uuid PRIMARY KEY, media_id bigint NOT NULL UNIQUE REFERENCES image_assets,
 owner_id bigint NOT NULL REFERENCES user_accounts, idempotency_key uuid NOT NULL,
 request_fingerprint varchar(64) NOT NULL, object_key varchar(600) NOT NULL UNIQUE,
 content_type varchar(100) NOT NULL, expected_bytes bigint NOT NULL CHECK(expected_bytes>0),
 expected_sha256 varchar(64) NOT NULL, expires_at timestamptz NOT NULL,
 completed_at timestamptz, cleaned_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(owner_id,idempotency_key)
);
CREATE INDEX upload_expiry ON upload_sessions(expires_at) WHERE cleaned_at IS NULL;
CREATE TABLE media_processing_jobs (
 id uuid PRIMARY KEY, media_id bigint NOT NULL REFERENCES image_assets,
 job_type varchar(20) NOT NULL CHECK(job_type IN ('MEDIA_PROCESS','EMBED','DELETE')),
 asset_version integer NOT NULL, pipeline_version varchar(80) NOT NULL,
 status varchar(20) NOT NULL DEFAULT 'QUEUED', attempt integer NOT NULL DEFAULT 0,
 next_attempt_at timestamptz NOT NULL DEFAULT now(), lease_until timestamptz,
 claim_token uuid, last_error_code varchar(80), traceparent varchar(100),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(media_id,job_type,asset_version,pipeline_version)
);
CREATE INDEX media_job_due ON media_processing_jobs(status,next_attempt_at,lease_until);
CREATE TABLE media_outbox (
 job_id uuid PRIMARY KEY REFERENCES media_processing_jobs ON DELETE CASCADE,
 last_published_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE media_variants (
 media_id bigint NOT NULL REFERENCES image_assets, asset_version integer NOT NULL,
 variant varchar(30) NOT NULL, object_key varchar(600) NOT NULL UNIQUE,
 content_type varchar(100) NOT NULL, size_bytes bigint NOT NULL,
 width integer, height integer, content_sha256 varchar(64) NOT NULL,
 PRIMARY KEY(media_id,asset_version,variant)
);
