"""Isolated exact cosine versus HNSW benchmark; never changes the application's tables/indexes.

Creates one random temporary schema, writes synthetic vectors in batches and drops ONLY that
schema in finally. The vector extension must already be installed. Large defaults are opt-in
via ALLOW_VECTOR_BENCHMARK_WRITES=1; smoke runs should use --sizes 100 --dimensions 16.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.cloud_common import required, revision, summary, write_report


def recall_at_k(actual, expected, k=10):
    truth = set(expected[:k])
    if not truth:
        raise ValueError("Exact neighbors must not be empty")
    return len(set(actual[:k]) & truth) / len(truth)


def vector_text(vector):
    return "[" + ",".join(format(float(x), ".9g") for x in vector) + "]"


def vectors(count, dimensions, seed, batch=500):
    """Deterministic unit-normal synthetic vectors; not CLIP embeddings or semantic labels."""
    import numpy as np
    rng = np.random.default_rng(seed)
    for start in range(0, count, batch):
        data = rng.standard_normal((min(batch, count - start), dimensions)).astype("float32")
        data /= np.linalg.norm(data, axis=1, keepdims=True)
        yield start, data


def plan_uses_index(plan, name):
    if isinstance(plan, list):
        return any(plan_uses_index(p, name) for p in plan)
    if isinstance(plan, dict):
        return plan.get("Index Name") == name or any(plan_uses_index(v, name) for v in plan.values())
    return False


def client_peak_rss_bytes():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if platform.system() == "Darwin" else value * 1024


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[10000, 50000, 100000, 500000])
    parser.add_argument("--dimensions", type=int, default=512)
    parser.add_argument("--queries", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260921)
    parser.add_argument("--ef-search", type=int, default=100)
    parser.add_argument("--statement-timeout-seconds", type=int, default=7200)
    parser.add_argument("--output", default="benchmarks/results/pgvector-comparison.json")
    args = parser.parse_args()
    if min(args.sizes) < 10 or not 1 <= args.dimensions <= 2000 or min(args.queries, args.repeats, args.statement_timeout_seconds) < 1 or args.ef_search < 10:
        parser.error("Need sizes>=10, dimensions 1..2000, positive query/repeat/timeout counts, ef-search>=10")
    report = {"kind": "synthetic-pgvector-exact-vs-hnsw", "status": "FAIL", "revision": revision(), "trials": [],
              "dataset": {"kind": "seeded independent standard-normal unit vectors", "semanticQuality": "unmeasured; no images, CLIP or relevance judgments", "seed": args.seed, "dimensions": args.dimensions},
              "configuration": {"queries": args.queries, "repeats": args.repeats, "efSearch": args.ef_search, "hnswM": 16, "hnswEfConstruction": 64},
              "memory": {"databasePeakResidentBytes": None, "status": "unmeasured", "reason": "SQL allocator snapshots and client RSS cannot establish RDS/server peak resident memory"},
              "latencyDefinition": "Warm-query client wall time including DB transport/fetch; exact before index creation, HNSW after creation; no cold cache claim"}
    schema = "vector_bench_" + uuid.uuid4().hex
    report["isolatedSchema"] = schema
    db = None
    created = False
    try:
        if os.getenv("ALLOW_VECTOR_BENCHMARK_WRITES") != "1":
            raise ValueError("Set ALLOW_VECTOR_BENCHMARK_WRITES=1 for isolated benchmark schema writes")
        import psycopg
        from psycopg import sql
        db = psycopg.connect(required("BENCHMARK_DATABASE_URL"), autocommit=True, connect_timeout=15)
        report["databaseVersion"] = db.execute("SELECT version()").fetchone()[0]
        extension = db.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()
        if not extension:
            raise ValueError("Install CREATE EXTENSION vector in the disposable benchmark database first")
        report["pgvectorVersion"] = extension[0]
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        created = True
        db.execute(sql.SQL("SET search_path TO {},public").format(sql.Identifier(schema)))
        db.execute("SELECT set_config('statement_timeout', %s, false)", (str(args.statement_timeout_seconds * 1000),))
        db.execute("SELECT set_config('hnsw.ef_search', %s, false)", (str(args.ef_search),))
        report["databaseSettings"] = {name: db.execute(sql.SQL("SHOW {}").format(sql.Identifier(name))).fetchone()[0]
                                      for name in ("shared_buffers", "work_mem", "maintenance_work_mem", "max_parallel_maintenance_workers")}
        import numpy as np
        queries = np.concatenate([data for _, data in vectors(args.queries, args.dimensions, args.seed + 1)])
        report["querySha256"] = hashlib.sha256(queries.tobytes()).hexdigest()
        for count in args.sizes:
            table = sql.Identifier(schema, "items")
            db.execute(sql.SQL("CREATE TABLE {} (id bigint PRIMARY KEY, embedding vector({}))").format(table, sql.Literal(args.dimensions)))
            digest = hashlib.sha256()
            inserted = time.perf_counter()
            with db.cursor().copy(sql.SQL("COPY {} (id,embedding) FROM STDIN").format(table)) as copy:
                for start, batch in vectors(count, args.dimensions, args.seed):
                    digest.update(batch.tobytes())
                    for index, vector in enumerate(batch, start + 1):
                        copy.write_row((index, vector_text(vector)))
            db.execute(sql.SQL("ANALYZE {}").format(table))
            trial = {"count": count, "corpusSha256": digest.hexdigest(), "insertSeconds": time.perf_counter() - inserted,
                     "tableBytes": db.execute("SELECT pg_total_relation_size(%s::regclass)", (schema + ".items",)).fetchone()[0]}
            report["trials"].append(trial)
            query = sql.SQL("SELECT id FROM {} ORDER BY embedding <=> %s::vector LIMIT 10").format(table)
            exact = []
            for mode in ("exact", "hnsw"):
                if mode == "hnsw":
                    started = time.perf_counter()
                    db.execute(sql.SQL("CREATE INDEX items_hnsw ON {} USING hnsw(embedding vector_cosine_ops) WITH (m=16,ef_construction=64)").format(table))
                    trial["hnswBuildSeconds"] = time.perf_counter() - started
                    trial["hnswIndexBytes"] = db.execute("SELECT pg_relation_size(%s::regclass)", (schema + ".items_hnsw",)).fetchone()[0]
                    db.execute("SET enable_seqscan=off")
                    db.execute("SET enable_indexscan=on")
                else:
                    db.execute("SET enable_seqscan=on")
                    db.execute("SET enable_indexscan=off")
                    db.execute("SET enable_bitmapscan=off")
                plan = db.execute(sql.SQL("EXPLAIN (FORMAT JSON) ") + query, (vector_text(queries[0]),)).fetchone()[0]
                if mode == "hnsw" and not plan_uses_index(plan, "items_hnsw"):
                    raise RuntimeError("HNSW plan did not use the intended index")
                rows = []
                # Warm every query equally before retaining timing samples.
                for vector in queries:
                    db.execute(query, (vector_text(vector),)).fetchall()
                for index, vector in enumerate(queries):
                    for repeat in range(args.repeats):
                        started = time.perf_counter()
                        neighbors = [row[0] for row in db.execute(query, (vector_text(vector),)).fetchall()]
                        elapsed = (time.perf_counter() - started) * 1000
                        if mode == "exact" and repeat == 0:
                            exact.append(neighbors)
                        rows.append({"queryIndex": index, "repeat": repeat, "latencyMs": elapsed, "neighbors": neighbors,
                                     "recall@10": recall_at_k(neighbors, exact[index])})
                allocator_bytes = None
                try:
                    allocator_bytes = db.execute("SELECT sum(total_bytes) FROM pg_backend_memory_contexts").fetchone()[0]
                except psycopg.Error:
                    pass  # Some hosted DB roles cannot inspect memory contexts.
                trial[mode] = {"latencyMs": summary([r["latencyMs"] for r in rows]), "recall@10": sum(r["recall@10"] for r in rows) / len(rows),
                               "plan": plan, "rawQueries": rows, "backendAllocatorSnapshotBytes": int(allocator_bytes) if allocator_bytes is not None else None}
                write_report(args.output, report)
            print(f"{count} vectors: exact/HNSW measured; HNSW Recall@10={trial['hnsw']['recall@10']:.4f}", flush=True)
            db.execute(sql.SQL("DROP TABLE {}").format(table))
        report["status"] = "PASS"
    except Exception as exc:
        report["fatalErrorType"] = type(exc).__name__
    finally:
        if db:
            if created:
                try:
                    from psycopg import sql
                    db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
                    report["cleanupStatus"] = "PASS"
                except Exception as exc:
                    report.update(cleanupStatus="FAIL", cleanupErrorType=type(exc).__name__, status="FAIL")
            db.close()
        report["clientPeakRssBytes"] = client_peak_rss_bytes()
        write_report(args.output, report)
    print(f"Vector benchmark: {report['status']}; report: {args.output}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
