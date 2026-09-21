# RDS PostgreSQL module

Creates a private, encrypted PostgreSQL 16 instance with an RDS-managed master
password secret. Supply private subnets in two Availability Zones and a security
group whose TCP 5432 ingress is restricted to the authorized ECS security groups.
The module exports only the secret ARN; it does not retrieve the password or put
the password in Terraform state.

TLS is required (`rds.force_ssl=1`). Clients should use the current Amazon RDS CA
bundle and hostname verification (`sslmode=verify-full`). The `postgres16`
parameter group restricts this module to major version 16. The major-only default
lets RDS select a supported minor version, and automatic minor upgrades remain
enabled. Configuration changes use the AWS maintenance-window default; this
module does not opt into `apply_immediately`.

The application migration must run `CREATE EXTENSION IF NOT EXISTS vector;` in
the `photoplatform` database. Terraform does not connect to PostgreSQL to install
extensions. Verify the chosen minor version against the
[RDS PostgreSQL extension matrix](https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-extensions.html)
before provisioning. Use the master role for bootstrap and migrations, then
provision a restricted application role for production. ECS secret injection is
resolved at task startup: coordinate task refresh with password rotation, or use
an application credential provider that refreshes credentials.

PostgreSQL and upgrade logs have 30-day CloudWatch retention. Queries taking at
least 500 ms are logged. Database Insights Standard collects seven days of
database and per-query metrics when `performance_insights_enabled=true`. The
Terraform/API field names retain "Performance Insights" even though the current
AWS console uses Database Insights. See
[AWS Standard mode configuration](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_DatabaseInsights.TurningOnStandard.html)
and the
[AWS provider resource schema](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/db_instance).
Query logs can include SQL literals, so restrict log access and avoid putting
sensitive values into literal SQL.

Defaults suit a disposable development environment: one `db.t4g.micro` instance,
20 GiB encrypted gp3 storage, autoscaling up to 100 GiB, seven-day automated
backups, no deletion protection and no final snapshot. A durable environment
should enable Multi-AZ and deletion protection and set `skip_final_snapshot=false`.
Disable deletion protection with a reviewed apply before destroying it.

When final snapshots are enabled, the default name is `${name}-final`. If you
recreate an environment and later delete it again, choose a fresh
`final_snapshot_identifier` before the second destroy (or preserve the old
snapshot under another name using a snapshot copy and remove the original only
after verifying the copy). RDS rejects duplicate final snapshot names. Retained
snapshots incur charges after the instance is deleted; review and remove them
when the retention requirement has ended.
