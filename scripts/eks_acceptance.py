"""Real disposable EKS dev business acceptance, with explicit incomplete P6 matrix.

Exit 0 means only the selected compute-independent business suite passed. It does
not certify multi-Pod JWT/WebSocket, IAM isolation, fault recovery, node drain,
autoscaling, CLIP quality or rollback. Those require separately retained evidence.
"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.cloud_common import write_report
from scripts.cloud_acceptance import Acceptance
from scripts.eks_common import eks_guard


MATRIX = ("multi_api_jwt_and_ticket", "cross_pod_websocket_and_reconnect", "upload_private_access",
    "multi_worker_duplicate_delivery_and_delete_races", "rollout_and_node_drain", "sigkill_and_database_loss",
    "mq_db_outage_and_no_liveness_storm", "migration_failure_preserves_old_api", "iam_and_network_isolation",
    "backlog_autoscaling_and_stale_metric_handling", "ml_warmup_and_semantic_quality", "application_rollback")


def matrix_results(business_pass):
    return [{"scenario": name, "status": "PASS" if name == "upload_private_access" and business_pass else "NOT_RUN",
             "evidenceScope": "compute-independent AWS business cases" if name == "upload_private_access" and business_pass
             else "Requires separate scenario evidence; not inferred from Ready Pods or business suite"}
            for name in MATRIX]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="benchmarks/results/eks-acceptance.json")
    parser.add_argument("--max-expiry-wait", type=int, default=1200)
    args = parser.parse_args()
    if args.max_expiry_wait < 1:
        parser.error("max expiry wait must be positive")
    report = {"kind": "real-aws-eks-business-acceptance", "status": "FAIL", "businessStatus": "FAIL",
              "matrixStatus": "INCOMPLETE", "matrix": matrix_results(False), "cases": []}
    suite = None
    try:
        aws, control, report["provenance"] = eks_guard()
        write_report(args.output, report)
        suite = Acceptance(aws, report, args.output, args.max_expiry_wait)
        for name, test in (("private_bucket_configuration", suite.bucket_controls),
                ("actual_browser_s3_cors_preflight", suite.browser_preflight),
                ("wrong_checksum_rejected", suite.checksum),
                ("wrong_signed_mime_rejected", suite.mime),
                ("invalid_mime_and_oversize_declarations_rejected", suite.invalid_declarations),
                ("actual_payload_size_enforced", suite.actual_size),
                ("decoded_mime_mismatch_rejected", suite.wrong_image_bytes),
                ("duplicate_completion_idempotent", suite.idempotency),
                ("authorization_private_s3_and_cloudfront", suite.authorization_and_raw_storage),
                ("public_moderation_gate", suite.moderation),
                ("actual_expiry_and_bounded_deletion", suite.expiry_and_deletion)):
            suite.case(name, test)
        report["finalPods"] = {component: control.state(component) for component in ("api", "media-worker")}
        report["businessStatus"] = "PASS" if report["cases"] and all(row["status"] == "PASS" for row in report["cases"]) else "FAIL"
    except Exception as exc:
        # Arbitrary SDK/HTTP errors can contain URLs/tokens, so retain only their type.
        report["fatalErrorType"] = type(exc).__name__
    finally:
        if suite:
            report["cleanup"] = suite.cleanup()
            if any(row["cleanup"] == "failed" for row in report["cleanup"]["uploads"]):
                report["businessStatus"] = "FAIL"
        business_pass = report["businessStatus"] == "PASS"
        report["status"] = "INCOMPLETE" if business_pass else "FAIL"
        report["matrix"] = matrix_results(business_pass)
        write_report(args.output, report)
    print(f"EKS business acceptance: {report['businessStatus']}; full P6 matrix: INCOMPLETE; report: {args.output}")
    return 0 if report["businessStatus"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
