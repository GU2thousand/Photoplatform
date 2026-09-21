"""Closed-loop real AWS upload cohorts; failures remain in the raw denominator."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
from threading import Barrier
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.cloud_common import aggregate, cloud_guard, cost_per_thousand, database_timings, fixture, register_user, upload_one, write_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", nargs="+", type=int, default=[20, 50, 100])
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--cost-evidence", help="JSON: totalUSD, source, allocationMethod, intervalStart, intervalEnd; matching this run's scope")
    parser.add_argument("--output", default="benchmarks/results/cloud-uploads.json")
    args = parser.parse_args()
    if min(args.users + [args.iterations, args.timeout]) < 1 or max(args.users) > 100:
        parser.error("Use 1..100 users and positive iterations/timeout")
    report = {"kind": "real-aws-upload-load", "status": "FAIL", "cohorts": [], "cleanup": [],
              "workload": "Closed-loop: each user waits for READY before the next iteration; not an open-loop capacity estimate",
              "latencyDefinition": "Client wall clock includes network; completionToReadyObservedMs includes queue + worker + <=1s polling, not worker processing duration"}
    uploads = []
    try:
        _, report["provenance"] = cloud_guard()
        payload = fixture()
        report["fixture"] = {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload), "source": "benchmarks/fixtures/upload.jpg", "kind": "fixed JPEG fixture, not real-user size distribution"}
        for users in args.users:
            apis = [register_user() for _ in range(users)]
            barrier = Barrier(users)
            def virtual_user(item):
                index, api = item
                results = []
                barrier.wait()
                for iteration in range(args.iterations):
                    row, upload = upload_one(api, payload, args.timeout)
                    row.update(virtualUser=index, iteration=iteration)
                    results.append(row)
                    if upload:
                        uploads.append((api, upload))
                return results
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=users) as pool:
                rows = [row for group in pool.map(virtual_user, enumerate(apis)) for row in group]
            elapsed = time.monotonic() - started
            cohort = {"users": users, "iterationsPerUser": args.iterations, "elapsedSeconds": elapsed,
                      "summary": aggregate(rows), "rawAttempts": rows}
            cohort["successfulImagesPerMinute"] = cohort["summary"]["successful"] * 60 / elapsed
            report["cohorts"].append(cohort)
            write_report(args.output, report)
            # Database observation is separate: its failure must not erase completed work.
            cohort["database"] = database_timings([r["mediaId"] for r in rows if "mediaId" in r])
            write_report(args.output, report)
            print(f"{users} users: {cohort['summary']['successful']}/{len(rows)} READY", flush=True)
        denominator = sum(c["summary"]["successful"] for c in report["cohorts"])
        report["cost"] = {"status": "unmeasured", "successfulImages": denominator, "usdPer1000SuccessfulImages": None}
        if args.cost_evidence:
            source = Path(args.cost_evidence).read_bytes()
            cost = json.loads(source)
            for name in ("source", "allocationMethod", "intervalStart", "intervalEnd", "totalUSD"):
                if name not in cost:
                    raise ValueError("Incomplete cost evidence")
            report["cost"] = {"status": "operator-supplied-allocation", **cost, "evidenceSha256": hashlib.sha256(source).hexdigest(),
                              "successfulImages": denominator, "usdPer1000SuccessfulImages": cost_per_thousand(float(cost["totalUSD"]), denominator)}
        report["status"] = "PASS" if all(c["summary"]["failed"] == 0 for c in report["cohorts"]) else "FAIL"
    except Exception as exc:
        report["fatalErrorType"] = type(exc).__name__
    finally:
        for api, upload in uploads:
            row = {"mediaId": upload["mediaId"]}
            try:
                api.cleanup(upload)
                row["status"] = "requested"
            except Exception as exc:
                row.update(status="FAIL", errorType=type(exc).__name__)
                report["status"] = "FAIL"
            report["cleanup"].append(row)
        report["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        write_report(args.output, report)
    print(f"Cloud upload benchmark: {report['status']}; report: {args.output}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
