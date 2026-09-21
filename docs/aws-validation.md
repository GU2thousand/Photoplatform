# AWS upgrade validation — 2026-09-21

Baseline: `9396f42a09ac063d3cf217dfe9daf7beb88afef3` on `GU2thousand/Photoplatform/main`, after the previous cloud-media PR was merged. The prior [local validation record](validation.md) is historical evidence and is not an AWS benchmark.

## Executed in this upgrade

| Check | Result |
|---|---|
| Backend | 72 tests collected: 71 passed, 1 opt-in real-Rabbit test skipped, 0 failures/errors. Includes RSA signature/tampering, S3 request binding, AWS config/TLS rejection, authorization, publisher confirms, JDBC/Hikari metrics and cross-replica relay logic. |
| Frontend | Locked install, TypeScript/Vite build, 15 tests, dependency audit passed; zero reported vulnerabilities. |
| Worker | 46 tests passed: provider/TLS config, bounded retries, RabbitMQ 4.3 QoS, pending delivery tracking, shutdown/recovery, real DB readiness probing, version deletion and controlled crash boundaries. |
| CloudWatch collector | 33 tests passed: actual running-task denominator, ready + unacknowledged backlog, stale/missing samples, bounded requests, credential-safe failures and recent-publication readiness. |
| Benchmark calculations / safety | 15 tests passed: raw denominators, latency/cost units, scaling restoration, URL guards, ANN plan/recall and duplicate-safe relevance metrics. |
| Deployment helper | 8 tests passed: digest-only promotion, exact Terraform SSM baseline, sidecar/config preservation, embedding image mapping and rollback rejection. |
| Terraform | Recursive formatting, root/dev/prod validation, and 5 mocked provider plan tests passed with Terraform 1.12.2 and locked AWS provider 6.66.0. |
| Workflow / documentation | actionlint 1.7.7 passed all workflows; repository documentation links resolve; whitespace checks passed. |
| Compose | `docker compose config --quiet` accepted the explicit MinIO configuration before the Docker daemon became unavailable. No new local full-container run is claimed. |
| AWS preflight | The locally configured default profile returned `InvalidClientTokenId` from STS. No account resources were created or changed. |

Local Java verification used JDK 18 while production images and GitHub CI target Java 17; local frontend checks used Node 25.5.0 while CI selects Node 22. Python suites used Python 3.12. GitHub's pipeline integration job builds the actual images and runs PostgreSQL/pgvector, MinIO and RabbitMQ **4.3.6**, direct upload/permission/deletion checks, failure recovery, broker outage, legacy migration, a real cross-instance relay check and a small exact/HNSW execution test. Consult the final PR's **current commit** checks and retained `pipeline-verification-logs` artifact for the actual CI outcome; workflow configuration alone is not a passed run.

No synthetic cloud performance numbers are included. The earlier local benchmark reports remain in place with their original scope.

## Cloud gates still requiring a target account

No real Amazon S3, CloudFront, RDS, Amazon MQ, or ECS deployment was exercised during the identity preflight. A successful Terraform validation is provider-schema validation, not a successful cloud plan/apply. No IAM execution, task-role authorization, TLS endpoint connectivity, edge-cache signature behavior, RDS extension installation, broker version compatibility, automatic task scaling, cloud failure injection, or cost measurement is established by local tests.

The executable cloud harnesses and runbooks are in [cloud-benchmarks.md](cloud-benchmarks.md) and [failure-testing.md](failure-testing.md). They intentionally require a disposable environment and explicit mutation settings. Run them after provisioning and preserve successful and failed raw results. A skipped cloud test is not a passed phase.

## Interpreting the roadmap

The user supplied a new Phase 0–12 AWS roadmap. Do not reuse the previous roadmap's “7 complete, 3 partial” count: it described a different ten-phase local/cloud-media roadmap. For this AWS roadmap, code availability, CI validation, actual cloud deployment, and measured acceptance must be tracked independently using [the phase gate table](aws-production.md#acceptance-matrix-for-this-roadmap).

HNSW is an optional isolated benchmark only. Existing application search continues to use exact cosine distance. No 10k–500k ANN quality or latency results are claimed without a preserved run, and no cloud cost per 1,000 uploads is reported without an actual cost window and successful-image denominator.
