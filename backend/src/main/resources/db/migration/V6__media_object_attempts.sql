-- Keep this registry after metadata/job deletion: an in-flight PUT from a
-- fenced worker can finish after an earlier deletion and must be swept again.
-- Deliberately omit cascading foreign keys and never delete registry entries.
CREATE TABLE media_object_attempts (
 claim_token uuid PRIMARY KEY,
 job_id uuid NOT NULL,
 media_id bigint NOT NULL,
 object_prefix varchar(600) NOT NULL UNIQUE,
 created_at timestamptz NOT NULL DEFAULT now(),
 next_cleanup_at timestamptz NOT NULL DEFAULT now()+interval '1 minute'
);
CREATE INDEX media_object_attempts_due ON media_object_attempts(next_cleanup_at,claim_token);
CREATE INDEX media_object_attempts_media ON media_object_attempts(media_id);
