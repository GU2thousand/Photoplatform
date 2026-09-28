"""Interrupt one exact disposable media worker with graceful Pod deletion.

Requires an explicitly identified RUNNING MEDIA_PROCESS job whose worker_id is the
selected Pod hostname. Graceful deletion is a shutdown/recovery scenario, not proof
of SIGKILL, after-S3 crash, fencing under DB loss or the complete P6 fault matrix.
The sigkill option fails closed: Linux namespace PID 1 cannot be killed from the
same namespace. It needs a separately reviewed ancestor/CRI operator helper with
exact Pod UID and container-ID checks, which this script does not implement.
"""
import argparse
import os
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.cloud_common import CloudAPI, database_timings, required, write_report
from scripts.eks_common import eks_guard, kubectl, safe_name


def running_job(media_id, job_id, pod_name):
    """Read-only ownership correlation; never write or expose the DB URL/claim token."""
    import psycopg
    with psycopg.connect(required("BENCHMARK_DATABASE_URL"), connect_timeout=10) as db:
        db.execute("SET TRANSACTION READ ONLY")
        db.execute("SET LOCAL statement_timeout='15s'")
        rows = db.execute("""SELECT id,media_id,status,worker_id,attempt,lease_until,claim_token IS NOT NULL,lease_until>now(),
            extract(epoch FROM now()-started_at)
            FROM media_processing_jobs WHERE id=%s AND media_id=%s AND job_type='MEDIA_PROCESS'""",
            (uuid.UUID(job_id), media_id)).fetchall()
    if (len(rows) != 1 or rows[0][2] != "RUNNING" or rows[0][3] != pod_name or not rows[0][6] or not rows[0][7]):
        raise ValueError("Exact media processing job must be RUNNING and owned by the selected Pod hostname")
    result = dict(zip(("jobId", "mediaId", "status", "workerId", "attempt", "leaseUntil"), rows[0][:6]))
    result["currentAttemptAgeSeconds"] = float(rows[0][8]) if rows[0][8] is not None else None
    return result


def delete_exact_pod(control, name, uid):
    if os.getenv("ALLOW_EKS_FAILURE_INJECTION") != "1":
        raise ValueError("Set ALLOW_EKS_FAILURE_INJECTION=1 for a single disposable Pod deletion")
    safe_name(name)
    uuid.UUID(uid)
    matches = [p for p in control.pods("media-worker") if p["name"] == name and p["uid"] == uid]
    if len(matches) != 1 or matches[0]["phase"] != "Running" or matches[0]["terminating"]:
        raise ValueError("Selected UID must identify one running member of the verified media worker Deployment")
    # Ordinary kubectl delete -f does not send a UID precondition. Raw DELETE
    # supplies DeleteOptions to the API server, closing the lookup/delete race.
    body = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid},
            "propagationPolicy": "Background"}
    kubectl(control.arn, control.namespace, "delete", "--raw",
            f"/api/v1/namespaces/{control.namespace}/pods/{name}", "-f", "-", body=body)
    return {"pod": matches[0], "deleteOptions": body,
            "semantics": "Default Pod grace period; deletion is not proof of immediate process termination"}


def kill_exact_worker(control, name, uid):
    if os.getenv("ALLOW_EKS_FAILURE_INJECTION") != "1":
        raise ValueError("Set ALLOW_EKS_FAILURE_INJECTION=1")
    # A receipt from a same-PID-namespace os.kill(1, SIGKILL) is not evidence that
    # the Linux namespace init received a fatal signal. Never issue that command
    # or attribute a coincidental Kubernetes restart to a successful SIGKILL.
    raise RuntimeError("UNSUPPORTED_ANCESTOR_SIGNAL_REQUIRED")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pod-name", required=True)
    parser.add_argument("--pod-uid", required=True)
    parser.add_argument("--upload-id", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--fault-mode", choices=("delete", "sigkill"), default="delete")
    parser.add_argument("--minimum-job-age-seconds", type=int, default=0,
                        help="Set above 300 to require a real task longer than the original five-minute lease")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--output", default="benchmarks/results/eks-worker-failure.json")
    args = parser.parse_args()
    if args.timeout < 1 or args.minimum_job_age_seconds < 0:
        parser.error("timeout must be positive and minimum job age nonnegative")
    report = {"kind": "real-aws-eks-single-worker-fault", "faultMode": args.fault_mode,
              "status": "FAIL", "matrixStatus": "INCOMPLETE", "faultIssued": False,
              "limitation": "Graceful deletion or a signal command may race job completion; DB loss, after-S3 crash and full fault matrix require separate evidence"}
    try:
        if os.getenv("ALLOW_EKS_FAILURE_INJECTION") != "1":
            raise ValueError("Set ALLOW_EKS_FAILURE_INJECTION=1")
        if args.fault_mode == "sigkill":
            # Unsupported capability is reported before any AWS/Pod lookup or
            # business request. There is no in-container SIGKILL fallback.
            kill_exact_worker(None, args.pod_name, args.pod_uid)
        _, control, report["provenance"] = eks_guard()
        api = CloudAPI()
        upload = {"uploadId": args.upload_id}
        state = api.state(upload)
        upload["mediaId"] = state["mediaId"]
        if state["status"] != "PROCESSING":
            raise ValueError("Selected upload must be PROCESSING before the fault")
        report["jobBefore"] = running_job(state["mediaId"], args.job_id, args.pod_name)
        age = report["jobBefore"]["currentAttemptAgeSeconds"]
        if age is None or age < args.minimum_job_age_seconds:
            raise ValueError("The selected running job does not meet the requested measured attempt age")
        report["longerThanOriginalFiveMinuteLease"] = age > 300
        report["before"] = control.state("media-worker")
        write_report(args.output, report)
        report["fault"] = (kill_exact_worker if args.fault_mode == "sigkill" else delete_exact_pod)(control, args.pod_name, args.pod_uid)
        report["faultIssued"] = True
        report["faultIssuedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        write_report(args.output, report)
        started = time.monotonic()
        api.wait(upload, timeout=args.timeout)
        after = database_timings([state["mediaId"]])
        if len(after["rows"]) != 1 or after["rows"][0]["status"] != "DONE":
            raise AssertionError("Recovery must yield exactly one DONE media processing job")
        report["claimReclaimedAfterFault"] = after["rows"][0]["attempt"] > report["jobBefore"]["attempt"]
        if args.fault_mode == "sigkill" and not report["claimReclaimedAfterFault"]:
            raise AssertionError("SIGKILL scenario did not prove interrupted job reclamation; job may have completed before the signal")
        deadline = time.monotonic() + args.timeout
        while True:
            recovered = control.state("media-worker")
            active = [p for p in recovered["pods"] if p["ready"] and not p["terminating"]]
            old = next((p for p in report["before"]["pods"] if p["uid"] == args.pod_uid), None)
            new = next((p for p in recovered["pods"] if p["uid"] == args.pod_uid), None)
            fault_recovered = (all(p["uid"] != args.pod_uid for p in recovered["pods"]) if args.fault_mode == "delete"
                               else old is not None and new is not None and new["restarts"] > old["restarts"])
            if (recovered["uid"] == report["before"]["uid"] and recovered["replicas"] == report["before"]["replicas"] and
                    len(active) == recovered["replicas"] and fault_recovered):
                break
            if time.monotonic() > deadline:
                raise TimeoutError("Verified Deployment did not replace the selected Pod and recover its replicas")
            time.sleep(5)
        report.update(status="PASS", after=after, recoverySeconds=time.monotonic() - started, recoveredDeployment=recovered)
    except Exception as exc:
        report["fatalErrorType"] = type(exc).__name__
        if isinstance(exc, RuntimeError) and str(exc) == "UNSUPPORTED_ANCESTOR_SIGNAL_REQUIRED":
            report["unsupportedScope"] = "UNSUPPORTED_ANCESTOR_SIGNAL_REQUIRED"
            report["requiredHelper"] = "Separately reviewed helper outside the container PID namespace, with exact Pod UID and CRI container ID guards"
    finally:
        write_report(args.output, report)
    print(f"Selected EKS worker fault: {report['status']}; report: {args.output}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
