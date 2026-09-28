"""Exact-node mutation boundary tests; no AWS, kubeconfig, credentials or network."""
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from scripts import eks_node_drain as drain


NODE_UID = "6ff31eeb-8956-44ba-8af6-d13b315a01d2"
API_UID = "d1f4e7ae-b66b-4b45-9d43-f2c0d661bf68"
WORKER_UID = "9780354f-3cd3-4f45-b056-50d6c7d4eb9c"
ACCOUNT, REGION = "012345678901", "us-east-1"
ARN = f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/dev"
ENV = {"ALLOW_EKS_NODE_DRAIN": "1", "EKS_RELEASE_SCENARIO_LOCK_ID": "exclusive-dev-2026-09-27",
       "AWS_REGION": REGION, "EXPECTED_AWS_ACCOUNT_ID": ACCOUNT}
TAGS = {"Project": "photoplatform", "Environment": "dev", "DisposableEnvironment": "true"}


def traverse(document, path):
    parts = [p.replace("~1", "/").replace("~0", "~") for p in path.split("/")[1:]]
    current = document
    for part in parts[:-1]:
        current = current[part]
    return current, parts[-1]


class Cluster:
    arn, cluster, namespace, namespace_uid, release, sha = ARN, "dev", "photo-dev", "namespace-uid", "photo", "a" * 40
    manifest = {"images": {"api": "api@sha256:" + "b" * 64, "worker": "worker@sha256:" + "c" * 64}}

    def __init__(self):
        self.calls, self.reads, self.hpas = [], [], []
        self.node_record = {"metadata": {"name": "node-a", "uid": NODE_UID, "resourceVersion": "7", "labels": {
            "photoplatform.io/drain-allowed": "true", "eks.amazonaws.com/nodegroup": "dev-application",
            "topology.kubernetes.io/region": REGION, "topology.kubernetes.io/zone": REGION + "a"}},
            "spec": {"providerID": "aws:///us-east-1a/i-0123456789abcdef0"},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
        self.ns = {"metadata": {"uid": self.namespace_uid, "labels": {"app.kubernetes.io/part-of": "photoplatform",
            "photoplatform.io/environment": "dev", "photoplatform.io/disposable": "true"}}}
        self.deployments, self.pod_records = {}, {}
        for component, uid in (("api", API_UID), ("media-worker", WORKER_UID)):
            name = "photo-" + component
            labels = {"app.kubernetes.io/name": "photoplatform", "app.kubernetes.io/instance": self.release,
                      "app.kubernetes.io/component": component}
            self.deployments[component] = {"metadata": {"name": name, "uid": "dep-" + component},
                "spec": {"replicas": 1, "template": {"metadata": {"labels": labels}}}}
            self.pod_records[name] = {"metadata": {"name": name, "namespace": self.namespace, "uid": uid,
                "resourceVersion": "42", "labels": labels, "ownerReferences": [{"kind": "ReplicaSet",
                "name": "rs-" + component, "uid": "rs-" + component, "controller": True}]},
                "spec": {"nodeName": "node-a"}, "status": {"phase": "Running", "ready": True}}
        self.ds = {"metadata": {"name": "aws-node", "uid": "ds-uid", "namespace": "kube-system"}}
        self.system = {"metadata": {"name": "aws-node-a", "namespace": "kube-system", "uid": "system-pod", "resourceVersion": "5",
            "ownerReferences": [{"controller": True, "kind": "DaemonSet", "name": "aws-node", "uid": "ds-uid"}]},
            "spec": {"nodeName": "node-a"}, "status": {"phase": "Running"}}
        self.extras = []
        self.group = {"clusterName": "dev", "nodegroupName": "dev-application", "nodegroupArn":
            f"arn:aws:eks:{REGION}:{ACCOUNT}:nodegroup/dev/dev-application/uuid", "status": "ACTIVE",
            "tags": copy.deepcopy(TAGS), "labels": {"photoplatform.io/workload": "application"}, "health": {"issues": []},
            "subnets": ["subnet-a"], "resources": {"autoScalingGroups": [{"name": "eks-dev-asg"}]}}
        tags = TAGS | {"kubernetes.io/cluster/dev": "owned", "eks:cluster-name": "dev",
                       "eks:nodegroup-name": "dev-application", "aws:autoscaling:groupName": "eks-dev-asg"}
        self.instance = {"InstanceId": "i-0123456789abcdef0", "State": {"Name": "running"}, "SubnetId": "subnet-a",
            "Placement": {"AvailabilityZone": "us-east-1a"}, "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}
        self.reservation = {"OwnerId": ACCOUNT, "Instances": [self.instance]}
        self.asg = {"AutoScalingGroupName": "eks-dev-asg", "Instances": [{"InstanceId": self.instance["InstanceId"],
                    "LifecycleState": "InService"}]}
        self.aws = Mock()
        self.clients = {name: Mock() for name in ("eks", "ec2", "autoscaling")}
        self.aws.client.side_effect = self.clients.__getitem__
        self.clients["eks"].describe_nodegroup.side_effect = lambda **kwargs: {"nodegroup": copy.deepcopy(self.group)}
        self.clients["ec2"].describe_instances.side_effect = lambda **kwargs: {"Reservations": copy.deepcopy([self.reservation])}
        self.clients["autoscaling"].describe_auto_scaling_groups.side_effect = lambda **kwargs: {"AutoScalingGroups": copy.deepcopy([self.asg])}

    def deployment(self, component):
        return copy.deepcopy(self.deployments[component])

    def pods(self, component):
        rows = []
        for pod in self.pod_records.values():
            if pod["metadata"]["labels"].get("app.kubernetes.io/component") != component:
                continue
            owners = pod["metadata"].get("ownerReferences", [])
            if len(owners) != 1 or owners[0].get("uid") != "rs-" + component:
                raise ValueError("Unverified ReplicaSet owner")
            rows.append({"name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"], "phase": pod["status"]["phase"],
                "ready": pod["status"]["ready"], "terminating": bool(pod["metadata"].get("deletionTimestamp")),
                "imageIDs": ["containerd://sha256:" + "b" * 64], "restarts": 0})
        return rows

    def state(self, component):
        dep, pods = self.deployment(component), self.pods(component)
        return {"deployment": dep["metadata"]["name"], "uid": dep["metadata"]["uid"], "replicas": dep["spec"]["replicas"],
                "generation": 1, "observedGeneration": 1, "readyReplicas": sum(p["ready"] for p in pods),
                "updatedReplicas": len(pods), "pods": pods}

    def json(self, *args):
        self.reads.append(args)
        if args[:2] == ("get", "node"):
            if args[2] == "node-a":
                return copy.deepcopy(self.node_record)
            return {"metadata": {"name": args[2], "uid": "other-node-uid"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
        if args[:2] == ("get", "namespace"):
            return copy.deepcopy(self.ns)
        if args[:2] == ("get", "pods"):
            if "--all-namespaces" not in args or "spec.nodeName=node-a" not in args:
                raise AssertionError("Inventory must include ALL namespaces on the exact node")
            rows = list(self.pod_records.values()) + [self.system] + self.extras
            return {"items": copy.deepcopy([p for p in rows if p["spec"].get("nodeName") == "node-a"])}
        if args[:2] == ("get", "pod"):
            return copy.deepcopy(self.pod_records[args[2]])
        if args[:2] == ("get", "daemonset"):
            if "--namespace" not in args or args[args.index("--namespace") + 1] != "kube-system":
                raise AssertionError("System DS read must be namespaced")
            return copy.deepcopy(self.ds)
        if args[:2] == ("get", "hpa"):
            return {"items": copy.deepcopy(self.hpas)}
        raise AssertionError("Unexpected read")

    def run(self, arn, namespace, *args, body=None):
        self.calls.append((arn, namespace, args, copy.deepcopy(body)))
        if (arn, namespace) != (self.arn, self.namespace):
            raise AssertionError("Unscoped mutation")
        if args[:2] == ("patch", "node"):
            if args[2] != "node-a":
                raise AssertionError("Mutation must be for exact resourceName")
            updates = json.loads(args[args.index("-p") + 1])
            candidate = copy.deepcopy(self.node_record)
            for operation in updates:
                parent, key = traverse(candidate, operation["path"])
                if operation["op"] == "test":
                    if parent.get(key) != operation["value"]:
                        raise ValueError("Server-side JSON Patch precondition failed")
                elif operation["op"] in {"add", "replace"}:
                    parent[key] = copy.deepcopy(operation["value"])
                elif operation["op"] == "remove":
                    del parent[key]
                else:
                    raise AssertionError("Unexpected patch")
            candidate["metadata"]["resourceVersion"] = str(int(self.node_record["metadata"]["resourceVersion"]) + 1)
            self.node_record = candidate
            return json.dumps(candidate)
        if args[:2] == ("create", "--raw"):
            name = body["metadata"]["name"]
            expected = f"/api/v1/namespaces/{self.namespace}/pods/{name}/eviction"
            if args[2] != expected or body["apiVersion"] != "policy/v1" or body["kind"] != "Eviction":
                raise AssertionError("Only namespace-scoped policy/v1 Pod evictions are allowed")
            pod = self.pod_records[name]
            if body["deleteOptions"]["preconditions"] != {k: pod["metadata"][k] for k in ("uid", "resourceVersion")}:
                raise ValueError("Eviction identity precondition failed")
            del self.pod_records[name]
            return "{}"
        raise AssertionError("Broad drain, force deletion or other mutation is forbidden")

    def replace(self, target, node_name="node-b"):
        name = target["name"] + "-replacement"
        self.pod_records[name] = {"metadata": {"name": name, "namespace": self.namespace, "uid": str(__import__("uuid").uuid4()),
            "resourceVersion": "1", "labels": {"app.kubernetes.io/name": "photoplatform", "app.kubernetes.io/instance": self.release,
            "app.kubernetes.io/component": target["component"]}, "ownerReferences": [{"controller": True, "kind": "ReplicaSet",
            "name": "rs-" + target["component"], "uid": "rs-" + target["component"]}]},
            "spec": {"nodeName": node_name}, "status": {"phase": "Running", "ready": True}}


class NodeDrainSafetyTests(unittest.TestCase):
    def setUp(self):
        self.cluster = Cluster()
        environment = patch.dict(os.environ, ENV, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        transport = patch.object(drain, "kubectl", side_effect=self.cluster.run)
        transport.start()
        self.addCleanup(transport.stop)

    def selected(self):
        return drain.NodeDrain(self.cluster.aws, self.cluster, "node-a", NODE_UID)

    def test_explicit_opt_in_and_exclusive_window_precede_all_mutations(self):
        for variable in ("ALLOW_EKS_NODE_DRAIN", "EKS_RELEASE_SCENARIO_LOCK_ID"):
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: ""}):
                with self.assertRaises(ValueError):
                    self.selected()
        self.assertEqual(self.cluster.calls, [])

    def test_exact_node_uid_and_explicit_label_required(self):
        for mutate in (lambda: self.cluster.node_record["metadata"].update(uid="another-node"),
                       lambda: self.cluster.node_record["metadata"]["labels"].pop("photoplatform.io/drain-allowed")):
            self.cluster = Cluster()
            mutate()
            with self.assertRaises(ValueError):
                self.selected()
            self.assertEqual(self.cluster.calls, [])

    def test_actual_ec2_account_tags_zone_and_asg_membership_are_required(self):
        mutations = [lambda c: c.reservation.update(OwnerId="111111111111"),
            lambda c: c.instance["Placement"].update(AvailabilityZone="us-west-2a"),
            lambda c: c.instance.update(Tags=[t for t in c.instance["Tags"] if t["Key"] != "DisposableEnvironment"]),
            lambda c: c.instance.update(Tags=[t | {"Value": "other"} if t["Key"] == "eks:nodegroup-name" else t for t in c.instance["Tags"]]),
            lambda c: c.asg.update(Instances=[]), lambda c: c.group.update(status="UPDATING"),
            lambda c: c.group["tags"].update(Environment="prod"),
            lambda c: c.group.update(nodegroupArn=c.group["nodegroupArn"].replace(ACCOUNT, "111111111111"))]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.cluster = Cluster()
                mutate(self.cluster)
                with self.assertRaises(ValueError):
                    self.selected()
                self.assertEqual(self.cluster.calls, [])

    def test_all_namespace_inventory_rejects_shared_static_unmanaged_and_wrong_owner_pods(self):
        mutations = [lambda c: c.pod_records["photo-api"]["metadata"].update(namespace="other-tenant"),
            lambda c: c.pod_records["photo-api"]["metadata"].update(annotations={"kubernetes.io/config.mirror": "hash"}),
            lambda c: c.pod_records["photo-api"]["metadata"].update(ownerReferences=[]),
            lambda c: c.pod_records["photo-api"]["metadata"]["ownerReferences"][0].update(uid="spoofed-rs"),
            lambda c: c.pod_records["photo-api"]["metadata"]["labels"].update({"app.kubernetes.io/component": "queue-exporter"}),
            lambda c: c.system["metadata"].update(namespace="other-system"),
            lambda c: c.ds["metadata"].update(uid="replaced-system-ds")]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.cluster = Cluster()
                mutate(self.cluster)
                with self.assertRaises(ValueError):
                    self.selected()
                self.assertEqual(self.cluster.calls, [])

    def test_exact_cordon_evictions_recovery_and_restore_leave_system_ds_untouched(self):
        selected = self.selected()
        plan = selected.plan()
        self.assertTrue(any("--all-namespaces" in call for call in self.cluster.reads))
        self.assertEqual(plan["systemDaemonSetPods"][0]["action"], "untouched")
        self.assertFalse(plan["operatorWindowIsDistributedLock"])
        selected.cordon()
        self.assertTrue(self.cluster.node_record["spec"]["unschedulable"])
        for target in selected.targets:
            selected.evict(target)
            self.cluster.replace(target)
        recovered = selected.recovered()
        self.assertTrue(all(p["nodeName"] == "node-b" for state in recovered.values() for p in state["replacementsOnDifferentNodes"]))
        self.assertEqual(selected.restore()["status"], "PASS")
        self.assertNotIn("unschedulable", self.cluster.node_record["spec"])
        self.assertNotIn("annotations", self.cluster.node_record["metadata"])
        self.assertEqual(self.cluster.system["metadata"]["uid"], "system-pod")
        self.assertEqual(len([call for call in self.cluster.calls if call[2][0] == "create"]), 2)
        for call in self.cluster.calls:
            if call[2][0] == "patch":
                updates = json.loads(call[2][call[2].index("-p") + 1])
                self.assertEqual([u["path"] for u in updates[:2]], ["/metadata/uid", "/metadata/resourceVersion"])
            else:
                self.assertEqual(call[3]["deleteOptions"]["preconditions"].keys(), {"uid", "resourceVersion"})

    def test_restore_preserves_explicit_false_and_concurrent_unrelated_annotations(self):
        self.cluster.node_record["spec"]["unschedulable"] = False
        self.cluster.node_record["metadata"]["annotations"] = {"owner.example/note": "original"}
        selected = self.selected()
        selected.cordon()
        self.cluster.node_record["metadata"]["annotations"]["new.example/note"] = "concurrent"
        self.assertEqual(selected.restore()["status"], "PASS")
        self.assertIs(self.cluster.node_record["spec"]["unschedulable"], False)
        self.assertEqual(self.cluster.node_record["metadata"]["annotations"], {
            "owner.example/note": "original", "new.example/note": "concurrent"})

    def test_changed_resource_version_or_workload_aborts_before_cordon(self):
        selected = self.selected()
        self.cluster.node_record["metadata"]["resourceVersion"] = "8"
        with self.assertRaises(ValueError):
            selected.cordon()
        self.assertEqual(self.cluster.calls, [])

    def test_new_shared_workload_between_preflight_and_cordon_is_rejected(self):
        selected = self.selected()
        shared = copy.deepcopy(self.cluster.pod_records["photo-api"])
        shared["metadata"].update(name="foreign", namespace="another-app")
        self.cluster.extras.append(shared)
        with self.assertRaises(ValueError):
            selected.cordon()
        self.assertEqual(self.cluster.calls, [])

    def test_pod_uid_or_resource_version_change_prevents_eviction(self):
        for field in ("uid", "resourceVersion"):
            self.cluster = Cluster()
            selected = self.selected()
            with patch.object(drain, "kubectl", side_effect=self.cluster.run):
                selected.cordon()
                target = selected.targets[0]
                self.cluster.pod_records[target["name"]]["metadata"][field] = "changed"
                with self.assertRaises(ValueError):
                    selected.evict(target)
                self.assertFalse(any(call[2][0] == "create" for call in self.cluster.calls))
                self.assertEqual(selected.restore()["status"], "PASS")

    def test_pdb_rejection_never_falls_back_to_delete_and_still_restores(self):
        selected = self.selected()
        selected.cordon()
        with patch.object(drain, "kubectl", side_effect=RuntimeError("PDB refuses disruption")):
            with self.assertRaises(RuntimeError):
                selected.evict(selected.targets[0])
        self.assertEqual(selected.evicted, [])
        self.assertEqual(selected.restore()["status"], "PASS")
        self.assertFalse(any("delete" in call[2] or "drain" in call[2] for call in self.cluster.calls))

    def test_ambiguous_cordon_transport_failure_is_restored_using_ownership_marker(self):
        selected = self.selected()
        def apply_then_fail(*args, **kwargs):
            self.cluster.run(*args, **kwargs)
            raise RuntimeError("Connection lost after API server applied patch")
        with patch.object(drain, "kubectl", side_effect=apply_then_fail):
            with self.assertRaises(RuntimeError):
                selected.cordon()
        self.assertTrue(selected.attempted)
        self.assertEqual(selected.restore()["status"], "PASS")
        self.assertNotIn("unschedulable", self.cluster.node_record["spec"])

    def test_restoration_refuses_replaced_node_other_marker_or_changed_cordon(self):
        for mutate in (lambda c: c.node_record["metadata"].update(uid="replacement-node"),
                       lambda c: c.node_record["metadata"]["annotations"].update({drain.MARKER: "someone-else"}),
                       lambda c: c.node_record["spec"].update(unschedulable=False)):
            self.cluster = Cluster()
            selected = self.selected()
            with patch.object(drain, "kubectl", side_effect=self.cluster.run):
                selected.cordon()
                mutate(self.cluster)
                writes = len(self.cluster.calls)
                result = selected.restore()
                self.assertEqual(result["status"], "FAIL")
                self.assertTrue(result["manualPlanRequired"])
                self.assertEqual(len(self.cluster.calls), writes)

    def test_replacement_on_same_node_or_concurrent_deployment_edit_cannot_pass(self):
        selected = self.selected()
        selected.cordon()
        for target in selected.targets:
            selected.evict(target)
        for target in selected.targets:
            self.cluster.replace(target, "node-a")
        with self.assertRaises(ValueError):
            selected.recovered()
        self.assertEqual(selected.restore()["status"], "PASS")
        self.cluster = Cluster()
        selected = self.selected()
        self.cluster.deployments["api"]["spec"]["replicas"] = 2
        with self.assertRaises(ValueError):
            selected.cordon()
        self.assertEqual(self.cluster.calls, [])

    def test_hpa_and_initially_cordoned_node_are_refused(self):
        self.cluster.hpas = [{"spec": {"scaleTargetRef": {"kind": "Deployment", "name": "photo-api"}}}]
        with self.assertRaises(ValueError):
            self.selected()
        self.cluster.hpas = []
        self.cluster.node_record["spec"]["unschedulable"] = True
        with self.assertRaises(ValueError):
            self.selected()
        self.assertEqual(self.cluster.calls, [])

    def test_durable_callback_failure_prevents_selected_pod_eviction(self):
        selected = self.selected()
        selected.cordon()
        callback = Mock(side_effect=ValueError("Job completed or owner changed"))
        with self.assertRaises(ValueError):
            selected.evict(selected.targets[0], callback)
        callback.assert_called_once_with(selected.targets[0])
        self.assertEqual(selected.evicted, [])
        self.assertFalse(any(call[2][0] == "create" for call in self.cluster.calls))

    def test_expired_deadline_after_guard_reads_prevents_new_mutations(self):
        selected = self.selected()
        deadline = Mock()
        deadline.remaining.side_effect = TimeoutError("Expired during preflight reads")
        with self.assertRaises(TimeoutError):
            selected.cordon(deadline)
        self.assertEqual(self.cluster.calls, [])
        self.assertFalse(selected.attempted)
        selected.cordon()
        with self.assertRaises(TimeoutError):
            selected.evict(selected.targets[0], deadline=deadline)
        self.assertFalse(any(call[2][0] == "create" for call in self.cluster.calls))
        self.assertEqual(selected.restore()["status"], "PASS")

    def test_exact_evicted_terminal_pod_waits_for_deletion_before_recovery(self):
        selected = self.selected()
        selected.cordon()
        target = selected.targets[0]
        old = copy.deepcopy(self.cluster.pod_records[target["name"]])
        selected.evict(target)
        old["status"].update(phase="Succeeded", ready=False)
        old["metadata"]["deletionTimestamp"] = "2026-09-27T12:00:00Z"
        self.cluster.pod_records[target["name"]] = old
        self.assertEqual(selected.inventory()[0][0]["uid"], target["uid"])
        self.assertIsNone(selected.recovered())


class NodeDrainCliTests(unittest.TestCase):
    def test_cli_bounds_and_incomplete_durable_selector_fail_before_guard(self):
        for extra in (["--timeout", "901"], ["--timeout", "0"], ["--upload-id", "only-one"], ["--minimum-job-age-seconds", "301"]):
            with self.subTest(extra=extra), patch.object(sys, "argv", ["drain", "--node-name", "node-a", "--node-uid", NODE_UID, *extra]), \
                    patch.object(drain, "eks_guard") as guard, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    drain.main()
                self.assertEqual(result.exception.code, 2)
                guard.assert_not_called()

    def test_prewrite_plan_and_final_restoration_are_retained_on_mutation_failure(self):
        node = Mock()
        node.plan.return_value = {"nodeName": "node-a", "safeRestorePlan": True}
        node.cordon.side_effect = RuntimeError("PDB/transport failure")
        node.restore.return_value = {"status": "PASS"}
        api = Mock()
        reports = []
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, ENV, clear=True), \
                patch.object(sys, "argv", ["drain", "--node-name", "node-a", "--node-uid", NODE_UID, "--output", str(Path(folder) / "report.json")]), \
                patch.object(drain, "eks_guard", return_value=(Mock(), Mock(), {"verified": True})), \
                patch.object(drain, "BoundedAPI", return_value=api), patch.object(drain, "NodeDrain", return_value=node), \
                patch.object(drain, "write_report", side_effect=lambda path, report: reports.append(copy.deepcopy(report))):
            self.assertEqual(drain.main(), 1)
        self.assertIn("restorePlan", reports[0])
        self.assertNotIn("fatalErrorType", reports[0])
        self.assertEqual(reports[-1]["fatalErrorType"], "RuntimeError")
        self.assertEqual(reports[-1]["restoration"]["status"], "PASS")
        node.restore.assert_called_once()
        api.close.assert_called_once()


class NodeDrainBusinessTests(unittest.TestCase):
    def test_actual_private_smoke_records_only_safe_response_evidence_and_cleans_up(self):
        env = ENV | {"TEST_OTHER_TOKEN": "other-token-secret", "S3_BUCKET": "dev-bucket", "CLOUDFRONT_DOMAIN": "dev.cloudfront.net"}
        api = Mock()
        api.token = "owner-token-secret"
        api.deadline = drain.Deadline(900)
        api.expect.side_effect = lambda response: response.json()
        def request(method, path, **kwargs):
            if path == "/api/auth/me":
                return Mock(status_code=200, json=lambda: {"id": 2 if kwargs.get("token") else 1, "role": "USER"})
            if kwargs.get("token") or kwargs.get("anonymous"):
                return Mock(status_code=403)
            return Mock(status_code=200, json=lambda: {"url": "https://dev.cloudfront.net/media/1?Signature=secret-signature&Key-Pair-Id=abc&Expires=123"})
        api.request.side_effect = request
        api.create.return_value = {"uploadId": "upload-safe-id", "mediaId": 1,
            "uploadUrl": "https://dev-bucket.s3.us-east-1.amazonaws.com/staging/image?X-Amz-Signature=secret-upload-signature",
            "headers": {"Content-Type": "image/jpeg"}}
        api.session.put.return_value = Mock(status_code=200)
        api.session.get.side_effect = [Mock(status_code=403), Mock(status_code=200), Mock(status_code=403)]
        with patch.dict(os.environ, env, clear=True):
            result = drain.private_smoke(api, api.deadline)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["cleanupStatus"], "PASS")
        api.cleanup.assert_called_once_with(api.create.return_value)
        self.assertEqual(api.session.get.call_count, 3)
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("cloudfront.net", json.dumps(result))

    def test_smoke_failure_still_uses_separate_cleanup_deadline(self):
        env = ENV | {"TEST_OTHER_TOKEN": "other", "S3_BUCKET": "dev-bucket"}
        api = Mock()
        api.token = "owner"
        old_deadline = api.deadline = drain.Deadline(1)
        api.expect.side_effect = lambda response: response.json()
        api.request.side_effect = [Mock(json=lambda: {"id": 1, "role": "USER"}), Mock(json=lambda: {"id": 2, "role": "USER"})]
        api.create.return_value = {"uploadId": "safe-id", "mediaId": 1, "headers": {},
            "uploadUrl": "https://dev-bucket.s3.us-east-1.amazonaws.com/staging/image?X-Amz-Signature=secret"}
        api.session.put.side_effect = TimeoutError("Expired observation")
        api.cleanup.side_effect = lambda upload: self.assertIsNot(api.deadline, old_deadline)
        with patch.dict(os.environ, env, clear=True):
            result = drain.private_smoke(api, old_deadline)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["cleanupStatus"], "PASS")
        self.assertEqual(result["uploadId"], "safe-id")
        self.assertEqual(result["mediaId"], 1)
        self.assertIs(api.deadline, old_deadline)

    def test_finished_job_requires_exact_done_identity_one_row_and_single_media_job(self):
        client = MagicMock()
        db = client.connect.return_value.__enter__.return_value
        row = (WORKER_UID, 7, "DONE", "photo-media-worker", 2, True, True, 1)
        db.execute.return_value.fetchall.return_value = [row]
        with patch.dict(sys.modules, {"psycopg": client}), patch.dict(os.environ, {"BENCHMARK_DATABASE_URL": "not-serialized"}):
            result = drain.finished_job(7, WORKER_UID)
            self.assertEqual(result["mediaJobCount"], 1)
            self.assertEqual(db.execute.call_args_list[0].args, ("SET TRANSACTION READ ONLY",))
            for rows in ([], [row, row], [row[:2] + ("RUNNING",) + row[3:]], [row[:-1] + (2,)]):
                db.execute.return_value.fetchall.return_value = rows
                with self.assertRaises(AssertionError):
                    drain.finished_job(7, WORKER_UID)


if __name__ == "__main__":
    unittest.main()
