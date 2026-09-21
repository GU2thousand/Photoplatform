This public trust bundle was fetched on 2026-09-21 from the official [Amazon RDS trust store](https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem). It contains public CA certificates, not credentials or a private signing key.

SHA-256: `e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3`.

Both worker images copy it to `/app/certs/global-bundle.pem`. Review AWS CA rotation notices, refresh this bundle from the same HTTPS source, and rebuild images before a required certificate rotation. RDS connections use `verify-full`, including hostname validation. See [RDS PostgreSQL TLS](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Concepts.General.SSL.html).
