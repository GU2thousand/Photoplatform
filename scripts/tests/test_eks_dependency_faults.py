"""Dependency fault boundaries without real AWS, kubectl, network or credentials."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import eks_dependency_faults as faults


UID = "9780354f-3cd3-4f45-b056-50d6c7d4eb9c"
ENV = {"ALLOW_EKS_DEPENDENCY_FAULTS": "1", "EKS_NETWORK_POLICY_ENFORCEMENT_VERIFIED": "1"}
LABELS = {"app.kubernetes.io/name": "photoplatform", "app.kubernetes.io/instance": "photo",
          "app.kubernetes.io/component": "media-worker"}
SUMMARY = {"name": "worker-a", "uid": UID, "phase": "Running", "ready": True,
           "terminating": False, "imageIDs": ["containerd://sha256:" + "a" * 64], "restarts": 0}


def traverse(document, path):
    parts = [part.replace("~1", "/").replace("~0", "~") for part in path.split("/")[1:]]
    current = document
    for part in parts[:-1]:
        current = current[part]
    return current, parts[-1]


class Cluster:
    """Model server-side JSON Patch tests and immutable identities."""
    arn, namespace, namespace_uid = "arn:aws:eks:us-east-1:012345678901:cluster/dev", "photo-dev", "namespace-uid"

    def __init__(self):
        self.calls, self.policies = [], {}
        self.pod = {"metadata": {"name": "worker-a", "uid": UID, "resourceVersion": "42", "labels": copy.deepcopy(LABELS)},
                    "spec": {"containers": [{"name": "media-worker", "livenessProbe": {"periodSeconds": 10, "failureThreshold": 3}}]}}
        self.other = copy.deepcopy(self.pod)
        self.other["metadata"].update(name="worker-b", uid="another-pod")
        self.summary = copy.deepcopy(SUMMARY)
        for name, egress in (("deny", []), ("dns-and-aws", [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
                                                           "ports": [{"protocol": "TCP", "port": 443}]}]),
                             ("managed", [{"to": [{"ipBlock": {"cidr": "10.0.0.0/8"}}],
                                            "ports": [{"protocol": "TCP", "port": 5432}]}])):
            self.policies[name] = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                "metadata": {"name": name, "uid": name + "-uid", "resourceVersion": "5",
                             "annotations": {"do-not-serialize": "secret"}},
                "spec": {"podSelector": {"matchLabels": {"app.kubernetes.io/instance": "photo"}},
                         "policyTypes": ["Egress"], "egress": egress}}

    def pods(self, component):
        return [copy.deepcopy(self.summary)]

    def state(self, component):
        return {"pods": self.pods(component), "uid": "deployment-uid"}

    def json(self, *args):
        if args[:2] == ("get", "namespace"):
            return {"metadata": {"uid": self.namespace_uid}}
        if args[:2] == ("get", "pods"):
            return {"items": copy.deepcopy([self.pod, self.other])}
        if args[:2] == ("get", "pod"):
            return copy.deepcopy(self.pod)
        if args[:2] == ("get", "networkpolicies"):
            return {"items": copy.deepcopy(list(self.policies.values()))}
        if args[:2] == ("get", "networkpolicy"):
            return copy.deepcopy(self.policies[args[2]])
        raise AssertionError("Unexpected scoped read")

    def run(self, arn, namespace, *args, body=None):
        self.calls.append((arn, namespace, args, copy.deepcopy(body)))
        if (arn, namespace) != (self.arn, self.namespace):
            raise AssertionError("Unscoped mutation")
        if args[0] == "create":
            document = copy.deepcopy(body)
            if document["metadata"]["name"] in self.policies:
                raise ValueError("Already exists")
            document["metadata"].update(uid="created-policy-uid", resourceVersion="1")
            self.policies[document["metadata"]["name"]] = document
            return json.dumps(document)
        if args[0] == "patch":
            document = self.pod if args[1] == "pod" else self.policies[args[2]]
            updates = json.loads(args[args.index("-p") + 1])
            candidate = copy.deepcopy(document)
            for operation in updates:
                parent, key = traverse(candidate, operation["path"])
                if operation["op"] == "test":
                    if parent.get(key) != operation["value"]:
                        raise ValueError("Server precondition failed")
                elif operation["op"] in {"add", "replace"}:
                    parent[key] = copy.deepcopy(operation["value"])
                elif operation["op"] == "remove":
                    del parent[key]
                else:
                    raise AssertionError("Unexpected JSON Patch operation")
            candidate["metadata"]["resourceVersion"] = str(int(document["metadata"]["resourceVersion"]) + 1)
            document.clear()
            document.update(candidate)
            return json.dumps(document)
        if args[:2] == ("delete", "--raw"):
            name = args[2].rsplit("/", 1)[1]
            metadata = self.policies[name]["metadata"]
            if body["preconditions"] != {"uid": metadata["uid"], "resourceVersion": metadata["resourceVersion"]}:
                raise ValueError("Delete precondition failed")
            del self.policies[name]
            return "{}"
        raise AssertionError("Unexpected mutation")


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.cluster = Cluster()
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.mutations = patch.object(faults, "kubectl", side_effect=self.cluster.run)
        self.mutations.start()
        self.addCleanup(self.mutations.stop)

    def fault(self):
        return faults.IsolatedFault(self.cluster, "worker-a", UID, ["10.1.2.3", "2001:db8::1"])

    def test_explicit_fault_and_enforcement_switches_precede_all_writes(self):
        for variable in ENV:
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: "0"}):
                with self.assertRaises(ValueError):
                    self.fault()
        self.assertEqual(self.cluster.calls, [])

    def test_chart_additive_policies_exclude_only_exact_worker_and_restore(self):
        original = copy.deepcopy(self.cluster.policies)
        fault = self.fault()
        self.assertEqual(len(fault.originals), 2)  # Deny-only is harmless.
        self.assertNotIn("secret", json.dumps(fault.plan()))
        fault.inject()
        labels = self.cluster.pod["metadata"]["labels"]
        for name in ("dns-and-aws", "managed"):
            selector = self.cluster.policies[name]["spec"]["podSelector"]
            self.assertFalse(faults.selector_matches(selector, labels))
            self.assertTrue(faults.selector_matches(selector, self.cluster.other["metadata"]["labels"]))
        self.assertEqual(self.cluster.policies["deny"], original["deny"])
        rules = self.cluster.policies[fault.policy_name]["spec"]["egress"]
        self.assertEqual([rule["ports"][0]["port"] for rule in rules], [443, 5432])
        self.assertEqual([rule["to"][0]["ipBlock"]["cidr"] for rule in rules], ["0.0.0.0/0", "10.0.0.0/8"])
        self.assertTrue(all(rule["to"][0]["ipBlock"]["except"] == ["10.1.2.3/32"] for rule in rules))
        self.assertEqual(fault.restore()["status"], "PASS")
        for name in original:
            self.assertEqual(self.cluster.policies[name]["spec"], original[name]["spec"])
        self.assertNotIn(fault.policy_name, self.cluster.policies)
        self.assertEqual(self.cluster.pod["metadata"]["labels"], LABELS)
        self.assertEqual(self.cluster.other["metadata"]["labels"], LABELS)
        patches = [call for call in self.cluster.calls if call[2][0] == "patch"]
        for call in patches:
            operations = json.loads(call[2][call[2].index("-p") + 1])
            self.assertEqual([op["path"] for op in operations[:2]], ["/metadata/uid", "/metadata/resourceVersion"])
        deletion = next(call for call in self.cluster.calls if call[2][0] == "delete")
        self.assertEqual(deletion[3]["preconditions"]["uid"], "created-policy-uid")

    def test_changed_pod_uid_rejected_before_create_or_patch(self):
        self.cluster.summary["uid"] = "different-pod"
        with self.assertRaises(ValueError):
            self.fault()
        self.assertEqual(self.cluster.calls, [])

    def test_existing_fault_label_or_label_sensitive_policy_rejected(self):
        self.cluster.pod["metadata"]["labels"][faults.FAULT_LABEL] = "other-run"
        with self.assertRaises(ValueError):
            self.fault()
        self.cluster.pod["metadata"]["labels"].pop(faults.FAULT_LABEL)
        self.cluster.policies["managed"]["spec"]["podSelector"] = {"matchExpressions": [
            {"key": faults.FAULT_LABEL, "operator": "Exists"}]}
        with self.assertRaises(ValueError):
            self.fault()
        self.assertEqual(self.cluster.calls, [])

    def test_second_additive_allow_policy_during_fault_is_detected(self):
        fault = self.fault()
        fault.inject()
        self.cluster.policies["new-allow-all"] = {"metadata": {"name": "new-allow-all"},
            "spec": {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{}]}}
        with self.assertRaises(ValueError):
            fault.verify_no_additive_grant()
        self.assertEqual(fault.restore()["status"], "PASS")

    def test_combined_granting_policy_rejected_without_changing_ingress(self):
        self.cluster.policies["managed"]["spec"]["policyTypes"] = ["Ingress", "Egress"]
        self.cluster.policies["managed"]["spec"]["ingress"] = []
        with self.assertRaises(ValueError):
            self.fault()
        self.assertEqual(self.cluster.calls, [])

    def test_ingress_only_fault_label_selector_is_rejected_before_writes(self):
        self.cluster.policies["ingress"] = {"metadata": {"name": "ingress"}, "spec": {
            "policyTypes": ["Ingress"], "podSelector": {"matchExpressions": [
                {"key": faults.FAULT_LABEL, "operator": "DoesNotExist"}]}}}
        with self.assertRaises(ValueError):
            self.fault()
        self.assertEqual(self.cluster.calls, [])

    def test_response_lost_after_policy_write_still_restores(self):
        fault = self.fault()
        def lost_response(arn, namespace, *args, body=None):
            result = self.cluster.run(arn, namespace, *args, body=body)
            if args[:3] == ("patch", "networkpolicy", "managed"):
                raise RuntimeError("response lost")
            return result
        with patch.object(faults, "kubectl", side_effect=lost_response):
            with self.assertRaises(RuntimeError):
                fault.inject()
        self.assertEqual(fault.restore()["status"], "PASS")
        self.assertEqual(self.cluster.pod["metadata"]["labels"], LABELS)
        self.assertNotIn(fault.policy_name, self.cluster.policies)

    def test_response_lost_after_create_is_cleaned_by_run_ownership(self):
        fault = self.fault()
        def lost_create(arn, namespace, *args, body=None):
            result = self.cluster.run(arn, namespace, *args, body=body)
            if args[0] == "create":
                raise RuntimeError("response lost")
            return result
        with patch.object(faults, "kubectl", side_effect=lost_create):
            with self.assertRaises(RuntimeError):
                fault.inject()
        self.assertEqual(fault.restore()["status"], "PASS")
        self.assertNotIn(fault.policy_name, self.cluster.policies)

    def test_concurrent_policy_change_is_preserved_and_cleanup_reports_failure(self):
        fault = self.fault()
        fault.inject()
        self.cluster.policies["managed"]["spec"]["egress"].append({"ports": [{"port": 4444}]})
        changed = copy.deepcopy(self.cluster.policies["managed"]["spec"])
        restored = fault.restore()
        self.assertEqual(restored["status"], "FAIL")
        self.assertEqual(self.cluster.policies["managed"]["spec"], changed)
        self.assertEqual(self.cluster.pod["metadata"]["labels"], LABELS)

    def test_replaced_policy_and_pod_are_never_overwritten_during_restore(self):
        fault = self.fault()
        fault.inject()
        self.cluster.policies["managed"]["metadata"]["uid"] = "replaced-policy"
        self.cluster.pod["metadata"]["uid"] = "replaced-pod"
        result = fault.restore()
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(self.cluster.pod["metadata"]["uid"], "replaced-pod")
        self.assertEqual(self.cluster.policies["managed"]["metadata"]["uid"], "replaced-policy")

    def test_fault_policy_replacement_is_never_deleted(self):
        fault = self.fault()
        fault.inject()
        self.cluster.policies[fault.policy_name]["metadata"]["uid"] = "replacement"
        self.assertEqual(fault.restore()["status"], "FAIL")
        self.assertIn(fault.policy_name, self.cluster.policies)

    def test_fault_policy_drift_and_second_matching_pod_invalidate_active_fault(self):
        fault = self.fault()
        fault.inject()
        self.cluster.other["metadata"]["labels"][faults.FAULT_LABEL] = fault.token
        with self.assertRaises(ValueError):
            fault.verify_no_additive_grant()
        self.cluster.other["metadata"]["labels"].pop(faults.FAULT_LABEL)
        self.cluster.policies[fault.policy_name]["spec"]["egress"].append({})
        with self.assertRaises(ValueError):
            fault.verify_no_additive_grant()
        self.assertEqual(fault.restore()["status"], "FAIL")

    def test_name_reuse_and_restarts_invalidate_probe_evidence(self):
        baseline = copy.deepcopy(self.cluster.summary)
        self.cluster.summary["restarts"] += 1
        with self.assertRaises(AssertionError):
            faults.same_pod(self.cluster, "worker-a", UID, baseline)

    def test_endpoint_rotation_invalidates_fault_coverage(self):
        result = {"host": "db.example", "port": 5432, "connections": [{"address": "10.1.2.3", "connected": False}]}
        baseline = copy.deepcopy(result)
        result["connections"][0]["address"] = "10.1.2.4"
        with self.assertRaises(ValueError):
            faults.require_endpoint(result, baseline)

    def test_unrestricted_namespace_can_still_be_isolated_without_broad_policy_writes(self):
        self.cluster.policies = {}
        fault = self.fault()
        self.assertEqual(fault.originals, [])
        fault.inject()
        self.assertEqual(fault.restore()["status"], "PASS")
        self.assertEqual(len([call for call in self.cluster.calls if call[2][:2] == ("patch", "networkpolicy")]), 0)

    def test_original_union_ports_dns_selectors_and_exclusions_are_preserved(self):
        rules = [{"to": [{"namespaceSelector": {"matchLabels": {"name": "kube-system"}},
                           "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                  "ports": [{"protocol": "UDP", "port": 53}]},
                 {"to": [{"ipBlock": {"cidr": "10.0.0.0/8", "except": ["10.2.0.0/16"]}}],
                  "ports": [{"protocol": "TCP", "port": 5432}]},
                 {"ports": [{"protocol": "TCP", "port": 443}]}]
        modified = faults.exclude_addresses(rules, [faults.ipaddress.ip_address("10.1.2.3")])
        self.assertEqual(modified[0], rules[0])
        self.assertEqual(modified[1]["to"][0]["ipBlock"]["except"], ["10.2.0.0/16", "10.1.2.3/32"])
        self.assertEqual(modified[2]["ports"], rules[2]["ports"])
        self.assertEqual(len(modified[2]["to"]), 2)
        self.assertTrue(all(rule["ports"] == rules[i]["ports"] for i, rule in enumerate(modified)))

    def test_single_ip_allow_is_removed_without_an_empty_to_allowall_rule(self):
        rules = [{"to": [{"ipBlock": {"cidr": "10.1.2.3/32"}}], "ports": [{"port": 5432}]}]
        self.assertEqual(faults.exclude_addresses(rules, [faults.ipaddress.ip_address("10.1.2.3")]), [])

    def test_generated_deny_only_policy_has_no_empty_egress_field_and_restores(self):
        self.cluster.policies = {"managed": self.cluster.policies["managed"]}
        self.cluster.policies["managed"]["spec"]["egress"][0]["to"][0]["ipBlock"]["cidr"] = "10.1.2.3/32"
        fault = self.fault()
        self.assertNotIn("egress", fault.fault_manifest["spec"])
        fault.inject()
        self.assertEqual(fault.restore()["status"], "PASS")

    def test_partitioned_policy_spec_change_invalidates_active_fault(self):
        fault = self.fault()
        fault.inject()
        self.cluster.policies["managed"]["spec"]["egress"].append({"ports": [{"port": 4444}]})
        with self.assertRaises(ValueError):
            fault.verify_no_additive_grant()
        self.assertEqual(fault.restore()["status"], "FAIL")

    def test_missing_dedicated_policy_cannot_claim_dependency_specific_fault(self):
        fault = self.fault()
        fault.inject()
        del self.cluster.policies[fault.policy_name]
        with self.assertRaises(ValueError):
            fault.verify_no_additive_grant()
        self.assertEqual(fault.restore()["status"], "PASS")

    def test_unknown_policy_peer_fields_fail_closed(self):
        with self.assertRaises(ValueError):
            faults.exclude_addresses([{"to": [{"mystery": "all"}]}], [faults.ipaddress.ip_address("10.1.2.3")])


class EvidenceTests(unittest.TestCase):
    def run_scenario(self, *, denied=True, kube_ready=False, alive=True, job_status="DONE", restore_status="PASS",
                     interrupted=False, final_restart=False):
        cluster = Cluster()
        baseline = {"host": "db.example", "port": 5432, "connections": [{"address": "10.1.2.3", "connected": True}],
            "readiness": True, "liveness": True, "databaseReady": True, "brokerHeartbeatFresh": True, "liveMaximumAgeSeconds": 180}
        outage = copy.deepcopy(baseline)
        outage.update(readiness=False, liveness=alive, databaseReady=False)
        outage["connections"][0]["connected"] = not denied
        second_outage = copy.deepcopy(outage)
        if interrupted:
            second_outage["connections"][0]["connected"] = True
        fault = Mock()
        fault.restore.return_value = {"status": restore_status}
        fault.plan.return_value = {"restoration": "saved before mutation"}
        fault.inject.return_value = {"faultPolicyUid": "created"}
        api = Mock()
        api.state.return_value = {"status": "PROCESSING", "mediaId": 12}
        reports = []
        paths = []
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-eks-unit-")
        self.addCleanup(temporary.cleanup)
        output = str(Path(temporary.name) / "synthetic-eks-dependency.json")
        def save_synthetic(path, report):
            paths.append(path)
            captured = copy.deepcopy(report)
            captured["executionScope"] = "synthetic_unit_test"
            reports.append(captured)
        clocks = iter([0, 1, 241] + [242] * 20)
        final = AssertionError("Worker restarted during durable recovery") if final_restart else SUMMARY
        argv = ["eks_dependency_faults.py", "--dependency", "database", "--pod-name", "worker-a", "--pod-uid", UID,
                "--upload-id", "upload", "--job-id", "job", "--output", output]
        with patch.dict(os.environ, ENV, clear=True), patch.object(faults, "eks_guard", return_value=(Mock(), cluster, {})), \
                patch.object(faults, "IsolatedFault", return_value=fault), patch.object(faults, "probe", side_effect=[baseline, outage, second_outage, baseline]), \
                patch.object(faults, "same_pod", side_effect=[SUMMARY, SUMMARY | {"ready": kube_ready}, SUMMARY | {"ready": kube_ready}, SUMMARY, final]), \
                patch.object(faults, "CloudAPI", return_value=api), patch.object(faults, "running_job", return_value={"status": "RUNNING"}), \
                patch.object(faults, "database_timings", return_value={"rows": [{"status": job_status}]}), \
                patch.object(faults, "queue_snapshot", return_value={"consumers": 1}), \
                patch.object(faults, "write_report", side_effect=save_synthetic), \
                patch.object(faults.time, "monotonic", side_effect=lambda: next(clocks)), patch.object(faults.time, "sleep"), patch("sys.argv", argv):
            code = faults.main()
        self.assertEqual(set(paths), {output})
        self.assertFalse(Path(output).exists(), "Mock tests must not emit a real cloud evidence file")
        return code, reports, fault

    def test_pass_requires_denial_readiness_no_restart_and_durable_recovery(self):
        code, reports, fault = self.run_scenario()
        self.assertEqual(code, 0)
        self.assertEqual(reports[-1]["status"], "PASS")
        self.assertEqual(reports[-1]["executionScope"], "synthetic_unit_test")
        self.assertEqual(reports[-1]["matrixStatus"], "INCOMPLETE")
        self.assertIn("restorePlan", reports[0])
        self.assertGreaterEqual(reports[-1]["outageHeldSeconds"], 240)
        fault.restore.assert_called_once()

    def test_connected_tcp_ready_pod_liveness_failure_or_unfinished_job_cannot_pass(self):
        for overrides in ({"denied": False}, {"kube_ready": True}, {"alive": False},
                          {"job_status": "RUNNING"}, {"restore_status": "FAIL"}, {"interrupted": True}, {"final_restart": True}):
            with self.subTest(overrides=overrides):
                code, reports, fault = self.run_scenario(**overrides)
                self.assertEqual(code, 1)
                self.assertEqual(reports[-1]["status"], "FAIL")
                self.assertGreaterEqual(fault.restore.call_count, 1)


if __name__ == "__main__":
    unittest.main()
