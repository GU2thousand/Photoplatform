"""Temporarily isolate one verified disposable EKS worker from its DB or broker.

NetworkPolicies are additive: every granting egress policy selecting the worker is
temporarily made to exclude its unique run label. Other Pods retain their original
selection. A dedicated policy then permits egress except the dependency's resolved
addresses. Original safe policy specs and a restoration plan are saved before writes.
No AWS security group, shared dependency, Deployment or process is changed/deleted.

PASS is only this observed worker outage/recovery, not the complete P6 matrix, a
SIGKILL/fencing test or proof that NetworkPolicy works on every cluster path.
"""
import argparse
import copy
import ipaddress
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.cloud_common import CloudAPI, database_timings, write_report
from scripts.eks_common import eks_guard, kubectl, queue_snapshot, safe_name, worker_python
from scripts.eks_failure import running_job


FAULT_LABEL = "photoplatform.io/fault-run"

# Executed only inside the selected worker. Never print DSNs, usernames, passwords,
# claim tokens, exception messages, complete environment or mounted file contents.
PROBE = r'''
import ipaddress, json, os, socket, sys
from app.config import load_secret_files, rabbit_parameters
from app.health import database_ready, healthy, live
load_secret_files()
dependency = json.loads(sys.argv[2])["dependency"]
if dependency == "database":
    from psycopg.conninfo import conninfo_to_dict
    dsn = conninfo_to_dict(os.getenv("DATABASE_URL", ""))
    host = dsn.get("host", os.getenv("DATABASE_HOST", ""))
    port = int(dsn.get("port", os.getenv("DATABASE_PORT", "5432")))
else:
    params = rabbit_parameters()
    host, port = params.host, params.port
if not host or "," in host or any(c.isspace() for c in host) or not 1 <= port <= 65535:
    raise ValueError("One dependency TCP endpoint is required")
addresses = sorted({str(ipaddress.ip_address(row[4][0])) for row in
    socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
if not addresses or len(addresses) > 8:
    raise ValueError("Bounded dependency resolution required")
connections = []
for address in addresses:
    try:
        with socket.create_connection((address, port), timeout=2):
            connected = True
    except OSError:
        connected = False
    connections.append({"address": address, "connected": connected})
db = database_ready()
broker = healthy(lambda: True)
print(json.dumps({"host": host, "port": port, "connections": connections,
    "databaseReady": db, "brokerHeartbeatFresh": broker, "readiness": db and broker,
    "liveness": live(), "liveMaximumAgeSeconds": float(os.getenv("WORKER_LIVE_MAX_AGE_SECONDS", "180"))}))
'''


def explicit_guard():
    if os.getenv("ALLOW_EKS_DEPENDENCY_FAULTS") != "1":
        raise ValueError("Set ALLOW_EKS_DEPENDENCY_FAULTS=1")
    if os.getenv("EKS_NETWORK_POLICY_ENFORCEMENT_VERIFIED") != "1":
        raise ValueError("Operator must verify the cluster's NetworkPolicy enforcement first")


def selector_matches(selector, labels):
    """Implement Kubernetes LabelSelector semantics; unknown shapes fail closed."""
    if set(selector) - {"matchLabels", "matchExpressions"}:
        raise ValueError("Unsupported NetworkPolicy selector")
    if any(labels.get(key) != value for key, value in selector.get("matchLabels", {}).items()):
        return False
    for expression in selector.get("matchExpressions", []):
        if set(expression) - {"key", "operator", "values"}:
            raise ValueError("Unsupported selector expression")
        key, operator, values = expression["key"], expression["operator"], expression.get("values", [])
        if operator == "In":
            matched = key in labels and labels[key] in values
        elif operator == "NotIn":
            matched = key not in labels or labels[key] not in values
        elif operator == "Exists":
            matched = key in labels
        elif operator == "DoesNotExist":
            matched = key not in labels
        else:
            raise ValueError("Unsupported selector operator")
        if not matched:
            return False
    return True


def egress_policy(policy):
    spec = policy["spec"]
    # Kubernetes defaults policyTypes to Ingress plus Egress when egress exists.
    return "Egress" in spec.get("policyTypes", ["Ingress"] + (["Egress"] if "egress" in spec else []))


def ingress_policy(policy):
    return "Ingress" in policy["spec"].get("policyTypes", ["Ingress"])


def exclude_addresses(rules, addresses):
    """Keep the original union of peers/ports; subtract only dependency IPs.

    Named peers retain their original selectors. Actual in-Pod denied TCP probes
    must establish that selectors/NAT do not provide another route to the target.
    An omitted/empty `to` means an existing all-destination grant; replacing it
    with v4/v6 ipBlocks preserves its original port limits without widening them.
    """
    result = []
    for rule in rules:
        if set(rule) - {"ports", "to"}:
            raise ValueError("Unsupported egress rule fields")
        peers = rule.get("to") or [{"ipBlock": {"cidr": "0.0.0.0/0"}}, {"ipBlock": {"cidr": "::/0"}}]
        retained = []
        for peer in peers:
            if "ipBlock" not in peer:
                if set(peer) - {"podSelector", "namespaceSelector"}:
                    raise ValueError("Unsupported NetworkPolicy peer")
                if peer:
                    retained.append(copy.deepcopy(peer))
                    continue
                # Empty peer grants every destination, so preserve this grant's
                # family union while excluding the target addresses.
                retained.extend(exclude_addresses([{"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}},
                                                             {"ipBlock": {"cidr": "::/0"}}]}], addresses)[0]["to"])
                continue
            if set(peer) != {"ipBlock"} or set(peer["ipBlock"]) - {"cidr", "except"}:
                raise ValueError("Unsupported ipBlock peer")
            block = peer["ipBlock"]
            network = ipaddress.ip_network(block["cidr"], strict=True)
            exclusions = [ipaddress.ip_network(value, strict=True) for value in block.get("except", [])]
            if any(ex.version != network.version or not ex.subnet_of(network) for ex in exclusions):
                raise ValueError("Invalid original ipBlock exclusions")
            for address in addresses:
                if address.version != network.version or address not in network or any(address in ex for ex in exclusions):
                    continue
                exclusions.append(ipaddress.ip_network(f"{address}/{address.max_prefixlen}"))
            if network in exclusions:
                continue  # Do not emit to=[]: that would become allow-all.
            retained.append({"ipBlock": {"cidr": block["cidr"], **(
                {"except": [str(exclusion) for exclusion in exclusions]} if exclusions else {})}})
        if retained:
            rewritten = copy.deepcopy(rule)
            rewritten["to"] = retained
            result.append(rewritten)
    return result


def safe_policy(policy):
    """NetworkPolicy spec has no credentials; arbitrary annotations are excluded."""
    metadata = policy["metadata"]
    return {"name": metadata["name"], "uid": metadata["uid"],
            "resourceVersion": metadata["resourceVersion"], "spec": copy.deepcopy(policy["spec"])}


def same_pod(control, name, uid, baseline=None):
    safe_name(name)
    uuid.UUID(uid)
    selected = [pod for pod in control.pods("media-worker") if pod["name"] == name and pod["uid"] == uid]
    if len(selected) != 1 or selected[0]["phase"] != "Running" or selected[0]["terminating"]:
        raise ValueError("Fault target must remain the exact running verified worker UID")
    pod = selected[0]
    if baseline and (pod["imageIDs"] != baseline["imageIDs"] or pod["restarts"] != baseline["restarts"]):
        raise AssertionError("Selected worker restarted or changed its immutable image")
    return pod


def probe(control, name, uid, dependency, baseline):
    same_pod(control, name, uid, baseline)
    raw = worker_python(control, name, uid, PROBE, {"dependency": dependency})
    result = json.loads(raw)
    same_pod(control, name, uid, baseline)
    if (not result["connections"] or len(result["connections"]) > 8 or
            type(result["port"]) is not int or not 1 <= result["port"] <= 65535):
        raise ValueError("Invalid measured dependency probe")
    for row in result["connections"]:
        ipaddress.ip_address(row["address"])
        if type(row["connected"]) is not bool:
            raise ValueError("Probe must return an actual TCP result")
    return result


class IsolatedFault:
    """All changes use API-server UID/resourceVersion/spec test preconditions."""
    def __init__(self, control, name, uid, addresses):
        explicit_guard()
        self.control, self.name, self.uid = control, safe_name(name), str(uuid.UUID(uid))
        self.baseline = same_pod(control, name, uid)
        self.token = uuid.uuid4().hex
        self.policy_name = "photoplatform-fault-" + self.token
        self.originals, self.attempted = [], []
        self.label_attempted = self.created_attempted = False
        self.created_uid = None
        namespace = control.json("get", "namespace", control.namespace, "-o", "json")
        if namespace["metadata"]["uid"] != control.namespace_uid:
            raise ValueError("Guarded namespace was replaced")
        all_pods = control.json("get", "pods", "-o", "json")["items"]
        matches = [p for p in all_pods if p["metadata"]["name"] == name and p["metadata"]["uid"] == uid]
        if len(matches) != 1 or FAULT_LABEL in matches[0]["metadata"].get("labels", {}):
            raise ValueError("Target must have no existing fault label")
        if any(p["metadata"].get("labels", {}).get(FAULT_LABEL) == self.token for p in all_pods):
            raise ValueError("Fault label must uniquely identify the exact Pod")
        self.pod_labels = copy.deepcopy(matches[0]["metadata"]["labels"])
        policies = control.json("get", "networkpolicies", "-o", "json")["items"]
        for policy in policies:
            if policy["metadata"]["name"] == self.policy_name or policy["metadata"].get("deletionTimestamp"):
                raise ValueError("NetworkPolicy set must be stable and fault policy absent")
            selector = policy["spec"].get("podSelector", {})
            # Adding the label must not unexpectedly select a previously unselected
            # policy (Exists/In on a label owned by another test).
            augmented = self.pod_labels | {FAULT_LABEL: self.token}
            before, after = selector_matches(selector, self.pod_labels), selector_matches(selector, augmented)
            if before != after:
                raise ValueError("Fault label changes existing ingress/egress selection")
            if before and egress_policy(policy) and policy["spec"].get("egress"):
                if ingress_policy(policy):
                    raise ValueError("Combined ingress/egress granting policy cannot safely be partitioned")
                original = safe_policy(policy)
                expected = copy.deepcopy(original["spec"])
                expected.setdefault("podSelector", {}).setdefault("matchExpressions", []).append(
                    {"key": FAULT_LABEL, "operator": "NotIn", "values": [self.token]})
                original["faultSpec"] = expected
                self.originals.append(original)
        ips = sorted({ipaddress.ip_address(address) for address in addresses}, key=lambda a: (a.version, int(a)))
        if not ips or len(ips) > 8 or any(address.is_unspecified or address.is_loopback for address in ips):
            raise ValueError("A bounded real remote dependency address set is required")
        existing_rules = [rule for original in self.originals for rule in original["spec"]["egress"]]
        if not any(egress_policy(policy) and selector_matches(policy["spec"].get("podSelector", {}), self.pod_labels)
                   for policy in policies):
            existing_rules = [{}]  # Already unrestricted egress, no new grants.
        fault_rules = exclude_addresses(existing_rules, ips)
        self.fault_manifest = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": self.policy_name, "namespace": control.namespace,
                         "labels": {FAULT_LABEL: self.token}},
            "spec": {"podSelector": {"matchLabels": {FAULT_LABEL: self.token}},
                     "policyTypes": ["Egress"], **({"egress": fault_rules} if fault_rules else {})}}

    def plan(self):
        return {"podName": self.name, "podUid": self.uid, "podOriginalLabels": self.pod_labels,
                "namespaceUid": self.control.namespace_uid, "faultLabel": FAULT_LABEL, "faultToken": self.token,
                "originalPolicies": self.originals, "faultPolicy": self.fault_manifest,
                "restoration": "Restore exact policies by UID and expected spec, delete exact fault policy UID, remove exact Pod label",
                "egressScope": "Original egress peer/port union minus dependency IPs for one Pod; other Pods retain all policies"}

    def patch(self, kind, current, operations):
        self.verify_namespace()
        metadata = current["metadata"]
        body = [{"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
                {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]}, *operations]
        kubectl(self.control.arn, self.control.namespace, "patch", kind, metadata["name"],
                "--type=json", "-p", json.dumps(body), "-o", "json")

    def verify_namespace(self):
        current = self.control.json("get", "namespace", self.control.namespace, "-o", "json")
        if (current["metadata"]["uid"] != self.control.namespace_uid or
                current["metadata"].get("deletionTimestamp")):
            raise ValueError("Guarded namespace changed or is terminating")

    def policy(self, original, expected):
        current = self.control.json("get", "networkpolicy", original["name"], "-o", "json")
        if current["metadata"]["uid"] != original["uid"] or current["spec"] != expected:
            raise ValueError("NetworkPolicy was replaced or concurrently edited; refuse overwrite")
        return current

    def verify_no_additive_grant(self):
        self.verify_namespace()
        pod = self.control.json("get", "pod", self.name, "-o", "json")
        if pod["metadata"]["uid"] != self.uid or pod["metadata"]["labels"].get(FAULT_LABEL) != self.token:
            raise ValueError("Target Pod or fault label changed")
        labeled = [p for p in self.control.json("get", "pods", "-o", "json")["items"]
                   if p["metadata"].get("labels", {}).get(FAULT_LABEL) == self.token]
        if len(labeled) != 1 or labeled[0]["metadata"]["uid"] != self.uid:
            raise ValueError("Fault policy must continue selecting only the exact target Pod")
        policies = self.control.json("get", "networkpolicies", "-o", "json")["items"]
        exact = [policy for policy in policies if policy["metadata"]["name"] == self.policy_name]
        if (len(exact) != 1 or exact[0]["metadata"]["uid"] != self.created_uid or
                exact[0]["spec"] != self.fault_manifest["spec"] or
                exact[0]["metadata"].get("labels", {}).get(FAULT_LABEL) != self.token):
            raise ValueError("Exact fault policy identity or restrictions changed")
        for original in self.originals:
            matching = [policy for policy in policies if policy["metadata"]["name"] == original["name"]]
            if (len(matching) != 1 or matching[0]["metadata"]["uid"] != original["uid"] or
                    matching[0]["spec"] != original["faultSpec"]):
                raise ValueError("Partitioned original policy was replaced or concurrently edited")
        for policy in policies:
            if (policy["metadata"]["name"] != self.policy_name and egress_policy(policy) and
                    policy["spec"].get("egress") and selector_matches(policy["spec"].get("podSelector", {}), pod["metadata"]["labels"])):
                raise ValueError("Additional granting egress policy defeats dependency isolation")
        return pod

    def inject(self):
        explicit_guard()
        self.verify_namespace()
        same_pod(self.control, self.name, self.uid, self.baseline)
        self.created_attempted = True
        created = json.loads(kubectl(self.control.arn, self.control.namespace, "create", "-f", "-", "-o", "json", body=self.fault_manifest))
        self.created_uid = created["metadata"]["uid"]
        pod = self.control.json("get", "pod", self.name, "-o", "json")
        if pod["metadata"]["uid"] != self.uid or pod["metadata"].get("labels", {}) != self.pod_labels:
            raise ValueError("Pod labels or UID changed before injection")
        self.label_attempted = True
        self.patch("pod", pod, [{"op": "test", "path": "/metadata/labels", "value": self.pod_labels},
            {"op": "add", "path": "/metadata/labels/photoplatform.io~1fault-run", "value": self.token}])
        for original in self.originals:
            current = self.policy(original, original["spec"])
            self.attempted.append(original)
            self.patch("networkpolicy", current, [{"op": "test", "path": "/spec", "value": original["spec"]},
                {"op": "replace", "path": "/spec/podSelector", "value": original["faultSpec"]["podSelector"]}])
        self.verify_no_additive_grant()
        return {"faultPolicyUid": self.created_uid, "faultToken": self.token}

    def restore(self):
        failures = []
        for original in reversed(self.attempted):
            try:
                current = self.control.json("get", "networkpolicy", original["name"], "-o", "json")
                if current["metadata"]["uid"] != original["uid"]:
                    raise ValueError("Policy UID changed")
                if current["spec"] == original["spec"]:
                    continue  # Mutation response may have failed before the write.
                self.policy(original, original["faultSpec"])
                self.patch("networkpolicy", current, [{"op": "test", "path": "/spec", "value": original["faultSpec"]},
                    {"op": "replace", "path": "/spec/podSelector", "value": original["spec"].get("podSelector", {})}])
            except Exception as error:
                failures.append({"resource": original["name"], "errorType": type(error).__name__})
        # Removing the run label re-selects every untouched original policy. This
        # also recovers the target when a concurrent policy edit prevents restore.
        if self.label_attempted:
            try:
                pod = self.control.json("get", "pod", self.name, "-o", "json")
                if pod["metadata"]["uid"] != self.uid:
                    raise ValueError("Pod replaced; refuse changing another UID")
                value = pod["metadata"].get("labels", {}).get(FAULT_LABEL)
                if value is not None:
                    if value != self.token:
                        raise ValueError("Fault label changed concurrently")
                    self.patch("pod", pod, [{"op": "test", "path": "/metadata/labels/photoplatform.io~1fault-run", "value": self.token},
                        {"op": "remove", "path": "/metadata/labels/photoplatform.io~1fault-run"}])
            except Exception as error:
                failures.append({"resource": self.name, "errorType": type(error).__name__})
        if self.created_attempted:
            try:
                policies = self.control.json("get", "networkpolicies", "-o", "json")["items"]
                matching = [p for p in policies if p["metadata"]["name"] == self.policy_name]
                if matching:
                    current = matching[0]
                    if (current["metadata"].get("labels", {}).get(FAULT_LABEL) != self.token or
                            (self.created_uid and current["metadata"]["uid"] != self.created_uid) or
                            current["spec"] != self.fault_manifest["spec"]):
                        raise ValueError("Fault policy ownership or spec changed")
                    metadata = current["metadata"]
                    body = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {
                        "uid": metadata["uid"], "resourceVersion": metadata["resourceVersion"]}}
                    self.verify_namespace()
                    kubectl(self.control.arn, self.control.namespace, "delete", "--raw",
                        f"/apis/networking.k8s.io/v1/namespaces/{self.control.namespace}/networkpolicies/{self.policy_name}",
                        "-f", "-", body=body)
            except Exception as error:
                failures.append({"resource": self.policy_name, "errorType": type(error).__name__})
        return {"status": "FAIL" if failures else "PASS", "failures": failures}


def require_endpoint(probe_result, baseline):
    if (probe_result["host"] != baseline["host"] or probe_result["port"] != baseline["port"] or
            {row["address"] for row in probe_result["connections"]} != {row["address"] for row in baseline["connections"]}):
        raise ValueError("Dependency endpoint changed; fault does not cover the current address set")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dependency", choices=("database", "mq"), required=True)
    parser.add_argument("--pod-name", required=True)
    parser.add_argument("--pod-uid", required=True)
    parser.add_argument("--upload-id", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--fault-seconds", type=int, default=240)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--output", default="benchmarks/results/eks-dependency-fault.json")
    args = parser.parse_args()
    if not 210 <= args.fault_seconds <= 900 or not 1 <= args.timeout <= 3600:
        parser.error("fault-seconds must be 210..900 and timeout 1..3600")
    report = {"kind": "real-aws-eks-worker-dependency-isolation", "dependency": args.dependency,
        "status": "FAIL", "matrixStatus": "INCOMPLETE", "outageSamples": [],
        "limitations": ["Specific worker/address/observation window only; operator enforcement flag is not measurement",
            "TCP probes test new connections; established sessions may survive NetworkPolicy changes",
            "Current mounted configuration endpoints are checked; process startup secret snapshot is not independently read",
            "Queue management gauges are sampled and do not identify individual consumer connections",
            "No SIGKILL, after-S3 crash, claim fencing, global outage or full P6 matrix is certified"]}
    fault = None
    try:
        explicit_guard()
        _, control, report["provenance"] = eks_guard()
        baseline = same_pod(control, args.pod_name, args.pod_uid)
        report["before"] = control.state("media-worker")
        first = probe(control, args.pod_name, args.pod_uid, args.dependency, baseline)
        report["dependencyBefore"] = first
        if not first["readiness"] or not first["liveness"] or not all(row["connected"] for row in first["connections"]):
            raise ValueError("Healthy dependency TCP/readiness/liveness baseline required")
        pod = control.json("get", "pod", args.pod_name, "-o", "json")
        container = next(c for c in pod["spec"]["containers"] if c["name"] == "media-worker")
        live_probe = container.get("livenessProbe", {})
        minimum_window = first["liveMaximumAgeSeconds"] + live_probe.get("periodSeconds", 10) * live_probe.get("failureThreshold", 3)
        if not math.isfinite(minimum_window) or minimum_window <= 0 or args.fault_seconds < minimum_window:
            raise ValueError("Fault window must exceed configured liveness age plus failure budget")
        api = CloudAPI()
        upload = {"uploadId": args.upload_id}
        initial = api.state(upload)
        upload["mediaId"] = initial["mediaId"]
        if initial["status"] != "PROCESSING":
            raise ValueError("Selected upload must be PROCESSING")
        report["jobBefore"] = running_job(initial["mediaId"], args.job_id, args.pod_name)
        report["queueBefore"] = queue_snapshot()
        if report["queueBefore"]["consumers"] < 1:
            raise ValueError("A real broker consumer baseline is required")
        fault = IsolatedFault(control, args.pod_name, args.pod_uid, [r["address"] for r in first["connections"]])
        report["restorePlan"] = fault.plan()
        write_report(args.output, report)
        report["fault"] = fault.inject()
        started = time.monotonic()
        held_started = None
        while True:
            fault.verify_no_additive_grant()
            measured = probe(control, args.pod_name, args.pod_uid, args.dependency, baseline)
            require_endpoint(measured, first)
            current = same_pod(control, args.pod_name, args.pod_uid, baseline)
            denied = all(not row["connected"] for row in measured["connections"])
            affected = not measured["databaseReady"] if args.dependency == "database" else (
                not measured["brokerHeartbeatFresh"] and measured["databaseReady"])
            if denied and "jobDuringOutage" not in report:
                report["jobDuringOutage"] = running_job(initial["mediaId"], args.job_id, args.pod_name)
            observed = time.monotonic()
            qualifying = denied and affected and not measured["readiness"] and not current["ready"]
            if held_started is None and qualifying:
                held_started = observed
            elif held_started is not None and not qualifying:
                raise AssertionError("Dependency denial/readiness downgrade was not sustained")
            held_seconds = observed - held_started if held_started is not None else 0
            report["outageSamples"].append({"elapsedSeconds": observed - started, "heldFaultSeconds": held_seconds,
                "probe": measured, "pod": current, "allTargetTcpDenied": denied, "dependencyAffected": affected})
            write_report(args.output, report)
            if not measured["liveness"]:
                raise AssertionError("External dependency fault failed independent liveness")
            if held_started is not None and held_seconds >= args.fault_seconds:
                break
            if held_started is None and observed - started >= args.timeout:
                raise TimeoutError("Actual dependency outage and Kubernetes readiness downgrade not observed")
            time.sleep(5)
        if "jobDuringOutage" not in report:
            raise AssertionError("Measured TCP denial and actual worker readiness downgrade are required")
        report["outageHeldSeconds"] = held_seconds
        report["queueDuring"] = queue_snapshot()
        report["restoration"] = fault.restore()
        if report["restoration"]["status"] != "PASS":
            raise AssertionError("Fault restoration did not complete")
        fault = None
        deadline = time.monotonic() + args.timeout
        while True:
            recovered = probe(control, args.pod_name, args.pod_uid, args.dependency, baseline)
            require_endpoint(recovered, first)
            current = same_pod(control, args.pod_name, args.pod_uid, baseline)
            if recovered["readiness"] and recovered["liveness"] and current["ready"] and all(r["connected"] for r in recovered["connections"]):
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("Same worker did not recover dependency connectivity and readiness")
            time.sleep(5)
        api.wait(upload, timeout=args.timeout)
        report["jobAfter"] = database_timings([initial["mediaId"]])
        if len(report["jobAfter"]["rows"]) != 1 or report["jobAfter"]["rows"][0]["status"] != "DONE":
            raise AssertionError("Exactly one durable media job must recover to DONE")
        report["queueAfter"] = queue_snapshot()
        if report["queueAfter"]["consumers"] < report["queueBefore"]["consumers"]:
            raise AssertionError("Broker consumer gauge did not recover to baseline")
        final_pod = same_pod(control, args.pod_name, args.pod_uid, baseline)
        if not final_pod["ready"]:
            raise AssertionError("Same verified worker must remain Ready through durable job recovery")
        report.update(status="PASS", dependencyAfter=recovered, after=control.state("media-worker"),
                      recoveryObservedSeconds=time.monotonic() - started)
    except Exception as error:
        report["fatalErrorType"] = type(error).__name__
    finally:
        if fault:
            report["restoration"] = fault.restore()
            if report["restoration"]["status"] != "PASS":
                report["status"] = "FAIL"
        write_report(args.output, report)
    print(f"EKS {args.dependency} worker isolation: {report['status']}; P6 matrix INCOMPLETE; report: {args.output}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
