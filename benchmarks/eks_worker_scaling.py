"""Explicit disposable EKS 1/2/4 fixed-replica cohorts with restoration in finally.

Rejects an HPA targeting the media Deployment; this is a controlled capacity trial,
not automatic-scaling validation. Prepared real backlog is drained by each cohort.
Zero replicas are used only while staging the disposable cohort. It requires the
same environment lock as release/fault experiments, and never controls ECS.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.cloud_common import aggregate, database_timings, fixture, register_user, summary, upload_one, write_report
from scripts.eks_common import eks_guard, kubectl, queue_snapshot


class WorkerControl:
    def __init__(self, control):
        self.control = control
        initial = control.state("media-worker")
        self.name, self.uid = initial["deployment"], initial["uid"]
        self.original_count = initial["replicas"]
        self.require_fixed_replicas()

    def require_fixed_replicas(self):
        hpas = self.control.json("get", "hpa", "-o", "json")["items"]
        if any(hpa["spec"].get("scaleTargetRef", {}).get("kind") == "Deployment" and
               hpa["spec"]["scaleTargetRef"].get("name") == self.name for hpa in hpas):
            raise ValueError("Fixed-replica experiment rejects a matching HPA; disable it through the reviewed dev release first")

    def state(self):
        state = self.control.state("media-worker")
        if state["uid"] != self.uid or state["deployment"] != self.name:
            raise ValueError("Deployment identity changed during the experiment")
        return state

    def request_scale(self, count):
        if os.getenv("ALLOW_EKS_SCALING") != "1":
            raise ValueError("Set ALLOW_EKS_SCALING=1 for explicitly authorized dev replica mutations")
        if type(count) is not int or count not in {0, 1, 2, 4, self.original_count}:
            raise ValueError("Unsupported replica count")
        self.require_fixed_replicas()  # Refuse concurrent autoscaling configuration changes.
        before = self.state()
        kubectl(self.control.arn, self.control.namespace, "scale", f"deployment/{self.name}",
                f"--replicas={count}", f"--current-replicas={before['replicas']}",
                f"--resource-version={before['resourceVersion']}")

    def scale(self, count, timeout=900):
        self.request_scale(count)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.state()
            nonterminating = [p for p in current["pods"] if not p["terminating"]]
            if (current["replicas"] == count and current["updatedReplicas"] == count and
                    current["readyReplicas"] == count and current["observedGeneration"] >= current["generation"] and
                    len(nonterminating) == count and all(p["ready"] for p in nonterminating) and
                    all(not p["terminating"] for p in current["pods"])):
                return current
            time.sleep(5)
        raise TimeoutError("EKS worker replicas did not settle within the allotted time")

    def restore(self):
        return self.scale(self.original_count)

    def resource_sample(self):
        try:
            # Kubernetes metrics-server is optional. Actual returned units are retained;
            # missing metrics are never inferred to be zero CPU/memory.
            raw = kubectl(self.control.arn, self.control.namespace, "top", "pods", "-l",
                          self.control.selector("media-worker"), "--containers", "--no-headers")
            rows = [line.split() for line in raw.splitlines() if line.strip()]
            if any(len(row) != 4 for row in rows):
                raise ValueError("Unexpected metrics-server output")
            return {"status": "measured" if rows else "unmeasured", "source": "metrics-server instantaneous samples",
                    "pods": [dict(zip(("pod", "container", "cpu", "memory"), row)) for row in rows],
                    "limitation": "Instantaneous CPU/memory; this does not measure peak memory or historical utilization"}
        except Exception as exc:
            return {"status": "unmeasured", "errorType": type(exc).__name__}


def trial_totals(trial):
    successful = sum(row["status"] == "READY" for row in trial.get("terminal", []))
    failed_terminal = sum(row["status"] != "READY" for row in trial.get("terminal", []))
    prepared = sum(row["success"] for row in trial.get("rawUploads", []))
    attempted = trial["attempted"]
    return {"attempted": attempted, "prepared": prepared, "successful": successful,
            "preparationFailedOrUnobserved": attempted - prepared, "terminalFailures": failed_terminal,
            "pendingOrUnobserved": max(0, prepared - successful - failed_terminal),
            "failedOrUnconfirmed": attempted - successful,
            "successRate": successful / attempted if attempted else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", nargs="+", type=int, default=[1, 2, 4])
    parser.add_argument("--images", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--output", default="benchmarks/results/eks-worker-scaling.json")
    args = parser.parse_args()
    if any(n not in {1, 2, 4} for n in args.counts) or len(args.counts) != len(set(args.counts)) or args.images < 1 or args.timeout < 1:
        parser.error("Use unique counts 1/2/4 and positive images/timeout")
    report = {"kind": "real-aws-eks-fixed-replica-worker-cohorts", "status": "FAIL", "matrixStatus": "INCOMPLETE",
              "trials": [], "cleanup": [], "definition": "Actual prepared MQ backlog; drain includes Pod startup and duplicates; fixed replicas do not certify HPA; failed/unconfirmed samples remain in denominator"}
    control = None
    changed = False
    uploads = []
    try:
        if os.getenv("ALLOW_EKS_SCALING") != "1":
            raise ValueError("Set ALLOW_EKS_SCALING=1")
        _, eks, report["provenance"] = eks_guard()
        control = WorkerControl(eks)
        report["restorePlan"] = {"clusterArn": eks.arn, "namespace": eks.namespace,
            "deployment": control.name, "deploymentUid": control.uid, "replicas": control.original_count,
            "autoscaling": "No matching HPA; rejected if one exists before any mutation"}
        write_report(args.output, report)  # Durable restoration state BEFORE the first mutation.
        report["initialQueue"] = queue_snapshot()
        if report["initialQueue"]["messages"] != 0:
            raise ValueError("Queue must be empty; refuse to interfere with existing work")
        payload = fixture()
        report["fixture"] = {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload), "kind": "fixed JPEG fixture"}
        for count in args.counts:
            trial = {"replicas": count, "attempted": args.images, "rawUploads": [], "terminal": [], "samples": [], "status": "FAIL"}
            report["trials"].append(trial)
            changed = True  # Even a timed-out API response may have scaled; always restore.
            control.scale(0)
            if queue_snapshot()["messages"] != 0:
                raise ValueError("Queue changed before cohort; refuse interference")
            apis = [register_user() for _ in range((args.images + 9) // 10)]
            def prepare(index):
                api = apis[index // 10]
                row, upload = upload_one(api, payload, wait_ready=False)
                return api, upload, row
            with ThreadPoolExecutor(max_workers=8) as pool:
                prepared = list(pool.map(prepare, range(args.images)))
            # Serialize only raw rows; upload objects include signed PUT URLs.
            trial["rawUploads"] = [row for _, _, row in prepared]
            trial["apiPreparation"] = aggregate(trial["rawUploads"])
            uploads.extend((api, upload) for api, upload, _ in prepared if upload)
            trial.update(trial_totals(trial))
            write_report(args.output, report)
            if any(not row["success"] for _, _, row in prepared):
                raise RuntimeError("Cohort preparation failed; all attempt rows retained")
            deadline = time.monotonic() + min(120, args.timeout)
            while queue_snapshot()["messages_ready"] < args.images:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Outbox did not publish the complete real cohort")
                time.sleep(2)
            trial["preparedQueue"] = queue_snapshot()
            if trial["preparedQueue"]["messages_unacknowledged"] != 0:
                raise ValueError("Unexpected active consumer while workers are stopped")
            started = time.monotonic()
            control.request_scale(count)
            pending = {upload["uploadId"]: (api, upload) for api, upload, _ in prepared}
            while True:
                for identity, (api, upload) in list(pending.items()):
                    status = api.state(upload)["status"]
                    if status in {"READY", "FAILED", "DELETED", "ABORTED"}:
                        trial["terminal"].append({"mediaId": upload["mediaId"], "status": status,
                            "observedTerminalSeconds": time.monotonic() - started})
                        del pending[identity]
                queue = queue_snapshot()
                trial["samples"].append({"elapsedSeconds": time.monotonic() - started,
                    "deployment": control.state(), "queue": queue, "pendingUploads": len(pending),
                    "resources": control.resource_sample()})
                trial.update(trial_totals(trial))
                write_report(args.output, report)
                if not pending and queue["messages"] == 0:
                    break
                if time.monotonic() - started > args.timeout:
                    raise TimeoutError("Cohort processing timed out")
                time.sleep(5)
            elapsed = time.monotonic() - started
            trial.update(trial_totals(trial))
            trial.update(queueDrainSecondsIncludingPodStartup=elapsed,
                         imagesPerMinute=trial["successful"] * 60 / elapsed,
                         readyObservationSecondsIncludingPodStartup=summary([row["observedTerminalSeconds"]
                             for row in trial["terminal"] if row["status"] == "READY"]),
                         database=database_timings([upload["mediaId"] for _, upload, _ in prepared]),
                         status="PASS" if trial["failedOrUnconfirmed"] == 0 else "FAIL")
            write_report(args.output, report)
            print(f"{count} EKS Pods: {trial['successful']}/{args.images} READY; {elapsed:.2f}s including startup", flush=True)
        report["status"] = "PASS" if all(trial["status"] == "PASS" for trial in report["trials"]) else "FAIL"
    except Exception as exc:
        report["fatalErrorType"] = type(exc).__name__
    finally:
        if control and changed:
            try:
                report["restoredDeployment"] = control.restore()
                report["restoreStatus"] = "PASS"
            except Exception as exc:
                report.update(restoreStatus="FAIL", restoreErrorType=type(exc).__name__, status="FAIL")
        for api, upload in uploads:
            try:
                api.cleanup(upload)
                report["cleanup"].append({"mediaId": upload["mediaId"], "status": "requested"})
            except Exception as exc:
                report["cleanup"].append({"mediaId": upload["mediaId"], "status": "FAIL", "errorType": type(exc).__name__})
                report["status"] = "FAIL"
        for trial in report["trials"]:
            trial.update(trial_totals(trial))
        write_report(args.output, report)
    print(f"EKS fixed-replica scaling: {report['status']}; report: {args.output}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
