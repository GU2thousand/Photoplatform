"""Controlled 1/2/4/8 ECS worker cohort benchmark, restoring service/autoscaling in finally."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sys
import time
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.cloud_common import cloud_guard, database_timings, fixture, register_user, required, secure_origin, upload_one, write_report


def queue_snapshot():
    import requests
    base = secure_origin(required("RABBITMQ_MANAGEMENT_URL"))
    virtual_host = quote(os.getenv("RABBITMQ_VHOST", "/"), safe="")
    queue = quote(os.getenv("RABBITMQ_PROCESS_QUEUE", "media.process"), safe="")
    response = requests.get(f"{base}/api/queues/{virtual_host}/{queue}",
                            auth=(required("RABBITMQ_USERNAME"), required("RABBITMQ_PASSWORD")), timeout=15)
    if response.status_code != 200:
        raise RuntimeError("Queue telemetry unavailable")
    data = response.json()
    return {name: data.get(name) for name in ("messages", "messages_ready", "messages_unacknowledged", "consumers")}


class ServiceControl:
    def __init__(self, aws):
        self.ecs = aws.client("ecs")
        self.scaler = aws.client("application-autoscaling")
        self.cluster = required("ECS_CLUSTER")
        self.service = required("ECS_WORKER_SERVICE")
        response = self.ecs.describe_clusters(clusters=[self.cluster], include=["TAGS"])
        if response.get("failures") or len(response["clusters"]) != 1:
            raise ValueError("ECS cluster not found")
        tags = {t["key"]: t["value"] for t in response["clusters"][0].get("tags", [])}
        if any(tags.get(k) != v for k, v in {"Project": "photoplatform", "Environment": "dev", "DisposableEnvironment": "true"}.items()):
            raise ValueError("ECS cluster is not tagged as disposable photoplatform dev")
        self.original_count = self.state()["desiredCount"]
        self.resource = f"service/{self.cluster.split('/')[-1]}/{self.service.split('/')[-1]}"
        self.target = self.scaler.describe_scalable_targets(ServiceNamespace="ecs", ResourceIds=[self.resource],
            ScalableDimension="ecs:service:DesiredCount")["ScalableTargets"]
        if len(self.target) > 1:
            raise ValueError("Unexpected multiple autoscaling targets")
        self.target = self.target[0] if self.target else None

    def state(self):
        result = self.ecs.describe_services(cluster=self.cluster, services=[self.service])
        if result.get("failures") or len(result["services"]) != 1:
            raise ValueError("ECS service not found")
        item = result["services"][0]
        if item["status"] != "ACTIVE" or item.get("launchType") not in {None, "FARGATE"}:
            raise ValueError("Expected active Fargate service")
        return {name: item[name] for name in ("desiredCount", "runningCount", "pendingCount", "taskDefinition")}

    def suspend(self):
        if self.target:
            self.scaler.register_scalable_target(ServiceNamespace="ecs", ResourceId=self.resource,
                ScalableDimension="ecs:service:DesiredCount", MinCapacity=0,
                MaxCapacity=max(8, self.target["MaxCapacity"]), SuspendedState={
                    "DynamicScalingInSuspended": True, "DynamicScalingOutSuspended": True, "ScheduledScalingSuspended": True})

    def scale(self, count):
        self.ecs.update_service(cluster=self.cluster, service=self.service, desiredCount=count)
        until = time.monotonic() + 900
        while time.monotonic() < until:
            current = self.state()
            if current["runningCount"] == count and current["pendingCount"] == 0:
                return current
            time.sleep(5)
        raise TimeoutError("ECS worker scaling did not settle in 15 minutes")

    def restore(self):
        # Restore desired count before re-enabling policy decisions.
        try:
            self.scale(self.original_count)
        finally:
            # Capacity launch failures must not leave the real scaling policy suspended.
            if self.target:
                self.scaler.register_scalable_target(ServiceNamespace="ecs", ResourceId=self.resource,
                    ScalableDimension="ecs:service:DesiredCount", MinCapacity=self.target["MinCapacity"],
                    MaxCapacity=self.target["MaxCapacity"], SuspendedState=self.target.get("SuspendedState", {
                        "DynamicScalingInSuspended": False, "DynamicScalingOutSuspended": False, "ScheduledScalingSuspended": False}))


def resource_metrics(aws, control, start, end):
    cloudwatch = aws.client("cloudwatch")
    output = {}
    for metric in ("CPUUtilization", "MemoryUtilization"):
        result = cloudwatch.get_metric_statistics(Namespace="AWS/ECS", MetricName=metric,
            Dimensions=[{"Name": "ClusterName", "Value": control.cluster.split("/")[-1]},
                        {"Name": "ServiceName", "Value": control.service.split("/")[-1]}],
            StartTime=datetime.fromtimestamp(start, timezone.utc), EndTime=datetime.fromtimestamp(end, timezone.utc),
            Period=60, Statistics=["Average", "Maximum"], Unit="Percent")
        # Minute datapoints overlapping startup/previous cohorts are excluded, never labeled this trial's CPU.
        points = sorted((p for p in result["Datapoints"] if start <= p["Timestamp"].timestamp() and p["Timestamp"].timestamp() + 60 <= end), key=lambda p: p["Timestamp"])
        output[metric] = {"status": "measured" if points else "unmeasured",
                          "datapoints": points, "unit": "percent", "periodSeconds": 60}
    return output


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--counts", nargs="+", type=int, default=[1, 2, 4, 8])
    p.add_argument("--images", type=int, default=100)
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--metrics-delay", type=int, default=90, help="Allow CloudWatch publication after each cohort")
    p.add_argument("--output", default="benchmarks/results/ecs-worker-scaling.json")
    args = p.parse_args()
    if any(n not in {1, 2, 4, 8} for n in args.counts) or args.images < 1 or args.timeout < 1 or args.metrics_delay < 0:
        p.error("Use counts 1/2/4/8, positive images/timeout and nonnegative metrics delay")
    report = {"kind": "real-aws-ecs-worker-scaling", "status": "FAIL", "trials": [], "cleanup": [],
              "definition": "Prepared backlog; queue drain includes Fargate startup; DB processing latency is final attempt only; API polling adds observer load"}
    control = None
    changed = False
    uploads = []
    try:
        if os.getenv("ALLOW_ECS_SCALING") != "1":
            raise ValueError("Set ALLOW_ECS_SCALING=1 for explicit worker service mutations")
        aws, report["provenance"] = cloud_guard()
        control = ServiceControl(aws)
        report["restorePlan"] = {"cluster": control.cluster, "service": control.service, "desiredCount": control.original_count,
                                 "scalableTarget": control.target}
        write_report(args.output, report)  # Persist restoration data before changing anything.
        initial_queue = queue_snapshot()
        if initial_queue.get("messages") != 0:
            raise ValueError("Worker queue must be empty before benchmark; refuse interference with existing work")
        changed = True
        control.suspend()
        payload = fixture()
        report["fixture"] = {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload), "kind": "fixed JPEG fixture"}
        for count in args.counts:
            control.scale(0)
            # <=10 outstanding uploads per account preserves the application's normal owner quota.
            apis = [register_user() for _ in range((args.images + 9) // 10)]
            def prepare(index):
                api = apis[index // 10]
                row, upload = upload_one(api, payload, wait_ready=False)
                if upload:
                    uploads.append((api, upload))
                return api, upload, row
            with ThreadPoolExecutor(max_workers=8) as pool:
                prepared = list(pool.map(prepare, range(args.images)))
            trial = {"tasks": count, "attempted": args.images, "rawUploads": [r for _, _, r in prepared], "samples": []}
            report["trials"].append(trial)
            write_report(args.output, report)
            if any(not row["success"] for _, _, row in prepared):
                raise RuntimeError("Cohort preparation failed; raw attempts preserved")
            until = time.monotonic() + 120
            while queue_snapshot()["messages_ready"] < args.images:
                if time.monotonic() > until:
                    raise TimeoutError("Outbox did not publish the complete cohort")
                time.sleep(2)
            started = time.monotonic()
            epoch = time.time()
            # Poll ourselves so startup is represented in queue/resource samples.
            control.ecs.update_service(cluster=control.cluster, service=control.service, desiredCount=count)
            pending = {u["uploadId"]: (api, u) for api, u, _ in prepared}
            terminal = []
            trial["terminal"] = terminal  # Keep partial recovery evidence even when the cohort times out.
            while True:
                for identity, (api, upload) in list(pending.items()):
                    state = api.state(upload)["status"]
                    if state in {"READY", "FAILED", "DELETED", "ABORTED"}:
                        terminal.append({"mediaId": upload["mediaId"], "status": state, "observedReadySeconds": time.monotonic() - started})
                        del pending[identity]
                queue = queue_snapshot()
                trial["samples"].append({"elapsedSeconds": time.monotonic() - started,
                                         "ecs": control.state(), "queue": queue, "pendingUploads": len(pending)})
                write_report(args.output, report)
                if not pending and queue.get("messages") == 0:
                    break  # Drain includes any duplicate deliveries, not just the last READY poll.
                if time.monotonic() - started > args.timeout:
                    raise TimeoutError("Cohort processing timed out")
                time.sleep(5)
            elapsed, ended = time.monotonic() - started, time.time()
            successful = sum(row["status"] == "READY" for row in terminal)
            trial.update(successful=successful, failed=args.images - successful, terminal=terminal,
                         queueDrainSecondsIncludingTaskStartup=elapsed, imagesPerMinute=successful * 60 / elapsed)
            write_report(args.output, report)
            trial["database"] = database_timings([u["mediaId"] for _, u, _ in prepared])
            time.sleep(args.metrics_delay)
            trial["resources"] = resource_metrics(aws, control, epoch, ended)
            write_report(args.output, report)
            print(f"{count} ECS tasks: {successful}/{args.images} READY; {elapsed:.2f}s incl. startup", flush=True)
        report["status"] = "PASS" if all(t["failed"] == 0 for t in report["trials"]) else "FAIL"
    except Exception as exc:
        report["fatalErrorType"] = type(exc).__name__
    finally:
        if control and changed:
            try:
                control.restore()
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
        write_report(args.output, report)
    print(f"ECS scaling benchmark: {report['status']}; report: {args.output}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
