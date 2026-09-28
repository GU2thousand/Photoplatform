"""Gracefully evacuate one exact, exclusively application-owned disposable EKS node.

Requires eks_guard, ALLOW_EKS_NODE_DRAIN=1, and the operator's exclusive
EKS_RELEASE_SCENARIO_LOCK_ID maintenance window. This assertion is not a distributed
lock: independently scheduled deployments/node maintenance MUST be excluded.
The node must have photoplatform.io/drain-allowed=true, belong to an ACTIVE tagged
managed node group, and its actual EC2 instance must carry disposable dev and
cluster/node-group/ASG ownership tags. All namespaces are inventoried before writes.
Only the guarded release's API/media/optional ML Deployment Pods are evicted; exact
kube-system DaemonSet Pods are left alone. A telemetry/queue/controller/shared node
is deliberately refused. No kubectl drain, force deletion, SG edits or resizing.

Node patch permission is a separate maintenance grant restricted by resourceNames
to --node-name. The release namespace needs policy/v1 Pod eviction permission;
read-only all-namespace inventory and node-group/EC2/ASG inspection are also needed.
Restore plan is persisted before cordoning; finally restores the original node
unschedulable field only with exact UID/ownership-marker/current-field preconditions.
PASS proves observed graceful replacement on other Ready nodes and post-drain
private upload smoke. It proves neither zero downtime, SIGKILL, stale-claim fencing
nor the full P6 matrix. Optional durable task selection must supply all four IDs.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qs, urlparse, urlunparse
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.cloud_common import CloudAPI, fixture, required, secure_origin, validate_s3_url, write_report
from scripts.eks_common import COMPONENTS, eks_guard, kubectl, safe_name, valid_tags, verify_public_pod
from scripts.eks_failure import running_job


ALLOWED_COMPONENTS = frozenset(("api", "media-worker", "encoder", "embedding-worker"))
MARKER = "photoplatform.io/node-drain-run"


def mutation_guard():
    if os.getenv("ALLOW_EKS_NODE_DRAIN") != "1":
        raise ValueError("Set ALLOW_EKS_NODE_DRAIN=1 for disposable dev node maintenance")
    lock = os.getenv("EKS_RELEASE_SCENARIO_LOCK_ID", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", lock):
        raise ValueError("EKS_RELEASE_SCENARIO_LOCK_ID must identify the exclusive operator window")
    return lock


def spec_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Deadline:
    def __init__(self, seconds):
        if not 1 <= seconds <= 900:
            raise ValueError("Node drain timeout must be 1..900 seconds")
        self.ends = time.monotonic() + seconds

    def remaining(self, maximum=None):
        remaining = self.ends - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Node drain observation deadline expired")
        return min(remaining, maximum) if maximum is not None else remaining


def node_identity(aws, control, node):
    """Bind Kubernetes providerID to actual account/region/managed-group membership."""
    metadata, spec = node["metadata"], node["spec"]
    labels = metadata.get("labels", {})
    if (metadata.get("deletionTimestamp") or labels.get("photoplatform.io/drain-allowed") != "true" or
            not any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in node.get("status", {}).get("conditions", []))):
        raise ValueError("Exact node must be Ready and explicitly opted into disposable drain")
    region, account = required("AWS_REGION"), required("EXPECTED_AWS_ACCOUNT_ID")
    provider = re.fullmatch(r"aws:///([a-z0-9-]+)/([i]-[a-f0-9]{8,17})", spec.get("providerID", ""))
    if not provider or labels.get("topology.kubernetes.io/region") != region:
        raise ValueError("Node must expose an exact EC2 providerID in the guarded AWS region")
    zone, instance_id = provider.groups()
    if not zone.startswith(region) or labels.get("topology.kubernetes.io/zone") != zone:
        raise ValueError("Node Availability Zone must match its actual providerID")
    group_name = safe_name(labels.get("eks.amazonaws.com/nodegroup", ""))
    group = aws.client("eks").describe_nodegroup(clusterName=control.cluster, nodegroupName=group_name)["nodegroup"]
    expected_arn = control.arn.rsplit(":cluster/", 1)[0] + ":nodegroup/" + control.cluster + "/" + group_name + "/"
    if (group.get("clusterName") != control.cluster or group.get("nodegroupName") != group_name or
            not group.get("nodegroupArn", "").startswith(expected_arn) or group.get("status") != "ACTIVE" or
            not valid_tags(group.get("tags", {})) or group.get("health", {}).get("issues") or
            group.get("labels", {}).get("photoplatform.io/workload") not in {"application", "ml"}):
        raise ValueError("Node group must be ACTIVE, healthy and tagged disposable photoplatform dev")
    response = aws.client("ec2").describe_instances(InstanceIds=[instance_id])
    reservations = response.get("Reservations", [])
    instances = [(reservation, instance) for reservation in reservations for instance in reservation.get("Instances", [])]
    if len(instances) != 1:
        raise ValueError("Exactly one actual EC2 instance must match providerID")
    reservation, instance = instances[0]
    tags = {row["Key"]: row["Value"] for row in instance.get("Tags", [])}
    asg_names = [row.get("name") for row in group.get("resources", {}).get("autoScalingGroups", [])]
    asg = tags.get("aws:autoscaling:groupName")
    if (reservation.get("OwnerId") != account or instance.get("InstanceId") != instance_id or
            instance.get("State", {}).get("Name") != "running" or instance.get("Placement", {}).get("AvailabilityZone") != zone or
            instance.get("SubnetId") not in group.get("subnets", []) or not valid_tags(tags) or
            tags.get("kubernetes.io/cluster/" + control.cluster) != "owned" or
            tags.get("eks:cluster-name") != control.cluster or tags.get("eks:nodegroup-name") != group_name or
            not asg or asg not in asg_names):
        raise ValueError("Actual EC2 account/region/disposable tags and managed group ownership must match")
    groups = aws.client("autoscaling").describe_auto_scaling_groups(AutoScalingGroupNames=[asg]).get("AutoScalingGroups", [])
    members = [member for item in groups if item.get("AutoScalingGroupName") == asg for member in item.get("Instances", [])
               if member.get("InstanceId") == instance_id]
    if len(groups) != 1 or len(members) != 1 or members[0].get("LifecycleState") != "InService":
        raise ValueError("Actual EC2 instance must be InService in the EKS managed node group's ASG")
    return {"providerId": spec["providerID"], "instanceId": instance_id, "accountId": account,
            "region": region, "availabilityZone": zone, "nodeGroupName": group_name,
            "nodeGroupArn": group["nodegroupArn"], "autoScalingGroupName": asg,
            "ownershipTags": {key: tags[key] for key in ("Project", "Environment", "DisposableEnvironment",
                "eks:cluster-name", "eks:nodegroup-name", "kubernetes.io/cluster/" + control.cluster)}}


class NodeDrain:
    def __init__(self, aws, control, name, uid):
        self.window = mutation_guard()
        self.aws, self.control, self.name, self.uid = aws, control, safe_name(name), str(uuid.UUID(uid))
        self.token = uuid.uuid4().hex
        self.attempted = False
        self.evicted = []
        self.original = self.node()
        if self.original["spec"].get("unschedulable", False) is not False:
            raise ValueError("Choose an initially schedulable node for a meaningful controlled drain")
        if MARKER in self.original["metadata"].get("annotations", {}):
            raise ValueError("Node already has a maintenance marker")
        self.provider = node_identity(aws, control, self.original)
        self.baselines = {component: self.pin(component) for component in ("api", "media-worker")}
        self.targets, self.daemonsets = self.inventory(initial=True)
        if not self.targets:
            raise ValueError("Selected node must host at least one guarded application Pod")
        self.no_hpa()

    def boundary(self):
        current = self.control.json("get", "namespace", self.control.namespace, "-o", "json")
        labels = current["metadata"].get("labels", {})
        if (current["metadata"]["uid"] != self.control.namespace_uid or current["metadata"].get("deletionTimestamp") or
                labels.get("app.kubernetes.io/part-of") != "photoplatform" or
                labels.get("photoplatform.io/environment") != "dev" or labels.get("photoplatform.io/disposable") != "true"):
            raise ValueError("Disposable namespace identity changed")

    def node(self):
        node = self.control.json("get", "node", self.name, "-o", "json")
        if node["metadata"]["uid"] != self.uid or node["metadata"].get("deletionTimestamp"):
            raise ValueError("Selected node UID changed or the node is terminating")
        return node

    def pin(self, component):
        if component not in ALLOWED_COMPONENTS or COMPONENTS[component] not in self.control.manifest["images"]:
            raise ValueError("Selected node contains an unpinned application component")
        deployment = self.control.deployment(component)
        state = self.control.state(component)
        if (state["replicas"] < 1 or state["readyReplicas"] != state["replicas"] or
                state["updatedReplicas"] != state["replicas"] or state["observedGeneration"] < state["generation"] or
                len(state["pods"]) != state["replicas"] or
                not all(p["ready"] and not p["terminating"] and p["phase"] == "Running" for p in state["pods"])):
            raise ValueError("Drain requires every selected Deployment to be settled and Ready")
        return {"deployment": state["deployment"], "uid": state["uid"], "replicas": state["replicas"],
                "specSha256": spec_hash(deployment["spec"]), "pods": state["pods"]}

    def unchanged(self):
        self.boundary()
        self.no_hpa()
        for component, pinned in self.baselines.items():
            deployment = self.control.deployment(component)
            if deployment["metadata"]["uid"] != pinned["uid"] or spec_hash(deployment["spec"]) != pinned["specSha256"]:
                raise ValueError("Application Deployment was replaced or concurrently edited")

    def no_hpa(self):
        names = {pin["deployment"] for pin in self.baselines.values()}
        hpas = self.control.json("get", "hpa", "-o", "json")["items"]
        if any(row.get("spec", {}).get("scaleTargetRef", {}).get("kind") == "Deployment" and
               row["spec"]["scaleTargetRef"].get("name") in names for row in hpas):
            raise ValueError("Controlled node drain refuses matching HPAs; use fixed reviewed dev replicas")

    def inventory(self, initial=False):
        """The only cross-namespace operation is read-only; every eviction stays scoped."""
        self.boundary()
        pods = self.control.json("get", "pods", "--all-namespaces", "--field-selector", "spec.nodeName=" + self.name,
                                 "-o", "json")["items"]
        targets, daemonsets = [], []
        for pod in pods:
            metadata, spec = pod["metadata"], pod["spec"]
            namespace = metadata.get("namespace")
            if spec.get("nodeName") != self.name or metadata.get("annotations", {}).get("kubernetes.io/config.mirror") is not None:
                raise ValueError("Node inventory contains a static/mirror Pod or inconsistent node assignment")
            owners = [owner for owner in metadata.get("ownerReferences", []) if owner.get("controller") is True]
            if len(owners) != 1:
                raise ValueError("Node inventory contains an unmanaged or ambiguously owned Pod")
            owner = owners[0]
            if namespace == "kube-system" and owner.get("kind") == "DaemonSet":
                daemonset = self.control.json("get", "daemonset", safe_name(owner.get("name", "")),
                                              "--namespace", "kube-system", "-o", "json")
                if (daemonset["metadata"].get("uid") != owner.get("uid") or
                        daemonset["metadata"].get("deletionTimestamp") or
                        daemonset["metadata"].get("namespace") != "kube-system"):
                    raise ValueError("System DaemonSet ownership must be verified by exact UID")
                daemonsets.append({"namespace": namespace, "name": metadata["name"], "uid": metadata["uid"],
                                   "daemonSetUid": owner["uid"], "action": "untouched"})
                continue
            labels = metadata.get("labels", {})
            component = labels.get("app.kubernetes.io/component")
            if (namespace != self.control.namespace or labels.get("app.kubernetes.io/name") != "photoplatform" or
                    labels.get("app.kubernetes.io/instance") != self.control.release or component not in ALLOWED_COMPONENTS or
                    owner.get("kind") != "ReplicaSet"):
                raise ValueError("Drain refuses any shared, nonapplication or out-of-release node workload")
            if component not in self.baselines:
                if not initial:
                    raise ValueError("Unexpected application component appeared during maintenance")
                self.baselines[component] = self.pin(component)
            summaries = self.control.pods(component)  # Exact Deployment -> ReplicaSet UID, SHA and actual imageID checks.
            matches = [p for p in summaries if p["name"] == metadata["name"] and p["uid"] == metadata["uid"]]
            draining_terminal = (not initial and metadata["uid"] in self.evicted and
                                 matches and matches[0]["phase"] in {"Succeeded", "Failed"})
            if len(matches) != 1 or (matches[0]["phase"] != "Running" and not draining_terminal):
                raise ValueError("Node Pod must be an exact running member of the pinned Deployment")
            if initial and (not matches[0]["ready"] or matches[0]["terminating"]):
                raise ValueError("Node drain baseline requires all application Pods Ready and nonterminating")
            target = {"namespace": namespace, "name": safe_name(metadata["name"]), "uid": metadata["uid"],
                      "resourceVersion": metadata["resourceVersion"], "component": component, "nodeName": self.name}
            uuid.UUID(target["uid"])
            if not initial and target["uid"] not in {p["uid"] for p in self.targets}:
                raise ValueError("New application Pod appeared on the selected node during maintenance")
            targets.append(target)
        return sorted(targets, key=lambda p: (p["component"] != "api", p["component"], p["name"])), daemonsets

    def plan(self):
        return {"nodeName": self.name, "nodeUid": self.uid, "provider": self.provider,
            "originalUnschedulablePresent": "unschedulable" in self.original["spec"],
            "originalUnschedulable": self.original["spec"].get("unschedulable", False),
            "namespace": self.control.namespace, "namespaceUid": self.control.namespace_uid,
            "maintenanceWindow": self.window, "operatorWindowIsDistributedLock": False,
            "marker": MARKER, "markerToken": self.token, "targets": self.targets,
            "systemDaemonSetPods": self.daemonsets, "deployments": self.baselines,
            "restoration": "Restore exact node UID, own marker and expected true unschedulable field with current resourceVersion tests",
            "permissions": "Exact resourceName node patch in separate maintenance grant; namespaced pods/eviction create; read-only cluster inventory"}

    def node_patch(self, current, operations):
        metadata = current["metadata"]
        body = [{"op": "test", "path": "/metadata/uid", "value": self.uid},
                {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]}, *operations]
        return json.loads(kubectl(self.control.arn, self.control.namespace, "patch", "node", self.name,
                                 "--type=json", "-p", json.dumps(body), "-o", "json"))

    def cordon(self, deadline=None):
        mutation_guard()
        self.unchanged()
        current = self.node()
        if (current["metadata"]["resourceVersion"] != self.original["metadata"]["resourceVersion"] or
                current["spec"] != self.original["spec"] or MARKER in current["metadata"].get("annotations", {}) or
                node_identity(self.aws, self.control, current) != self.provider):
            raise ValueError("Selected node changed before cordoning")
        current_targets, _ = self.inventory()
        if current_targets != self.targets:
            raise ValueError("Node Pod inventory changed before cordoning")
        operations = [{"op": "test", "path": "/spec", "value": self.original["spec"]}]
        if "annotations" in current["metadata"]:
            operations.append({"op": "add", "path": "/metadata/annotations/photoplatform.io~1node-drain-run", "value": self.token})
        else:
            operations.append({"op": "add", "path": "/metadata/annotations", "value": {MARKER: self.token}})
        operations.append({"op": "add", "path": "/spec/unschedulable", "value": True})
        if deadline:
            deadline.remaining()  # Recheck after blocking guards, immediately before dispatch.
        self.attempted = True  # Transport failure after submission may still have applied the patch.
        patched = self.node_patch(current, operations)
        if (patched["metadata"]["uid"] != self.uid or patched["spec"].get("unschedulable") is not True or
                patched["metadata"].get("annotations", {}).get(MARKER) != self.token):
            raise ValueError("API server did not return the exact owned cordoned node")
        return {"nodeName": self.name, "nodeUid": self.uid, "resourceVersion": patched["metadata"]["resourceVersion"],
                "unschedulable": True, "markerToken": self.token}

    def evict(self, target, before_evict=None, deadline=None):
        mutation_guard()
        self.unchanged()
        current = self.node()
        if (current["spec"].get("unschedulable") is not True or
                current["metadata"].get("annotations", {}).get(MARKER) != self.token or
                node_identity(self.aws, self.control, current) != self.provider):
            raise ValueError("Node is not the exact owned disposable cordoned node")
        if target not in self.targets or target["uid"] in self.evicted:
            raise ValueError("Only one eviction per exact planned Pod is allowed")
        current_targets, _ = self.inventory()
        matches = [pod for pod in current_targets if pod["uid"] == target["uid"]]
        if matches != [target]:
            raise ValueError("Planned Pod UID/resourceVersion/node changed before eviction")
        raw = self.control.json("get", "pod", target["name"], "-o", "json")
        if (raw["metadata"]["uid"] != target["uid"] or raw["metadata"]["resourceVersion"] != target["resourceVersion"] or
                raw["spec"].get("nodeName") != self.name or raw["metadata"].get("deletionTimestamp")):
            raise ValueError("Exact Pod changed or is already terminating")
        if before_evict:
            before_evict(target)  # Optional durable job is rechecked immediately before its worker eviction.
        body = {"apiVersion": "policy/v1", "kind": "Eviction", "metadata": {"name": target["name"], "namespace": self.control.namespace},
                "deleteOptions": {"preconditions": {"uid": target["uid"], "resourceVersion": target["resourceVersion"]}}}
        # PDB rejection or ambiguous transport failure aborts and restores the node.
        # Never fall back to delete/force/SIGKILL or enumerate unguarded workloads.
        if deadline:
            deadline.remaining()  # Read-only guards/job correlation may have exhausted the deadline.
        kubectl(self.control.arn, self.control.namespace, "create", "--raw",
                f"/api/v1/namespaces/{self.control.namespace}/pods/{target['name']}/eviction", "-f", "-", body=body)
        self.evicted.append(target["uid"])
        return {"podName": target["name"], "podUid": target["uid"], "preconditions": body["deleteOptions"]["preconditions"],
                "semantics": "policy/v1 Eviction, PDB respected, original Pod grace period; no force or SIGKILL"}

    def recovered(self):
        self.unchanged()
        current = self.node()
        if current["spec"].get("unschedulable") is not True or current["metadata"].get("annotations", {}).get(MARKER) != self.token:
            raise ValueError("Selected node cordon/maintenance ownership changed")
        self.inventory()  # Refuse a shared/new workload even during recovery observation.
        result = {}
        for component, before in self.baselines.items():
            state = self.control.state(component)
            if (state["uid"] != before["uid"] or state["replicas"] != before["replicas"] or
                    state["observedGeneration"] < state["generation"] or state["updatedReplicas"] != state["replicas"] or
                    state["readyReplicas"] != state["replicas"] or len(state["pods"]) != state["replicas"] or
                    not all(p["ready"] and not p["terminating"] and p["phase"] == "Running" for p in state["pods"])):
                return None
            old_uids = {p["uid"] for p in before["pods"]}
            evicted = {p["uid"] for p in self.targets if p["component"] == component}
            now = {p["uid"] for p in state["pods"]}
            if evicted & now:
                return None
            if not old_uids - evicted <= now or len(now - old_uids) != len(evicted):
                raise ValueError("Unselected Pod replacement occurred during controlled node drain")
            replacements = []
            for pod in state["pods"]:
                if pod["uid"] not in now - old_uids:
                    continue
                raw = self.control.json("get", "pod", pod["name"], "-o", "json")
                other_node = raw["spec"].get("nodeName")
                if raw["metadata"]["uid"] != pod["uid"] or not other_node or other_node == self.name:
                    raise ValueError("Exact replacement Pod must be scheduled on a different node")
                other = self.control.json("get", "node", safe_name(other_node), "-o", "json")
                if (other["metadata"].get("deletionTimestamp") or not any(c.get("type") == "Ready" and c.get("status") == "True"
                        for c in other.get("status", {}).get("conditions", []))):
                    return None
                replacements.append(dict(pod, nodeName=other_node, expectedSha=self.control.sha))
            result[component] = dict(state, replacementsOnDifferentNodes=replacements)
        return result

    def wait_recovered(self, deadline):
        while True:
            deadline.remaining()
            result = self.recovered()
            if result is not None:
                return result
            time.sleep(deadline.remaining(3))

    def restore(self):
        if not self.attempted:
            return {"status": "NOT_NEEDED"}
        try:
            self.boundary()
            current = self.node()
            if current["spec"].get("providerID") != self.original["spec"].get("providerID"):
                raise ValueError("Node provider instance changed; refuse restoration")
            annotations = current["metadata"].get("annotations", {})
            if MARKER not in annotations and ("unschedulable" in current["spec"]) == ("unschedulable" in self.original["spec"]) and \
                    current["spec"].get("unschedulable", False) == self.original["spec"].get("unschedulable", False):
                return {"status": "PASS", "operation": "already original; no write"}
            if annotations.get(MARKER) != self.token or current["spec"].get("unschedulable") is not True:
                raise ValueError("Own node marker or expected cordon field changed; refuse overwrite")
            operations = [{"op": "test", "path": "/metadata/annotations/photoplatform.io~1node-drain-run", "value": self.token},
                          {"op": "test", "path": "/spec/unschedulable", "value": True}]
            operations.append({"op": "replace", "path": "/spec/unschedulable", "value": self.original["spec"]["unschedulable"]}
                              if "unschedulable" in self.original["spec"] else {"op": "remove", "path": "/spec/unschedulable"})
            if "annotations" not in self.original["metadata"] and annotations == {MARKER: self.token}:
                operations.extend([{"op": "test", "path": "/metadata/annotations", "value": {MARKER: self.token}},
                                   {"op": "remove", "path": "/metadata/annotations"}])
            else:
                operations.append({"op": "remove", "path": "/metadata/annotations/photoplatform.io~1node-drain-run"})
            patched = self.node_patch(current, operations)
            if (patched["metadata"]["uid"] != self.uid or MARKER in patched["metadata"].get("annotations", {}) or
                    ("unschedulable" in patched["spec"]) != ("unschedulable" in self.original["spec"]) or
                    patched["spec"].get("unschedulable", False) != self.original["spec"].get("unschedulable", False)):
                raise ValueError("Node restoration response differs from the original field")
            return {"status": "PASS", "nodeName": self.name, "nodeUid": self.uid,
                    "restoredUnschedulable": self.original["spec"].get("unschedulable", False)}
        except Exception as error:
            return {"status": "FAIL", "errorType": type(error).__name__, "manualPlanRequired": True}


class BoundedAPI(CloudAPI):
    def __init__(self, deadline):
        super().__init__()
        import requests
        self.deadline = deadline
        self.session = requests.Session()
        self.session.trust_env = False

    def request(self, method, path, token=None, anonymous=False, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        if not anonymous:
            headers["Authorization"] = "Bearer " + (token or self.token)
        return self.session.request(method, self.base + path, headers=headers,
            timeout=self.deadline.remaining(15), allow_redirects=False, **kwargs)

    def close(self):
        self.session.close()


def private_smoke(api, deadline):
    """Actual upload/Ready/authorization/S3/CloudFront responses; no URLs in report."""
    other = required("TEST_OTHER_TOKEN")
    if other == api.token:
        raise ValueError("Private smoke requires a distinct actual unrelated user token")
    profiles = [api.expect(api.request("GET", "/api/auth/me", **kwargs)) for kwargs in ({}, {"token": other})]
    if (any(type(profile.get("id")) is not int or profile["id"] <= 0 or profile.get("role") != "USER" for profile in profiles) or
            profiles[0]["id"] == profiles[1]["id"]):
        raise ValueError("Owner and unrelated fixture must be two actual distinct USER accounts")
    upload = None
    result = {"status": "FAIL", "cleanupStatus": "NOT_NEEDED"}
    try:
        payload = fixture()
        upload = api.create(payload, visibility="PRIVATE")
        result.update(uploadId=upload["uploadId"], mediaId=upload["mediaId"])
        validate_s3_url(upload["uploadUrl"])
        response = api.session.put(upload["uploadUrl"], headers=upload["headers"], data=payload,
                                   timeout=deadline.remaining(15), allow_redirects=False)
        if response.status_code != 200:
            raise AssertionError("Actual S3 fixture PUT failed")
        api.complete(upload)
        api.wait(upload, timeout=deadline.remaining())
        result.update(uploadId=upload["uploadId"], mediaId=upload["mediaId"], actualUploadReady=True)
        path = f"/api/files/{upload['mediaId']}/url"
        codes = {"unrelatedApiStatus": api.request("GET", path, token=other).status_code,
                 "anonymousApiStatus": api.request("GET", path, anonymous=True).status_code}
        if any(code != 403 for code in codes.values()):
            raise AssertionError("Private delivery authorization did not deny unrelated/anonymous callers")
        raw = urlunparse(urlparse(upload["uploadUrl"])._replace(query=""))
        codes["unsignedS3Status"] = api.session.get(raw, timeout=deadline.remaining(15), allow_redirects=False).status_code
        signed = api.expect(api.request("GET", path))["url"]
        parsed = urlparse(signed)
        cdn = urlparse(secure_origin("https://" + required("CLOUDFRONT_DOMAIN").removeprefix("https://").rstrip("/"))).hostname
        if parsed.scheme != "https" or parsed.hostname != cdn or parsed.username or parsed.password or not {"Signature", "Key-Pair-Id", "Expires"} <= set(parse_qs(parsed.query)):
            raise ValueError("Delivery must be a signed URL at the guarded CloudFront domain")
        codes["signedCloudFrontStatus"] = api.session.get(signed, timeout=deadline.remaining(15), allow_redirects=False).status_code
        unsigned = urlunparse(parsed._replace(query=""))
        codes["unsignedCloudFrontStatus"] = api.session.get(unsigned, timeout=deadline.remaining(15), allow_redirects=False).status_code
        if codes["unsignedS3Status"] != 403 or codes["signedCloudFrontStatus"] != 200 or codes["unsignedCloudFrontStatus"] != 403:
            raise AssertionError("Private raw S3/signed CloudFront/unsigned CloudFront contract failed")
        result.update(status="PASS", responses=codes)
    except Exception as error:
        result["errorType"] = type(error).__name__
    finally:
        if upload:
            original_deadline = api.deadline
            try:
                # Fixture deletion is cleanup, not a new maintenance observation.
                # Do not abandon owned fixtures because the observation expired.
                api.deadline = Deadline(300)
                api.cleanup(upload)
                api.wait(upload, timeout=api.deadline.remaining(), target="DELETED")
                result["cleanupStatus"] = "PASS"
            except Exception as error:
                result.update(status="FAIL", cleanupStatus="FAIL", cleanupErrorType=type(error).__name__)
            finally:
                api.deadline = original_deadline
    return result


def finished_job(media_id, job_id):
    """Exact durable job identity/cardinality; no claim token or DB URL serialized."""
    import psycopg
    with psycopg.connect(required("BENCHMARK_DATABASE_URL"), connect_timeout=10) as db:
        db.execute("SET TRANSACTION READ ONLY")
        db.execute("SET LOCAL statement_timeout='15s'")
        rows = db.execute("""SELECT id,media_id,status,worker_id,attempt,lease_until IS NULL,finished_at IS NOT NULL,
            (SELECT count(*) FROM media_processing_jobs WHERE media_id=%s AND job_type='MEDIA_PROCESS')
            FROM media_processing_jobs WHERE id=%s AND media_id=%s AND job_type='MEDIA_PROCESS'""",
            (media_id, uuid.UUID(job_id), media_id)).fetchall()
    if len(rows) != 1 or rows[0][2] != "DONE" or not rows[0][5] or not rows[0][6] or rows[0][7] != 1:
        raise AssertionError("Exactly the selected durable media job must finish DONE with one processing job and no active lease")
    return dict(zip(("jobId", "mediaId", "status", "workerId", "attempt", "noActiveLease", "finished", "mediaJobCount"), rows[0]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-name", required=True)
    parser.add_argument("--node-uid", required=True)
    for name in ("upload-id", "job-id", "pod-name", "pod-uid"):
        parser.add_argument("--" + name)
    parser.add_argument("--minimum-job-age-seconds", type=int, default=0,
                        help="Optional actual task age requirement; >300 measures a task beyond the original five-minute lease")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--output", default="benchmarks/results/eks-node-drain.json")
    args = parser.parse_args()
    selected = [args.upload_id, args.job_id, args.pod_name, args.pod_uid]
    if not 1 <= args.timeout <= 900 or args.minimum_job_age_seconds < 0 or (any(selected) and not all(selected)) or (args.minimum_job_age_seconds and not all(selected)):
        parser.error("timeout must be 1..900; durable task requires all four IDs; job age must be nonnegative and used with a task")
    report = {"kind": "real-aws-eks-controlled-node-drain", "status": "FAIL", "matrixStatus": "INCOMPLETE",
        "durableTaskStatus": "NOT_RUN", "evictions": [], "limitations": [
            "Operator maintenance-window identifier is an assertion, not distributed exclusion",
            "Graceful policy/v1 eviction only; observed post-drain recovery does not establish zero downtime",
            "SIGKILL, stale-claim fencing, after-S3 crash and complete P6 matrix are not certified",
            "Long-running task behavior is measured only when an exact qualifying active task is selected"]}
    drain = api = None
    try:
        mutation_guard()
        deadline = Deadline(args.timeout)
        aws, control, report["provenance"] = eks_guard()
        api = BoundedAPI(deadline)
        drain = NodeDrain(aws, control, args.node_name, args.node_uid)
        upload = None
        if all(selected):
            report["durableTaskStatus"] = "FAIL"
            uuid.UUID(args.upload_id)
            uuid.UUID(args.job_id)
            uuid.UUID(args.pod_uid)
            safe_name(args.pod_name)
            matches = [pod for pod in drain.targets if pod["component"] == "media-worker" and
                       pod["name"] == args.pod_name and pod["uid"] == args.pod_uid]
            if len(matches) != 1:
                raise ValueError("Durable task Pod must be an exact media worker on the selected node")
            upload = {"uploadId": args.upload_id}
            initial = api.state(upload)
            if initial.get("status") != "PROCESSING" or type(initial.get("mediaId")) is not int or initial["mediaId"] <= 0:
                raise ValueError("Selected upload must be a real PROCESSING media upload")
            upload["mediaId"] = initial["mediaId"]
            report["jobBefore"] = running_job(initial["mediaId"], args.job_id, args.pod_name)
            age = report["jobBefore"].get("currentAttemptAgeSeconds")
            if age is None or age < args.minimum_job_age_seconds:
                raise ValueError("Selected live claimed job must meet the requested measured attempt age")
            report["longerThanOriginalFiveMinuteLease"] = age > 300
        # Complete and persist a reviewable restore plan before the first node write.
        report["restorePlan"] = drain.plan()
        write_report(args.output, report)
        deadline.remaining()
        report["cordon"] = drain.cordon(deadline)
        write_report(args.output, report)
        def before_evict(target):
            if upload and target["uid"] == args.pod_uid:
                if api.state(upload)["status"] != "PROCESSING":
                    raise ValueError("Selected upload ceased processing before its worker eviction")
                job = running_job(upload["mediaId"], args.job_id, args.pod_name)
                if (job.get("attempt") != report["jobBefore"]["attempt"] or
                        job.get("currentAttemptAgeSeconds") is None or job["currentAttemptAgeSeconds"] < args.minimum_job_age_seconds):
                    raise ValueError("Selected running attempt changed or no longer meets the measured task age")
                report["jobImmediatelyBeforeEviction"] = job
        for target in drain.targets:
            deadline.remaining()
            report["evictions"].append(drain.evict(target, before_evict, deadline))
            write_report(args.output, report)
            # Respect per-Deployment PDB budgets: observe replacement before the
            # next eviction rather than forcing every replica off simultaneously.
            # Full recovery waits after every selected Pod has been evicted.
            if target is not drain.targets[-1]:
                component = target["component"]
                while True:
                    deadline.remaining()
                    state = control.state(component)
                    active = [pod for pod in state["pods"] if pod["ready"] and not pod["terminating"]]
                    if len(active) == drain.baselines[component]["replicas"] and target["uid"] not in {p["uid"] for p in state["pods"]}:
                        break
                    time.sleep(deadline.remaining(3))
        report["recoveredDeployments"] = drain.wait_recovered(deadline)
        report["publicApiAfterDrain"] = verify_public_pod(control, control.state("api"))
        if upload:
            api.wait(upload, timeout=deadline.remaining())
            report["jobAfter"] = finished_job(upload["mediaId"], args.job_id)
            if report["jobAfter"]["attempt"] < report["jobBefore"]["attempt"]:
                raise AssertionError("Durable job attempt count regressed")
            report["claimReclaimedAfterEviction"] = report["jobAfter"]["attempt"] > report["jobBefore"]["attempt"]
            report["durableTaskStatus"] = "PASS"
        report["privateUploadSmoke"] = private_smoke(api, deadline)
        if report["privateUploadSmoke"]["status"] != "PASS":
            raise AssertionError("Post-drain business/private upload smoke failed")
        report["status"] = "PASS"
    except Exception as error:
        report["fatalErrorType"] = type(error).__name__
    finally:
        if drain:
            report["restoration"] = drain.restore()
            if report["restoration"]["status"] == "FAIL":
                report["status"] = "FAIL"
        if api:
            api.close()
        write_report(args.output, report)
    print(f"Controlled EKS node drain: {report['status']}; full P6 matrix: INCOMPLETE; report: {args.output}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
