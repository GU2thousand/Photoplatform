-- A brief, token-fenced publication lease supports multiple API schedulers.
-- Broker confirmation and the DB update still cannot be one atomic commit:
-- consumers must retain idempotency and publication remains at least once.
ALTER TABLE media_outbox ADD COLUMN publication_claim_token uuid;
ALTER TABLE media_outbox ADD COLUMN publication_lease_until timestamptz;
CREATE INDEX media_outbox_publication_lease ON media_outbox(publication_lease_until);
