"""EKS-only dev harness controls; never write before identity/disposable guards pass.

Uses the caller's kubeconfig with an explicit EKS ARN context. Kubernetes responses
are reduced to metadata before writing evidence; credentials and signed URLs are
never persisted. No ECS client or controller is used.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import quote, urlparse

from benchmarks.cloud_common import cloud_guard, required, secure_origin


DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?")
COMPONENTS = {"api": "api", "media-worker": "worker", "encoder": "encoder", "embedding-worker": "encoder"}


def safe_name(value):
    if not NAME.fullmatch(value) or len(value) > 253:
        raise ValueError("Invalid Kubernetes resource name")
    return value


def kubectl(context, namespace, *args, body=None, allowed_returncodes=(0,)):
    """No shell, explicit context/namespace, bounded calls, no credential-bearing stderr."""
    result = subprocess.run(["kubectl", "--context", context, "--namespace", namespace,
                             "--request-timeout=30s", *args],
                            input=json.dumps(body) if body is not None else None,
                            text=True, capture_output=True, timeout=45)
    if result.returncode not in allowed_returncodes:
        raise RuntimeError("Scoped kubectl operation failed")
    return result.stdout


def exact_worker_pod(control, name, uid):
    """Bind exec to a verified UID both before dispatch and inside the container."""
    import uuid
    safe_name(name)
    uuid.UUID(uid)
    matches = [p for p in control.pods("media-worker") if p["name"] == name and p["uid"] == uid]
    if len(matches) != 1 or matches[0]["phase"] != "Running" or matches[0]["terminating"]:
        raise ValueError("Selected UID must identify a running member of the media worker Deployment")
    raw = control.json("get", "pod", name, "-o", "json")
    if raw["metadata"]["uid"] != uid:
        raise ValueError("Selected Pod UID changed before exec")
    containers = [c for c in raw["spec"]["containers"] if c["name"] == "media-worker"]
    if len(containers) != 1 or containers[0].get("image") != control.manifest["images"]["worker"]:
        raise ValueError("Expected one verified media-worker container")
    env = containers[0].get("env", [])
    fields = [v for v in env if v.get("name") == "POD_UID"]
    if len(fields) != 1 or fields[0].get("valueFrom", {}).get("fieldRef", {}).get("fieldPath") != "metadata.uid":
        raise ValueError("Pod exec requires downward API POD_UID=metadata.uid")
    return matches[0]


def worker_python(control, name, uid, code, payload=None, allowed_returncodes=(0,)):
    exact_worker_pod(control, name, uid)
    wrapped = "import os,sys,json\nif os.environ.get('POD_UID') != sys.argv[1]: raise SystemExit(42)\n" + code
    return kubectl(control.arn, control.namespace, "exec", name, "--container", "media-worker", "--",
                   "python", "-c", wrapped, uid, json.dumps(payload or {}), allowed_returncodes=allowed_returncodes)


def load_manifest(path, sha):
    manifest = json.loads(Path(path).read_text())
    if not re.fullmatch(r"[a-f0-9]{40}", sha) or manifest.get("sha") != sha:
        raise ValueError("Manifest must match the expected full deployment SHA")
    if not all(key in manifest.get("images", {}) for key in ("api", "worker")):
        raise ValueError("Manifest must include API and worker images")
    registry = f"{required('EXPECTED_AWS_ACCOUNT_ID')}.dkr.ecr.{required('AWS_REGION')}.amazonaws.com/"
    for key, image in manifest["images"].items():
        if not re.fullmatch(re.escape(registry) + r"[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[a-f0-9]{64}", image):
            raise ValueError("Manifest images must use immutable digests in the selected account/region ECR")
        if not manifest.get("runnableDigests", {}).get(key):
            raise ValueError("Every manifest image must include runnable platform digest provenance")
    for key, leaves in manifest.get("runnableDigests", {}).items():
        if key not in manifest["images"] or not leaves or any(not DIGEST.fullmatch(d) for d in leaves):
            raise ValueError("Invalid platform digest provenance")
    return manifest


def valid_tags(tags):
    return all(tags.get(k) == v for k, v in {
        "Project": "photoplatform", "Environment": "dev", "DisposableEnvironment": "true"}.items())


class EKSControl:
    def __init__(self, aws, manifest, sha):
        self.cluster = required("EKS_CLUSTER_NAME")
        self.arn = required("EKS_CLUSTER_ARN")
        self.namespace = safe_name(required("EKS_NAMESPACE"))
        self.release = safe_name(os.getenv("EKS_RELEASE", "photoplatform"))
        self.sha, self.manifest = sha, manifest
        cluster = aws.client("eks").describe_cluster(name=self.cluster)["cluster"]
        expected_arn = f"arn:{self.arn.split(':')[1]}:eks:{required('AWS_REGION')}:{required('EXPECTED_AWS_ACCOUNT_ID')}:cluster/{self.cluster}" if self.arn.startswith("arn:") else ""
        if cluster.get("arn") != self.arn or self.arn != expected_arn or cluster.get("status") != "ACTIVE":
            raise ValueError("EKS cluster ARN/account/region/status do not match")
        if not valid_tags(cluster.get("tags", {})):
            raise ValueError("EKS cluster must be tagged as disposable photoplatform dev")
        # Only inspect server and CA from raw kubeconfig; never serialize the config.
        config = self.json("config", "view", "--minify", "--raw", "-o", "json")
        entries = config.get("clusters", [])
        if len(entries) != 1:
            raise ValueError("Expected exactly one explicitly selected kubeconfig cluster")
        configured = entries[0]["cluster"]
        if (configured.get("server") != cluster["endpoint"] or
                configured.get("certificate-authority-data") != cluster["certificateAuthority"]["data"] or
                configured.get("insecure-skip-tls-verify")):
            raise ValueError("Kubeconfig endpoint/CA must match the actual EKS cluster")
        system = self.json("get", "namespace", "kube-system", "-o", "json")
        system_uid = system["metadata"]["uid"]
        if system_uid != required("EKS_KUBE_SYSTEM_UID"):
            raise ValueError("kube-system UID differs from the pinned cluster identity")
        namespace = self.json("get", "namespace", self.namespace, "-o", "json")
        labels = namespace["metadata"].get("labels", {})
        if (labels.get("app.kubernetes.io/part-of") != "photoplatform" or
                labels.get("photoplatform.io/environment") != "dev" or
                labels.get("photoplatform.io/disposable") != "true" or
                namespace["metadata"].get("deletionTimestamp")):
            raise ValueError("Namespace must explicitly be disposable dev and not terminating")
        self.namespace_uid = namespace["metadata"]["uid"]
        self.cluster_evidence = {"clusterArn": self.arn, "clusterVersion": cluster["version"],
            "clusterTags": cluster["tags"], "namespace": self.namespace,
            "namespaceUid": self.namespace_uid, "kubeSystemUid": system_uid,
            "release": self.release, "expectedSha": sha,
            "manifestSha256": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()}
        self.deployments = {}
        for component in ("api", "media-worker"):
            self.deployments[component] = self.deployment(component)
        self.verify_endpoint()

    def json(self, *args, body=None):
        return json.loads(kubectl(self.arn, self.namespace, *args, body=body))

    def selector(self, component):
        if component not in COMPONENTS:
            raise ValueError("Unsupported application component")
        return f"app.kubernetes.io/name=photoplatform,app.kubernetes.io/instance={self.release},app.kubernetes.io/component={component}"

    def deployment(self, component):
        rows = self.json("get", "deployments", "-l", self.selector(component), "-o", "json")["items"]
        if len(rows) != 1:
            raise ValueError("Expected exactly one Deployment for the selected component")
        deployment = rows[0]
        template = deployment["spec"]["template"]
        if (template["metadata"].get("annotations", {}).get("photoplatform.io/revision") != self.sha or
                deployment["metadata"].get("deletionTimestamp")):
            raise ValueError("Deployment does not match the selected SHA or is terminating")
        image = self.manifest["images"][COMPONENTS[component]]
        containers = template["spec"]["containers"]
        if not any(c.get("image") == image for c in containers):
            raise ValueError("Deployment image does not match the digest manifest")
        labels = template["metadata"].get("labels", {})
        expected = {"app.kubernetes.io/name": "photoplatform", "app.kubernetes.io/instance": self.release,
                    "app.kubernetes.io/component": component}
        if any(labels.get(k) != v for k, v in expected.items()):
            raise ValueError("Deployment Pod labels do not match the component")
        return deployment

    def pods(self, component, deployment=None):
        deployment = deployment or self.deployment(component)
        sets = self.json("get", "replicasets", "-l", self.selector(component), "-o", "json")["items"]
        set_uids = {s["metadata"]["uid"] for s in sets if any(
            r.get("controller") and r.get("uid") == deployment["metadata"]["uid"] and r.get("kind") == "Deployment"
            for r in s["metadata"].get("ownerReferences", []))}
        result = []
        for pod in self.json("get", "pods", "-l", self.selector(component), "-o", "json")["items"]:
            if not any(r.get("controller") and r.get("uid") in set_uids and r.get("kind") == "ReplicaSet"
                       for r in pod["metadata"].get("ownerReferences", [])):
                raise ValueError("Selected Pod is not owned by the verified Deployment")
            image = self.manifest["images"][COMPONENTS[component]]
            expected = {image.rsplit("@", 1)[1], *self.manifest.get("runnableDigests", {}).get(COMPONENTS[component], [])}
            containers = {c["name"]: c for c in pod["spec"]["containers"] if c.get("image") == image}
            if not containers or pod["metadata"].get("annotations", {}).get("photoplatform.io/revision") != self.sha:
                raise ValueError("Pod template does not match the selected SHA/digest")
            statuses = [s for s in pod.get("status", {}).get("containerStatuses", []) if s["name"] in containers]
            phase = pod.get("status", {}).get("phase")
            for status in statuses:
                digests = DIGEST.findall(status.get("imageID", ""))
                if (status.get("ready") or "running" in status.get("state", {})) and (len(digests) != 1 or digests[0] not in expected):
                    raise ValueError("Running Pod imageID differs from the immutable platform manifest")
            ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in pod.get("status", {}).get("conditions", []))
            if ready and (not statuses or any(not s.get("ready") for s in statuses)):
                raise ValueError("Ready Pod lacks verified application container status")
            result.append({"name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
                "phase": phase, "ready": ready, "terminating": bool(pod["metadata"].get("deletionTimestamp")),
                "image": image, "imageIDs": [s.get("imageID") for s in statuses],
                "restarts": sum(s.get("restartCount", 0) for s in statuses)})
        return result

    def state(self, component):
        deployment = self.deployment(component)
        metadata, spec, status = deployment["metadata"], deployment["spec"], deployment.get("status", {})
        return {"deployment": metadata["name"], "uid": metadata["uid"], "resourceVersion": metadata["resourceVersion"],
            "replicas": spec.get("replicas", 1), "generation": metadata["generation"],
            "observedGeneration": status.get("observedGeneration", 0),
            "updatedReplicas": status.get("updatedReplicas", 0), "readyReplicas": status.get("readyReplicas", 0),
            "pods": self.pods(component, deployment)}

    def verify_endpoint(self):
        host = urlparse(secure_origin(required("API_URL"))).hostname
        ingresses = self.json("get", "ingresses", "-l", f"app.kubernetes.io/instance={self.release}", "-o", "json")["items"]
        services = self.json("get", "services", "-l", self.selector("api"), "-o", "json")["items"]
        valid_services = set()
        expected = self.deployments["api"]["spec"]["template"]["metadata"]["labels"]
        for service in services:
            selector = service["spec"].get("selector", {})
            if selector and all(expected.get(k) == v for k, v in selector.items()):
                valid_services.add(service["metadata"]["name"])
        routes = [path for ingress in ingresses for rule in ingress["spec"].get("rules", []) if rule.get("host") == host
                  for path in rule.get("http", {}).get("paths", [])
                  if path.get("backend", {}).get("service", {}).get("name") in valid_services]
        if not routes:
            raise ValueError("API endpoint host must route to the guarded release's API Service")
        self.cluster_evidence["apiIngressHost"] = host


def eks_guard():
    aws, evidence = cloud_guard()
    sha = os.getenv("EXPECTED_DEPLOY_SHA") or required("DEPLOY_SHA")
    if os.getenv("EXPECTED_DEPLOY_SHA") and os.getenv("DEPLOY_SHA") and os.environ["EXPECTED_DEPLOY_SHA"] != os.environ["DEPLOY_SHA"]:
        raise ValueError("Expected and deployed SHA variables disagree")
    manifest = load_manifest(required("EKS_IMAGE_MANIFEST"), sha)
    control = EKSControl(aws, manifest, sha)
    for component in ("api", "media-worker"):
        state = control.state(component)
        if (state["replicas"] < 1 or state["readyReplicas"] != state["replicas"] or
                state["updatedReplicas"] != state["replicas"] or state["observedGeneration"] < state["generation"] or
                len([p for p in state["pods"] if p["ready"] and not p["terminating"]]) != state["replicas"]):
            raise ValueError("Guard requires a settled release with all selected replicas Ready")
        evidence[component] = state
    evidence["eks"] = control.cluster_evidence
    return aws, control, evidence


def queue_snapshot():
    """Real management gauges; missing metrics fail, never become fake zeros.

    observedAtEpoch is the client observation time, not a broker freshness claim.
    Management sampling/caching is a measurement limitation recorded in evidence.
    """
    import requests
    base = secure_origin(required("RABBITMQ_MANAGEMENT_URL"))
    virtual_host = quote(os.getenv("RABBITMQ_VHOST", "/"), safe="")
    queue = quote(os.getenv("RABBITMQ_PROCESS_QUEUE", "media.process"), safe="")
    response = requests.get(f"{base}/api/queues/{virtual_host}/{queue}",
        auth=(required("RABBITMQ_USERNAME"), required("RABBITMQ_PASSWORD")), timeout=15, allow_redirects=False)
    if response.status_code != 200:
        raise RuntimeError("Queue telemetry unavailable")
    data = response.json()
    names = ("messages", "messages_ready", "messages_unacknowledged", "consumers")
    if any(type(data.get(name)) is not int or data[name] < 0 for name in names):
        raise ValueError("Queue telemetry missing an actual nonnegative integer gauge")
    if data["messages"] != data["messages_ready"] + data["messages_unacknowledged"]:
        raise ValueError("Queue telemetry gauges are inconsistent")
    timestamp = data.get("message_stats", {}).get("publish_details", {}).get("last_event")
    # Queue totals are live gauges; last publish event is not gauge freshness.
    return {name: data[name] for name in names} | {"observedAtEpoch": time.time(), "lastPublishEvent": timestamp,
        "freshness": "HTTP observation of management gauges; broker sampling/cache age is not independently measured"}
