ALTER TABLE media_processing_jobs ADD COLUMN started_at timestamptz;
ALTER TABLE media_processing_jobs ADD COLUMN finished_at timestamptz;
ALTER TABLE media_processing_jobs ADD COLUMN worker_id varchar(120);
