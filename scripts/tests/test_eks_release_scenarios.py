"""Release rehearsal safety tests; no cloud, network, credentials or real CLIs."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import eks_release_scenarios as scenarios


SHA = "a" * 40
PREVIOUS_SHA = "c" * 40
ARN = "arn:aws:eks:us-east-1:012345678901:cluster/dev"
IMAGE = "012345678901.dkr.ecr.us-east-1.amazonaws.com/api@sha256:" + "b" * 64
WORKER = IMAGE.replace("/api@", "/worker@")
LEAF = "sha256:" + "d" * 64
MANIFEST = {"sha": SHA, "images": {"api": IMAGE, "worker": WORKER},
            "runnableDigests": {"api": [LEAF], "worker": [LEAF]}}
PREVIOUS_MANIFEST = {"sha": PREVIOUS_SHA,
    "images": {"api": IMAGE.replace("b" * 64, "e" * 64), "worker": WORKER.replace("b" * 64, "e" * 64)},
    "runnableDigests": {"api": ["sha256:" + "f" * 64], "worker": ["sha256:" + "f" * 64]}}
GATES = {"ALLOW_EKS_RELEASE_SCENARIOS": "1", "EKS_RELEASE_SCENARIO_LOCK_ID": "run-37-exclusive-dev"}
POD_SECURITY = {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}}
CONTAINER_SECURITY = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                      "capabilities": {"drop": ["ALL"]}}


def labels(component):
    return {"app.kubernetes.io/name": "photoplatform", "app.kubernetes.io/instance": "photoplatform",
            "app.kubernetes.io/component": component}


def deployment(component, manifest=MANIFEST):
    image = manifest["images"][scenarios.COMPONENTS[component]]
    image_digest = image.split("@")[-1]
    env = [{"name": "OTEL_RESOURCE_ATTRIBUTES", "value":
        f"k8s.cluster.name=dev,k8s.namespace.name=$(POD_NAMESPACE),k8s.pod.name=$(POD_NAME),k8s.deployment.name=photoplatform-{component},service.version={manifest['sha']},container.image.id={image_digest}"}]
    if component == "api":
        env.append({"name": "APP_RELEASE_SHA", "value": manifest["sha"]})
    return {"apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "photoplatform-" + component, "namespace": "photoplatform-dev",
            "uid": "dep-" + component, "resourceVersion": "42", "generation": 1,
            "labels": labels(component), "annotations": {"photoplatform.io/revision": manifest["sha"]}},
        "spec": {"replicas": 2, "selector": {"matchLabels": labels(component)},
            "template": {"metadata": {"labels": labels(component), "annotations": {
                "photoplatform.io/revision": manifest["sha"], "photoplatform.io/image-digest": image_digest,
                "operator.example/preserved": "yes"}},
                "spec": {"containers": [{"name": component, "image": image, "env": env}]}}},
        "status": {"observedGeneration": 1, "replicas": 2, "updatedReplicas": 2,
                   "readyReplicas": 2, "availableReplicas": 2}}


class FakeControl:
    arn, namespace, release, namespace_uid = ARN, "photoplatform-dev", "photoplatform", "namespace-identity"
    sha, manifest = SHA, MANIFEST

    def __init__(self):
        self.deps = {component: deployment(component) for component in scenarios.COMPONENT_SET}
        self.pod_ids = {component: [component + "-old-1", component + "-old-2"] for component in scenarios.COMPONENT_SET}
        self.namespace_object = {"metadata": {"uid": self.namespace_uid, "labels": {
            "app.kubernetes.io/part-of": "photoplatform",
            "photoplatform.io/environment": "dev", "photoplatform.io/disposable": "true"}}}
        self.service = {"metadata": {"name": "photoplatform-api", "uid": "service-api"},
                        "spec": {"selector": labels("api")}}
        self.endpoint_objects = None
        self.hpas = []

    def deployment(self, component):
        return copy.deepcopy(self.deps[component])

    def state(self, component):
        dep = self.deps[component]
        return {"uid": dep["metadata"]["uid"], "replicas": dep["spec"]["replicas"],
            "generation": dep["metadata"]["generation"], "observedGeneration": dep["status"]["observedGeneration"],
            "pods": [{"uid": uid, "ready": True, "terminating": False} for uid in self.pod_ids[component]]}

    def selector(self, component):
        return "app.kubernetes.io/instance=photoplatform,app.kubernetes.io/component=" + component

    def json(self, *args):
        if args[:2] == ("get", "namespace"):
            return copy.deepcopy(self.namespace_object)
        if args[:2] == ("get", "secret"):
            revision = int(args[2].rsplit("v", 1)[1])
            return {"type": "helm.sh/release.v1", "metadata": {
                "name": args[2], "uid": "helm-uid-" + str(revision), "resourceVersion": "91",
                "namespace": self.namespace, "labels": {
                    "owner": "helm", "name": self.release, "version": str(revision)}}}
        if args[:2] == ("get", "services"):
            return {"items": [copy.deepcopy(self.service)]}
        if args[:2] == ("get", "hpa"):
            return {"items": copy.deepcopy(self.hpas)}
        if args[:2] == ("get", "endpointslices"):
            items = self.endpoint_objects
            if items is None:
                items = [{"metadata": {"ownerReferences": [{"kind": "Service", "uid": "service-api"}]},
                    "endpoints": [{"conditions": {"ready": True}, "targetRef": {
                        "kind": "Pod", "namespace": self.namespace, "uid": uid}} for uid in self.pod_ids["api"]]}]
            return {"items": copy.deepcopy(items)}
        raise AssertionError("Unexpected mocked Kubernetes read: " + repr(args))

    def apply_patch(self, context, namespace, *args, body=None):
        self.last_patch = (context, namespace, args, body)
        self.assert_scope(context, namespace)
        if args[:2] != ("patch", "deployment"):
            raise AssertionError("Unexpected mocked mutation")
        component = args[2].removeprefix("photoplatform-")
        dep = self.deps[component]
        operations = json.loads(args[args.index("--patch") + 1])
        for operation in operations:
            if operation["op"] == "test":
                node = dep
                for key in operation["path"].strip("/").split("/"):
                    node = node[key.replace("~1", "/").replace("~0", "~")]
                if node != operation["value"]:
                    raise RuntimeError("Server rejected mutation precondition")
            elif operation["op"] == "add":
                dep["spec"]["template"]["metadata"]["annotations"][scenarios.MARKER] = operation["value"]
            elif operation["op"] == "remove":
                del dep["spec"]["template"]["metadata"]["annotations"][scenarios.MARKER]
            else:
                raise AssertionError("Unexpected patch operation")
        dep["metadata"]["resourceVersion"] = str(int(dep["metadata"]["resourceVersion"]) + 1)
        dep["metadata"]["generation"] += 1
        dep["status"]["observedGeneration"] = dep["metadata"]["generation"]
        self.pod_ids[component] = [component + "-new-" + str(dep["metadata"]["generation"]) + "-" + str(i) for i in (1, 2)]
        return json.dumps(dep)

    def assert_scope(self, context, namespace):
        if (context, namespace) != (self.arn, self.namespace):
            raise AssertionError("Unscoped mutation")


def source_migration(control):
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {
        "name": "photoplatform-migrate-previous", "namespace": control.namespace, "uid": "migration-source-uid",
        "resourceVersion": "77", "labels": labels("migrator"), "annotations": {"photoplatform.io/revision": SHA}},
        "status": {"conditions": [{"type": "Complete", "status": "True"}]},
        "spec": {"activeDeadlineSeconds": 900, "backoffLimit": 1,
            "template": {"metadata": {"annotations": {"photoplatform.io/revision": SHA}}, "spec": {
            "restartPolicy": "Never", "serviceAccountName": "photoplatform-migrator",
            "securityContext": copy.deepcopy(POD_SECURITY), "nodeSelector": {"photoplatform.io/workload": "application"},
            "containers": [{"name": "migrator", "image": IMAGE, "command": ["/app/migrate.sh"],
                "securityContext": copy.deepcopy(CONTAINER_SECURITY), "resources": {"limits": {"memory": "768Mi"}},
                "env": [{"name": "MIGRATOR_DATABASE_PASSWORD_FILE", "value": "/mnt/secrets/DB_PASSWORD"}],
                "envFrom": [{"secretRef": {"name": "never-clone-me"}}],
                "volumeMounts": [{"name": "runtime-secrets", "mountPath": "/mnt/secrets"}]}],
            "volumes": [{"name": "runtime-secrets", "csi": {"driver": "secrets-store.csi.k8s.io"}}],
            "imagePullSecrets": [{"name": "never-clone-image-credentials"}]}}}}


def resources(manifest):
    objects = {}
    for component in scenarios.COMPONENT_SET:
        item = deployment(component, manifest)
        item.pop("status")
        for key in ("uid", "resourceVersion", "generation"):
            item["metadata"].pop(key)
        objects["Deployment/" + item["metadata"]["name"]] = item
    config = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {
        "name": "photoplatform-config", "namespace": "photoplatform-dev", "labels": labels("api")},
        "data": {"DATABASE_HOST": "fixed.database.internal", "DATABASE_PORT": "5432"}}
    objects["ConfigMap/photoplatform-config"] = config
    return objects


def snapshot(revision, manifest):
    objects = resources(manifest)
    values = {"environment": "dev", "runtimeMode": "aws", "release": {"commitSha": manifest["sha"]},
              "migration": {"enabled": False}, "aws": {"accountId": "012345678901", "clusterName": "dev"},
              "secrets": {"api": {"arn": "arn:aws:secretsmanager:us-east-1:012345678901:secret:api-reference"}},
              "config": {"databaseHost": "fixed.database.internal", "storageBucket": "fixed-disposable-bucket"}}
    return {"revision": revision, "objects": objects, "values": values,
            "components": {component: "Deployment/photoplatform-" + component for component in scenarios.COMPONENT_SET},
            "manifestSha256": scenarios.digest(objects), "valuesSha256": scenarios.digest(values),
            "storage": {"name": "sh.helm.release.v1.photoplatform.v" + str(revision),
                        "uid": "helm-uid-" + str(revision), "resourceVersion": "91", "revision": revision}}


def render(objects):
    return "\n---\n".join(json.dumps(item) for item in objects.values())


class GateAndHelmTests(unittest.TestCase):
    def test_explicit_gate_and_maintenance_id_are_required_before_cloud_guard(self):
        for env in ({}, {"ALLOW_EKS_RELEASE_SCENARIOS": "1"},
                    {**GATES, "EKS_RELEASE_SCENARIO_LOCK_ID": "invalid lock with spaces"}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), \
                    patch.object(scenarios, "eks_guard") as guard, patch.object(scenarios, "kubectl") as kube, \
                    patch.object(scenarios, "helm") as helm, patch.object(scenarios, "write_report") as report, \
                    patch("sys.argv", ["eks_release_scenarios.py", "--scenario", "rollout", "--output", "ignored.json"]), \
                    patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(scenarios.main(), 1)
                guard.assert_not_called()
                kube.assert_not_called()
                helm.assert_not_called()
                self.assertEqual(report.call_args.args[1]["fatalErrorType"], "ValueError")

    def test_helm_uses_pinned_scope_secret_storage_and_removes_all_kube_overrides(self):
        env = {"PATH": "/safe/bin", "HELM_KUBEAPISERVER": "https://attacker.invalid",
               "HELM_KUBETOKEN": "SECRET_TOKEN", "HELM_KUBECAFILE": "/wrong/ca",
               "HELM_KUBECONTEXT": "untrusted", "HELM_KUBEINSECURE_SKIP_TLS_VERIFY": "1",
               "HELM_DRIVER": "configmap", "KUBECONFIG": "/verified/kubeconfig"}
        with patch.dict(os.environ, env, clear=True), patch.object(scenarios.subprocess, "run") as run:
            run.return_value = Mock(returncode=0, stdout="bounded result")
            self.assertEqual(scenarios.helm(FakeControl(), "history", "photoplatform"), "bounded result")
        args, kwargs = run.call_args
        self.assertEqual(args[0][:5], ["helm", "--kube-context", ARN, "--namespace", "photoplatform-dev"])
        self.assertFalse(any(key.startswith("HELM_KUBE") for key in kwargs["env"]))
        self.assertEqual(kwargs["env"]["HELM_DRIVER"], "secret")
        self.assertEqual(kwargs["env"]["KUBECONFIG"], "/verified/kubeconfig")
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(kwargs["timeout"], 45)

    def test_failed_helm_stderr_and_stdout_do_not_escape_in_exception(self):
        with patch.object(scenarios.subprocess, "run", return_value=Mock(returncode=1,
                stdout="TOKEN=never-persist-this", stderr="PASSWORD=never-persist-this")):
            with self.assertRaises(RuntimeError) as raised:
                scenarios.helm(FakeControl(), "history", "photoplatform")
        self.assertEqual(str(raised.exception), "Scoped Helm operation failed")
        self.assertNotIn("never-persist", repr(raised.exception))

    def test_changed_disposable_namespace_blocks_mutation(self):
        control = FakeControl()
        for changes in ({"uid": "recreated-namespace"}, {"deletionTimestamp": "now"},
                        {"labels": {"photoplatform.io/environment": "prod", "photoplatform.io/disposable": "true"}}):
            with self.subTest(changes=changes):
                original = copy.deepcopy(control.namespace_object)
                control.namespace_object["metadata"].update(changes)
                with self.assertRaises(ValueError):
                    scenarios.boundary(control)
                control.namespace_object = original


class RollingReplacementTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, GATES, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.control = FakeControl()
        self.pin = Mock(storage={"uid": "helm-uid-7"})

    def test_rollout_patch_has_server_side_uid_resource_version_and_sha_tests(self):
        replacement = scenarios.RollingReplacement(self.control, self.pin)
        original = copy.deepcopy(self.control.deps["api"]["spec"])
        with patch.object(scenarios, "kubectl", side_effect=self.control.apply_patch):
            replacement.patch("api")
            args = self.control.last_patch[2]
            ops = json.loads(args[args.index("--patch") + 1])
            self.assertEqual(ops[:3], [
                {"op": "test", "path": "/metadata/uid", "value": "dep-api"},
                {"op": "test", "path": "/metadata/resourceVersion", "value": "42"},
                {"op": "test", "path": "/spec/template/metadata/annotations/photoplatform.io~1revision", "value": SHA}])
            changed = copy.deepcopy(self.control.deps["api"]["spec"])
            marker = changed["template"]["metadata"]["annotations"].pop(scenarios.MARKER)
            self.assertEqual(marker, replacement.token)
            self.assertEqual(changed, original)
            replacement.patch("api", restore=True)
        self.assertEqual(self.control.deps["api"]["spec"], original)

    def test_response_loss_after_patch_still_restores_original_spec(self):
        originals = copy.deepcopy(self.control.deps)
        calls = []

        def response_loss(*args, **kwargs):
            result = self.control.apply_patch(*args, **kwargs)
            calls.append(args)
            if len(calls) == 1:
                raise TimeoutError("response lost after server committed patch")
            return result

        report = {}
        with patch.object(scenarios, "HelmPin", return_value=self.pin), \
                patch.object(scenarios, "kubectl", side_effect=response_loss), \
                patch.object(scenarios, "write_report"):
            with self.assertRaises(TimeoutError):
                scenarios.rollout_scenario(self.control, report, "ignored.json", 5)
        self.assertEqual(len(calls), 2)
        self.assertEqual(report["restoreStatus"], "PASS")
        self.assertEqual(self.control.deps["api"]["spec"], originals["api"]["spec"])
        self.assertEqual(self.control.deps["media-worker"], originals["media-worker"])

    def test_rollout_restoration_refuses_external_spec_or_uid_change(self):
        for change in ("replicas", "uid", "marker"):
            with self.subTest(change=change):
                control = FakeControl()
                replacement = scenarios.RollingReplacement(control, self.pin)
                with patch.object(scenarios, "kubectl", side_effect=control.apply_patch):
                    replacement.changed.append("api")
                    replacement.patch("api")
                if change == "replicas":
                    control.deps["api"]["spec"]["replicas"] = 3
                elif change == "uid":
                    control.deps["api"]["metadata"]["uid"] = "replacement-deployment"
                else:
                    control.deps["api"]["spec"]["template"]["metadata"]["annotations"][scenarios.MARKER] = "someone-else"
                external = copy.deepcopy(control.deps["api"])
                with patch.object(scenarios, "kubectl") as mutate:
                    result = replacement.restore(1)
                mutate.assert_not_called()
                self.assertEqual(result[0]["status"], "FAIL")
                self.assertEqual(control.deps["api"], external)

    def test_stale_scenario_marker_requires_recovery_before_rollout(self):
        self.control.deps["api"]["spec"]["template"]["metadata"]["annotations"][scenarios.MARKER] = "old-run"
        with self.assertRaises(ValueError), patch.object(scenarios, "kubectl") as mutate:
            scenarios.RollingReplacement(self.control, self.pin)
        mutate.assert_not_called()

    def test_rollout_gate_applies_to_restoration_mutations_too(self):
        replacement = scenarios.RollingReplacement(self.control, self.pin)
        with patch.dict(os.environ, {"ALLOW_EKS_RELEASE_SCENARIOS": "0"}), \
                patch.object(scenarios, "kubectl") as mutate:
            with self.assertRaises(ValueError):
                replacement.patch("api", restore=True)
        mutate.assert_not_called()

    def test_ready_requires_new_uid_set_full_counts_and_exact_service_membership(self):
        old = list(self.control.pod_ids["api"])
        with patch.object(scenarios.time, "monotonic", side_effect=[0, 2]), patch.object(scenarios.time, "sleep"):
            with self.assertRaises(TimeoutError):
                scenarios.ready(self.control, "api", "dep-api", 2, 1, old)
        self.control.pod_ids["api"] = ["api-new-1", "api-new-2"]
        self.control.deps["api"]["status"]["replicas"] = 3
        with patch.object(scenarios.time, "monotonic", side_effect=[0, 2]), patch.object(scenarios.time, "sleep"):
            with self.assertRaises(TimeoutError):
                scenarios.ready(self.control, "api", "dep-api", 2, 1, old)
        self.control.deps["api"]["status"]["replicas"] = 2
        self.control.endpoint_objects = [{"metadata": {"ownerReferences": [{"kind": "Service", "uid": "service-api"}]},
            "endpoints": [{"conditions": {"ready": True}, "targetRef": {"kind": "Pod", "uid": "old-serving-pod"}}]}]
        with patch.object(scenarios.time, "monotonic", side_effect=[0, 2]), patch.object(scenarios.time, "sleep"):
            with self.assertRaises(TimeoutError):
                scenarios.ready(self.control, "api", "dep-api", 2, 1, old)

    def test_endpoint_label_spoof_without_service_ownership_is_rejected(self):
        self.control.endpoint_objects = [{"metadata": {"ownerReferences": [{"kind": "Service", "uid": "other-service"}]},
            "endpoints": []}]
        with self.assertRaises(ValueError):
            scenarios.api_endpoints(self.control, self.control.state("api"))

    def test_matching_hpa_blocks_rollout_before_any_patch(self):
        self.control.hpas = [{"spec": {"scaleTargetRef": {"kind": "Deployment", "name": "photoplatform-api"}}}]
        with patch.object(scenarios, "kubectl") as mutate, self.assertRaises(ValueError):
            scenarios.RollingReplacement(self.control, self.pin)
        mutate.assert_not_called()


class MigrationFailureTests(unittest.TestCase):
    def setUp(self):
        self.control = FakeControl()
        self.source = source_migration(self.control)

    def clone(self):
        return scenarios.migration_clone(self.control, self.source, "photoplatform-migration-failure-test", 900)

    def test_clone_rebuilds_no_secret_pod_and_does_not_mutate_successful_source(self):
        original = copy.deepcopy(self.source)
        job = self.clone()
        spec = job["spec"]["template"]["spec"]
        self.assertEqual(self.source, original)
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertLessEqual(job["spec"]["activeDeadlineSeconds"], 120)
        self.assertEqual(spec["restartPolicy"], "Never")
        self.assertIs(spec["automountServiceAccountToken"], False)
        self.assertEqual(spec["serviceAccountName"], job["metadata"]["name"])
        self.assertNotEqual(spec["serviceAccountName"], "photoplatform-migrator")
        self.assertEqual(spec["volumes"], [{"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}}])
        self.assertEqual(spec["containers"][0]["command"], ["/app/migrate.sh"])
        self.assertEqual({item["name"]: item["value"] for item in spec["containers"][0]["env"]}, scenarios.MIGRATION_ENV)
        serialized = json.dumps(job)
        for forbidden in ("secretRef", "secretKeyRef", "envFrom", "csi", "initContainers", "imagePullSecrets",
                          "never-clone", "/mnt/secrets", "_FILE", "MIGRATOR_DATABASE_PASSWORD_FILE"):
            self.assertNotIn(forbidden, serialized)

    def test_source_must_be_completed_same_namespace_release_sha_digest_and_entrypoint(self):
        cases = [(["kind"], "Deployment"), (["metadata", "namespace"], "other-namespace"),
            (["metadata", "labels", "app.kubernetes.io/name"], "other-app"),
            (["metadata", "labels", "app.kubernetes.io/instance"], "other-release"),
            (["metadata", "annotations", "photoplatform.io/revision"], PREVIOUS_SHA),
            (["metadata", "deletionTimestamp"], "now"),
            (["spec", "activeDeadlineSeconds"], 901), (["spec", "backoffLimit"], 2),
            (["status", "conditions"], [{"type": "Failed", "status": "True"}]),
            (["spec", "template", "metadata", "annotations", "photoplatform.io/revision"], PREVIOUS_SHA),
            (["spec", "template", "spec", "containers", 0, "image"], "repo:latest"),
            (["spec", "template", "spec", "containers", 0, "command"], ["/app/start.sh"]),
            (["spec", "template", "spec", "containers", 0, "args"], ["--start-services"]),
            (["spec", "template", "spec", "restartPolicy"], "Always"),
            (["spec", "template", "spec", "hostNetwork"], True),
            (["spec", "template", "spec", "initContainers"], [{"name": "injected", "image": "untrusted:init"}]),
            (["spec", "template", "spec", "serviceAccountName"], "other-identity")]
        for path, value in cases:
            with self.subTest(path=path):
                self.source = source_migration(self.control)
                node = self.source
                for key in path[:-1]:
                    node = node[key]
                node[path[-1]] = value
                with self.assertRaises(ValueError):
                    self.clone()

    def test_source_restrictive_context_rejects_root_privilege_and_additional_capabilities(self):
        changes = [("pod", {"runAsNonRoot": False}), ("pod", {"runAsUser": 0}),
            ("pod", {"seccompProfile": {"type": "Unconfined"}}),
            ("pod", {"sysctls": [{"name": "net.ipv4.ip_forward", "value": "1"}]}),
            ("container", {"allowPrivilegeEscalation": True}), ("container", {"privileged": True}),
            ("container", {"procMount": "Unmasked"}),
            ("container", {"capabilities": {"drop": ["ALL"], "add": ["NET_ADMIN"]}})]
        for target, change in changes:
            with self.subTest(target=target, change=change):
                self.source = source_migration(self.control)
                pod = self.source["spec"]["template"]["spec"]
                context = pod["securityContext"] if target == "pod" else pod["containers"][0]["securityContext"]
                context.update(change)
                with self.assertRaises(ValueError):
                    self.clone()

    def failed_objects(self):
        expected = self.clone()
        job = copy.deepcopy(expected)
        job["metadata"]["uid"] = "fault-job-uid"
        job["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
        pod = {"metadata": {"name": "fault-pod", "uid": "fault-pod-uid", "ownerReferences": [
            {"kind": "Job", "uid": "fault-job-uid", "controller": True}]},
            "spec": copy.deepcopy(expected["spec"]["template"]["spec"]),
            "status": {"containerStatuses": [{"name": "migrator", "imageID": "containerd://" + LEAF,
                "state": {"terminated": {"exitCode": 1, "reason": "Error"}}}]}}
        return expected, job, pod

    def evidence(self, expected, job, pod, logs=None):
        logs = logs if logs is not None else (
            "org.flywaydb.core.internal.exception.FlywaySqlException: Unable to obtain connection from database\n"
            "Unable to parse URL jdbc:postgresql://127.0.0.1:65536/eks_no_target\n"
            "at com.generatecloud.app.MigrationApplication.migrate(MigrationApplication.java:23)\n"
            "this arbitrary output must never appear in evidence")
        with patch.object(self.control, "json", side_effect=[job, {"items": [pod]}]), \
                patch.object(scenarios, "kubectl", return_value=logs):
            return scenarios.failed_migration_evidence(self.control, expected["metadata"]["name"],
                "fault-job-uid", expected, 30)

    def test_failure_requires_executed_immutable_image_nonzero_exit_and_all_flyway_markers(self):
        expected, job, pod = self.failed_objects()
        result = self.evidence(expected, job, pod)
        self.assertEqual(result["exitCode"], 1)
        self.assertEqual(result["imageID"], "containerd://" + LEAF)
        self.assertTrue(all(result["markers"].values()))
        self.assertNotIn("arbitrary output", json.dumps(result))
        for field, bad in (("exitCode", 0), ("exitCode", True), ("reason", "OOMKilled")):
            with self.subTest(field=field, bad=bad):
                changed = copy.deepcopy(pod)
                changed["status"]["containerStatuses"][0]["state"]["terminated"][field] = bad
                with self.assertRaises(ValueError):
                    self.evidence(expected, job, changed)
        for logs in ("generic startup failure", "org.flywaydb.core FlywaySqlException Unable to parse URL 127.0.0.1:65536",
                     "MigrationApplication.migrate Unable to parse URL 127.0.0.1:65536"):
            with self.subTest(logs=logs), self.assertRaises(ValueError):
                self.evidence(expected, job, pod, logs)

    def test_job_uid_success_condition_and_pod_owner_cannot_be_substituted(self):
        expected, job, pod = self.failed_objects()
        cases = [("job-uid", "different-job"), ("job-complete", True), ("owner", "different-job"),
                 ("image-id", "containerd://sha256:" + "9" * 64)]
        for case, value in cases:
            with self.subTest(case=case):
                changed_job, changed_pod = copy.deepcopy(job), copy.deepcopy(pod)
                if case == "job-uid":
                    changed_job["metadata"]["uid"] = value
                elif case == "job-complete":
                    changed_job["status"]["conditions"] = [{"type": "Complete", "status": "True"}]
                elif case == "owner":
                    changed_pod["metadata"]["ownerReferences"][0]["uid"] = value
                else:
                    changed_pod["status"]["containerStatuses"][0]["imageID"] = value
                with self.assertRaises(ValueError):
                    self.evidence(expected, changed_job, changed_pod)

    def test_admission_cannot_add_secret_credentials_sidecars_or_relax_security(self):
        expected, job, pod = self.failed_objects()
        changes = [("automountServiceAccountToken", True), ("hostNetwork", True),
            ("initContainers", [{"name": "injected", "image": "untrusted:init"}]),
            ("ephemeralContainers", [{"name": "debug", "image": "untrusted:debug"}]),
            ("imagePullSecrets", [{"name": "injected-secret"}]),
            ("securityContext", {"runAsNonRoot": False, "runAsUser": 0}),
            ("volumes", [{"name": "injected", "secret": {"secretName": "credentials"}}])]
        for key, value in changes:
            with self.subTest(key=key):
                changed = copy.deepcopy(pod)
                changed["spec"][key] = value
                with self.assertRaises(ValueError):
                    self.evidence(expected, job, changed)
        changed = copy.deepcopy(pod)
        changed["spec"]["containers"][0]["envFrom"] = [{"secretRef": {"name": "injected-secret"}}]
        with self.assertRaises(ValueError):
            self.evidence(expected, job, changed)

    def test_job_cleanup_uses_server_side_uid_precondition_and_disposable_boundary(self):
        for kind, path in (("job", "/apis/batch/v1/namespaces/photoplatform-dev/jobs/"),
                           ("serviceaccount", "/api/v1/namespaces/photoplatform-dev/serviceaccounts/")):
            with self.subTest(kind=kind), patch.dict(os.environ, GATES, clear=True), \
                    patch.object(scenarios, "kubectl") as mutate:
                scenarios.delete_probe_resource(self.control, "photoplatform-migration-failure-test", "fault-uid", kind)
            args, kwargs = mutate.call_args
            self.assertEqual(args[:4], (ARN, "photoplatform-dev", "delete", "--raw"))
            self.assertEqual(args[4], path + "photoplatform-migration-failure-test")
            self.assertEqual(kwargs["body"]["preconditions"], {"uid": "fault-uid"})
            self.assertEqual(kwargs["body"]["propagationPolicy"], "Foreground")

    def run_migration_scenario(self, response_loss=False, injected_identity=False):
        created = {}
        reads = self.control.json
        source = source_migration(self.control)
        mutations = []

        def kube(context, namespace, *args, body=None):
            self.control.assert_scope(context, namespace)
            mutations.append((args, copy.deepcopy(body)))
            if args[0] == "wait":
                return "deleted"
            self.assertEqual(args[:2], ("create", "-f"))
            item = copy.deepcopy(body)
            uid = "fresh-account-uid" if item["kind"] == "ServiceAccount" else "fault-job-uid"
            item["metadata"]["uid"] = uid
            created[item["kind"]] = item
            if item["kind"] == "ServiceAccount" and injected_identity:
                item["metadata"]["annotations"] = {"eks.amazonaws.com/role-arn": "injected-identity"}
            if item["kind"] == "Job" and response_loss:
                raise TimeoutError("Job was created but response was lost")
            return json.dumps(item)

        def read(*args):
            if args[:3] == ("get", "job", source["metadata"]["name"]):
                return copy.deepcopy(source)
            if args[:2] == ("get", "job") and "Job" in created:
                return copy.deepcopy(created["Job"])
            if args[:2] == ("get", "serviceaccount") and "ServiceAccount" in created:
                return copy.deepcopy(created["ServiceAccount"])
            return reads(*args)

        report, caught = {}, None
        with patch.dict(os.environ, GATES, clear=True), patch.object(self.control, "json", side_effect=read), \
                patch.object(scenarios, "HelmPin", return_value=Mock(storage={"uid": "helm-uid-7"})), \
                patch.object(scenarios, "kubectl", side_effect=kube), patch.object(scenarios, "delete_probe_resource") as cleanup, \
                patch.object(scenarios, "failed_migration_evidence", return_value={"executedFailure": True}), \
                patch.object(scenarios, "business_probe", return_value={"status": "PASS"}), patch.object(scenarios, "write_report"):
            try:
                scenarios.migration_scenario(self.control, report, "ignored.json", 30,
                    source["metadata"]["name"], source["metadata"]["uid"])
            except (TimeoutError, ValueError) as error:
                caught = error
        return report, mutations, cleanup.call_args_list, caught

    def test_migration_uses_fresh_no_token_identity_and_cleans_exact_job_and_identity(self):
        report, mutations, cleanup, error = self.run_migration_scenario()
        self.assertIsNone(error)
        creates = [body for args, body in mutations if args[0] == "create"]
        self.assertEqual([body["kind"] for body in creates], ["ServiceAccount", "Job"])
        account, job = creates
        self.assertIs(account["automountServiceAccountToken"], False)
        self.assertNotIn("annotations", account["metadata"])
        self.assertEqual(job["spec"]["template"]["spec"]["serviceAccountName"], account["metadata"]["name"])
        self.assertNotEqual(account["metadata"]["name"], "photoplatform-migrator")
        self.assertEqual([(call.args[2], call.args[3]) for call in cleanup],
                         [("fault-job-uid", "job"), ("fresh-account-uid", "serviceaccount")])
        self.assertEqual(report["restoreStatus"], "PASS")
        self.assertIs(report["restorePlan"]["applicationReleaseInvoked"], False)

    def test_response_lost_job_creation_discovers_and_uid_cleans_both_probe_resources(self):
        report, mutations, cleanup, error = self.run_migration_scenario(response_loss=True)
        self.assertIsInstance(error, TimeoutError)
        self.assertEqual([(call.args[2], call.args[3]) for call in cleanup],
                         [("fault-job-uid", "job"), ("fresh-account-uid", "serviceaccount")])
        self.assertEqual(report["restoreStatus"], "PASS")
        self.assertEqual(report["cleanup"]["job"]["status"], "PASS")
        self.assertEqual(report["cleanup"]["serviceAccount"]["status"], "PASS")

    def test_injected_aws_identity_blocks_job_creation_and_cleans_new_service_account(self):
        report, mutations, cleanup, error = self.run_migration_scenario(injected_identity=True)
        self.assertIsInstance(error, ValueError)
        self.assertEqual([body["kind"] for args, body in mutations if args[0] == "create"], ["ServiceAccount"])
        self.assertEqual([(call.args[2], call.args[3]) for call in cleanup], [("fresh-account-uid", "serviceaccount")])
        self.assertEqual(report["restoreStatus"], "PASS")


class HelmCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.control = FakeControl()
        self.current, self.previous = snapshot(7, MANIFEST), snapshot(5, PREVIOUS_MANIFEST)

    def test_application_sha_and_immutable_images_only_are_compatible(self):
        scenarios.compatible_snapshots(self.current, self.previous)
        for change in ("replicas", "container-command", "config", "secret-reference", "resource-set"):
            with self.subTest(change=change):
                previous = copy.deepcopy(self.previous)
                dep = previous["objects"]["Deployment/photoplatform-api"]
                if change == "replicas":
                    dep["spec"]["replicas"] = 3
                elif change == "container-command":
                    dep["spec"]["template"]["spec"]["containers"][0]["command"] = ["/new/command"]
                elif change == "config":
                    previous["objects"]["ConfigMap/photoplatform-config"]["data"]["DATABASE_HOST"] = "different.database"
                elif change == "secret-reference":
                    previous["values"]["secrets"]["api"]["arn"] = "different-secret-reference"
                else:
                    del previous["objects"]["ConfigMap/photoplatform-config"]
                with self.assertRaises(ValueError):
                    scenarios.compatible_snapshots(self.current, previous)

    def test_sha_like_configmap_data_is_behavior_not_ignored_metadata(self):
        self.current["objects"]["ConfigMap/photoplatform-config"]["data"]["photoplatform.io/revision"] = "behavior-current"
        self.previous["objects"]["ConfigMap/photoplatform-config"]["data"]["photoplatform.io/revision"] = "behavior-previous"
        with self.assertRaises(ValueError):
            scenarios.compatible_snapshots(self.current, self.previous)

    def invoke_snapshot(self, objects=None, values=None, hooks="", manifest=MANIFEST):
        pin = object.__new__(scenarios.HelmPin)
        pin.control = self.control
        objects = objects if objects is not None else self.current["objects"]
        values = values if values is not None else self.current["values"]

        def run(control, *args, **kwargs):
            if args[:2] == ("get", "manifest"):
                return render(objects)
            if args[:2] == ("get", "values"):
                return json.dumps(values)
            if args[:2] == ("get", "hooks"):
                return hooks
            raise AssertionError(args)

        with patch.object(scenarios, "helm", side_effect=run):
            return pin.snapshot(7, manifest)

    def test_revision_env_image_annotation_and_telemetry_suffix_must_match_exact_manifest(self):
        for change in ("reported-sha", "image-annotation", "telemetry-image"):
            with self.subTest(change=change):
                objects = copy.deepcopy(self.current["objects"])
                template = objects["Deployment/photoplatform-api"]["spec"]["template"]
                if change == "reported-sha":
                    template["spec"]["containers"][0]["env"][1]["value"] = PREVIOUS_SHA
                elif change == "image-annotation":
                    template["metadata"]["annotations"]["photoplatform.io/image-digest"] = "sha256:" + "9" * 64
                else:
                    template["spec"]["containers"][0]["env"][0]["value"] += ",extra=unreviewed"
                with self.assertRaises(ValueError):
                    self.invoke_snapshot(objects)

    def test_telemetry_prefix_and_unrelated_env_changes_are_behavior_changes(self):
        for change in ("telemetry-prefix", "ordinary-env"):
            with self.subTest(change=change):
                previous = copy.deepcopy(self.previous)
                env = previous["objects"]["Deployment/photoplatform-api"]["spec"]["template"]["spec"]["containers"][0]["env"]
                if change == "telemetry-prefix":
                    env[0]["value"] = env[0]["value"].replace("k8s.cluster.name=dev", "k8s.cluster.name=other")
                else:
                    env.append({"name": "APP_SEED_ENABLED", "value": "true"})
                with self.assertRaises(ValueError):
                    scenarios.compatible_snapshots(self.current, previous)

    def test_optional_ml_manifest_requires_both_pinned_deployments(self):
        manifest = copy.deepcopy(MANIFEST)
        manifest["images"]["encoder"] = IMAGE.replace("/api@", "/ml@")
        manifest["runnableDigests"]["encoder"] = [LEAF]
        with self.assertRaises(ValueError):
            self.invoke_snapshot(manifest=manifest)
        objects = copy.deepcopy(self.current["objects"])
        for component in ("encoder", "embedding-worker"):
            objects["Deployment/photoplatform-" + component] = deployment(component, manifest)
        result = self.invoke_snapshot(objects, manifest=manifest)
        self.assertEqual(set(result["components"]), {"api", "media-worker", "encoder", "embedding-worker"})
        del objects["Deployment/photoplatform-embedding-worker"]
        with self.assertRaises(ValueError):
            self.invoke_snapshot(objects, manifest=manifest)

    def test_snapshot_blocks_secret_job_cluster_scope_hooks_and_foreign_objects(self):
        self.invoke_snapshot()
        for kind, namespace, release_name in (("Secret", "photoplatform-dev", "photoplatform-extra"),
                ("Job", "photoplatform-dev", "photoplatform-extra"),
                ("ClusterRole", "photoplatform-dev", "photoplatform-extra"),
                ("ConfigMap", "other-namespace", "photoplatform-extra"),
                ("ConfigMap", "photoplatform-dev", "foreign-extra")):
            with self.subTest(kind=kind, namespace=namespace, name=release_name):
                objects = copy.deepcopy(self.current["objects"])
                objects[kind + "/" + release_name] = {"apiVersion": "v1", "kind": kind,
                    "metadata": {"name": release_name, "namespace": namespace, "labels": labels("api")}}
                with self.assertRaises(ValueError):
                    self.invoke_snapshot(objects)
        with self.assertRaises(ValueError):
            self.invoke_snapshot(hooks="apiVersion: batch/v1\nkind: Job\n")

    def test_snapshot_rejects_migration_enabled_prod_and_digest_template_mismatch(self):
        for key, value in (("environment", "prod"), ("runtimeMode", "kubernetes-local"),
                           ("migration", {"enabled": True})):
            with self.subTest(key=key):
                values = copy.deepcopy(self.current["values"])
                values[key] = value
                with self.assertRaises(ValueError):
                    self.invoke_snapshot(values=values)
        objects = copy.deepcopy(self.current["objects"])
        objects["Deployment/photoplatform-api"]["spec"]["template"]["spec"]["containers"][0]["image"] = "repo:latest"
        with self.assertRaises(ValueError):
            self.invoke_snapshot(objects)

    def test_snapshot_rejects_clusterrole_binding_even_in_guarded_namespace(self):
        objects = copy.deepcopy(self.current["objects"])
        objects["RoleBinding/photoplatform-binding"] = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
            "metadata": {"name": "photoplatform-binding", "namespace": "photoplatform-dev", "labels": labels("api")},
            "roleRef": {"kind": "ClusterRole", "name": "cluster-admin"}}
        with self.assertRaises(ValueError):
            self.invoke_snapshot(objects)

    def test_release_summary_contains_only_safe_hashes_and_resource_identities(self):
        result = scenarios.release_summary(self.current)
        self.assertEqual(result["revision"], 7)
        self.assertEqual(result["resources"], sorted(self.current["objects"]))
        self.assertNotIn("objects", result)
        self.assertNotIn("values", result)
        self.assertNotIn("fixed.database.internal", json.dumps(result))


class HelmRollbackRestorationTests(unittest.TestCase):
    def setUp(self):
        self.control = FakeControl()
        self.current, self.previous = snapshot(7, MANIFEST), snapshot(5, PREVIOUS_MANIFEST)

    def rehearse(self, failure_status=None, external_revision=None, tamper_interim=False):
        state = {"latest": {"revision": 7, "status": "deployed"}, "calls": []}
        pin = Mock(control=self.control, revision=7, storage=self.current["storage"])
        pin.latest.side_effect = lambda: copy.deepcopy(state["latest"])
        pin.history.return_value = [{"revision": 5, "status": "superseded"}, {"revision": 7, "status": "deployed"}]
        pin.storage_identity.side_effect = lambda revision: snapshot(revision, MANIFEST)["storage"]

        def get_snapshot(revision, manifest):
            if revision == 7:
                return copy.deepcopy(self.current)
            value = snapshot(revision, PREVIOUS_MANIFEST)
            if revision == 8 and tamper_interim:
                value["manifestSha256"] = "unrelated-content"
            return value

        pin.snapshot.side_effect = get_snapshot

        def rollback(control, revision, timeout):
            state["calls"].append(revision)
            if len(state["calls"]) == 1:
                state["latest"] = {"revision": external_revision or 8, "status": failure_status or "deployed"}
                if failure_status or external_revision or tamper_interim:
                    raise TimeoutError("sensitive stderr must remain absent from evidence")
            else:
                state["latest"] = {"revision": 9, "status": "deployed"}

        def helm_get(control, *args, **kwargs):
            revision = int(args[args.index("--revision") + 1])
            selected = self.previous if revision == 8 else self.current
            if args[:2] == ("get", "manifest"):
                return render(selected["objects"])
            if args[:2] == ("get", "values"):
                return json.dumps(selected["values"])
            if args[:2] == ("get", "hooks"):
                return ""
            raise AssertionError(args)

        report = {}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "previous.json"
            path.write_text(json.dumps(PREVIOUS_MANIFEST))
            with patch.dict(os.environ, GATES, clear=True), patch.object(scenarios, "load_manifest", return_value=PREVIOUS_MANIFEST), \
                    patch.object(scenarios, "HelmPin", return_value=pin), patch.object(scenarios, "helm_rollback", side_effect=rollback), \
                    patch.object(scenarios, "helm", side_effect=helm_get), patch.object(scenarios, "verified_runtime", return_value={"ready": True}), \
                    patch.object(scenarios, "business_probe", return_value={"status": "PASS"}), patch.object(scenarios, "write_report"):
                try:
                    scenarios.rollback_scenario(Mock(), self.control, report, "ignored.json", 30, 5, str(path))
                except TimeoutError:
                    pass
        return state, report

    def test_successful_previous_release_always_restores_explicit_original_revision(self):
        state, report = self.rehearse()
        self.assertEqual(state["calls"], [5, 7])
        self.assertEqual(report["scenarioStatus"], "PASS")
        self.assertEqual(report["restoreStatus"], "PASS")
        self.assertEqual(report["restoredStorage"]["revision"], 9)
        self.assertEqual(report["restorePlan"]["restoreExactRevision"], 7)
        self.assertIs(report["restorePlan"]["schemaRollback"], False)

    def test_failed_or_partial_rollback_still_restores_original_after_response_loss(self):
        for status in ("failed", "pending-rollback", "deployed"):
            with self.subTest(status=status):
                state, report = self.rehearse(failure_status=status)
                self.assertEqual(state["calls"], [5, 7])
                self.assertEqual(report["restoreStatus"], "PASS")
                self.assertNotIn("sensitive stderr", json.dumps(report))

    def test_restoration_refuses_unrelated_latest_revision_pending_operation_or_content(self):
        for kwargs in ({"external_revision": 9}, {"failure_status": "pending-upgrade"}, {"tamper_interim": True}):
            with self.subTest(kwargs=kwargs):
                state, report = self.rehearse(**kwargs)
                self.assertEqual(state["calls"], [5])
                self.assertEqual(report["restoreStatus"], "FAIL")
                self.assertEqual(report["restoreErrorType"], "ValueError")

    def test_direct_helm_rollback_cannot_mutate_without_explicit_gate(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(scenarios, "helm") as mutate:
            with self.assertRaises(ValueError):
                scenarios.helm_rollback(self.control, 5, 30)
        mutate.assert_not_called()

    def test_rollback_command_has_explicit_numeric_revision_scope_and_no_hooks(self):
        with patch.dict(os.environ, GATES, clear=True), patch.object(scenarios, "helm") as mutate:
            scenarios.helm_rollback(self.control, 5, 30)
        args, kwargs = mutate.call_args
        self.assertEqual(args[1:5], ("rollback", "photoplatform", "5", "--no-hooks"))
        self.assertIn("--wait", args)
        self.assertEqual(kwargs["timeout"], 60)


if __name__ == "__main__":
    unittest.main()
