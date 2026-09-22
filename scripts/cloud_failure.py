"""Stop exactly one verified disposable ECS worker task; preserve recovery evidence.

Does not disable an entire broker/database or mutate security groups. Runbooks describe
isolated network faults and mandatory restoration separately.
"""
import argparse
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.cloud_common import CloudAPI, cloud_guard, database_timings, required, write_report
from benchmarks.ecs_worker_scaling import ServiceControl


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-arn", required=True)
    parser.add_argument("--upload-id", required=True)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--output", default="benchmarks/results/cloud-worker-failure.json")
    args = parser.parse_args()
    report = {"kind": "real-aws-single-worker-stop", "status": "FAIL",
              "limitation": "StopTask allows graceful termination; task may finish before exiting. Service membership is verified but job ownership is not; correlate logs or use the targeted after-S3 crash hook for abrupt interruption proof"}
    try:
        if os.getenv("ALLOW_ECS_FAILURE_INJECTION") != "1":
            raise ValueError("Set ALLOW_ECS_FAILURE_INJECTION=1 to stop exactly one selected dev worker task")
        aws, report["provenance"] = cloud_guard()
        service = ServiceControl(aws)  # Validates cluster tags; does not change desired count/scaling.
        api = CloudAPI()
        upload = {"uploadId": args.upload_id}
        state = api.state(upload)
        upload["mediaId"] = state["mediaId"]
        if state["status"] != "PROCESSING":
            raise ValueError("Selected upload must be PROCESSING before the fault")
        if not os.getenv("BENCHMARK_DATABASE_URL"):
            raise ValueError("BENCHMARK_DATABASE_URL required to verify durable recovery")
        before = database_timings([state["mediaId"]])
        if len(before["rows"]) != 1 or before["rows"][0]["status"] != "RUNNING":
            raise ValueError("Selected upload must have exactly one running MEDIA_PROCESS job")
        response = service.ecs.describe_tasks(cluster=service.cluster, tasks=[args.task_arn])
        if response.get("failures") or len(response["tasks"]) != 1:
            raise ValueError("Selected task is not found")
        task = response["tasks"][0]
        if task.get("group") != "service:" + service.service.split("/")[-1] or task.get("lastStatus") != "RUNNING":
            raise ValueError("Selected task must be a running member of the exact worker service")
        report.update(taskArn=task["taskArn"], mediaId=state["mediaId"], before=before,
                      originalDesiredCount=service.original_count)
        write_report(args.output, report)
        service.ecs.stop_task(cluster=service.cluster, task=task["taskArn"], reason="Photoplatform disposable worker recovery verification")
        report["faultIssuedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        started = time.monotonic()
        api.wait(upload, timeout=args.timeout)
        after = database_timings([state["mediaId"]])
        if len(after["rows"]) != 1 or after["rows"][0]["status"] != "DONE":
            raise AssertionError("Recovery did not yield exactly one DONE processing job")
        # ECS service should return to original desired count without our changing its configuration.
        until = time.monotonic() + args.timeout
        while service.state()["runningCount"] < service.original_count:
            if time.monotonic() > until:
                raise TimeoutError("Service did not restore its worker count")
            time.sleep(5)
        report.update(status="PASS", after=after, recoverySeconds=time.monotonic() - started,
                      recoveredService=service.state())
    except Exception as exc:
        report["fatalErrorType"] = type(exc).__name__
    finally:
        write_report(args.output, report)
    print(f"Single worker recovery: {report['status']}; report: {args.output}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
