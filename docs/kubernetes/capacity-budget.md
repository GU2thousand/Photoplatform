# Initial capacity and database connection budget

These are conservative configuration candidates, not measured EKS throughput,
latency, memory usage or cost. The deployment overlay must record the actual RDS
connection limit, reserved connections, other application clients and the
allocatable application budget. Chart rendering rejects a computed upper bound
above `capacity.databaseConnectionBudget`.

Let `A` be API HPA maximum (or fixed replicas), `P` API `DB_POOL_MAX`, `M` media
worker maximum, and `E` embedding worker maximum when ML is enabled. Every
Deployment has `maxSurge=1`. Use:

```text
API:        (A + 1 rollout Pod) × P
Workers:    (M + 1 rollout Pod + [E + 1 rollout Pod if ML]) × 4
Reserve:    probeReserve + migrationReserve + administrativeReserve
Total:      API + Workers + Reserve ≤ allocatable application DB budget
```

The worker multiplier covers a long-lived job/advisory-lock session, independent
attempt-prefix garbage collector, renewal/session activity and transient readiness
connection. Renewal currently uses the job session, making four conservative.
The separate probe reserve covers other overlap/health/admin uncertainty; reduce
it only with retained measurements. Worker session advisory locks require direct
stable RDS sessions and are incompatible with transaction pooling assumptions.
Encoder and queue collector do not connect to the database.

| Candidate | API upper bound | Media incl. surge | Reserve | Total | Budget |
| --- | --- | --- | --- | --- | --- |
| Dev, fixed API 2, media max 4, ML off | `3×10=30` | `5×4=20` | `8+2+10=20` | 70 | 80 |
| Prod, HPA API max 4, media max 4, ML off | `5×10=50` | `5×4=20` | 20 | 90 | 100 |
| Same production with embedding max 4 | 50 | `2×5×4=40` | 20 | 110 | supply at least 110 |

Fixed replicas do not mean manual changes are free: the declared worker maxima
are release/harness limits, and Kubernetes `kubectl scale` can bypass Helm
validation. Restrict scaling to the bounded release/test role and review any
manual update. A generic privileged cluster operator can also bypass the chart;
this is a calculation guard, not an admission controller. API HPA is resource
based with CPU/memory requests and 300-second scale-down stabilization. Worker
backlog autoscaling is intentionally disabled until the real collector/adapter,
missing-data policy and drain measurements are proven. Queue sample failure must
not become a zero workload observation.

Compute requests at the above maximum including rollout Pods, telemetry and
migrator must fit across the node groups and AZs, after kube-system/daemonset
overhead. General API requests 500m/768Mi, media 500m/512Mi; candidate limits are
2 CPU/1Gi each. API JVM gets 65% MaxRAMPercentage with 1Gi container limit so native
memory has room; actual peak RSS, GC and OOM evidence remain required. ML requests
2 CPU/3Gi and limits 4 CPU/6Gi per encoder/embedding Pod, each with 5Gi ephemeral
model cache. Separate ML nodes and toleration prevent model cold load pressure
from displacing API Pods. Budget disk/NAT download pressure and cache eviction
alongside memory; caches are not business data.

ALB readiness checks 8080 `/readyz`; management stays internal. API termination
budget is 15s preStop + 30s target deregistration + 30s Spring shutdown with 90s
grace. The conservative sum leaves 15s for propagation/scheduling. Workers get
150s grace and 100s drain with 50s margin; their preStop does not consume that
budget. Startup budgets are 5 minutes JVM/media and up to 20 minutes ML readiness.
Replace these with cold-start/task duration distributions, with failures and
timeouts included, before claiming production fit.

For the first dev experiment record 1/2/4-worker runs with image classes and sizes,
request/timeout/error denominators, p50/p95/p99, raw queue ready/inflight depth,
Ready Pod counts, DB connections/pool waits, peak memory, model hash, drain time
and shutdown events. Separate fixed-scale experiment evidence from an automatic
scaling claim. The EKS harness must retain cluster/namespace/Pod UID/image digest
and may never reuse ECS task counts or controller actions as Pod evidence.
