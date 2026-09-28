#!/usr/bin/env python3
"""Destructive acceptance ONLY inside a labelled disposable, loopback kind cluster.

Run after integration_test.py. Every mandatory case has a result, elapsed time,
and raw JSONL observations. This is Kubernetes runtime evidence; it cannot attest
to EKS IAM, the AWS ALB, managed databases/brokers, or VPC security policies.
"""
import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from urllib.parse import quote, urlsplit, urlunsplit
import uuid

NAMESPACE = "photoplatform-dev"
PART_OF = "photoplatform"
CASES = (
    "replicas_images_revision",
    "cross_pod_auth_websocket_reconnect",
    "broker_outage_outbox_liveness",
    "database_outage_readiness_liveness",
    "claimed_worker_sigkill_recovery",
)


def outage_observation_seconds(heartbeat_max_age):
    """Observation must outlast a stale heartbeat, not merely the probe period."""
    ensure(type(heartbeat_max_age) in (int, float) and 0 < heartbeat_max_age <= 600,
           "Worker heartbeat budget must be explicit and bounded")
    return max(210, heartbeat_max_age + 30)


def ensure(condition, message):
    if not condition:
        raise AssertionError(message)


def validate_environment(environment):
    """Pure preflight, also callable by guard regression tests without kubectl."""
    ensure(environment.get("ALLOW_KIND_FAULTS") == "1", "ALLOW_KIND_FAULTS=1 is required")
    ensure(environment.get("ALLOW_INTEGRATION_WRITES") == "1", "ALLOW_INTEGRATION_WRITES=1 is required")
    cluster = environment.get("KIND_CLUSTER", "")
    ensure(re.fullmatch(r"photoplatform-ci-[a-z0-9][a-z0-9-]*", cluster),
           "Only photoplatform-ci-* disposable kind clusters are permitted")
    ensure(environment.get("KIND_NAMESPACE", NAMESPACE) == NAMESPACE,
           "Fault namespace must be photoplatform-dev")
    kubeconfig = environment.get("KUBECONFIG", "")
    ensure(kubeconfig and os.pathsep not in kubeconfig, "One explicit KUBECONFIG file is required")
    ensure(Path(kubeconfig).is_file(), "Explicit KUBECONFIG file does not exist")
    for name in ("TEST_API_URL", "TEST_DATABASE_URL", "TEST_STORAGE_ENDPOINT"):
        value = environment.get(name, "")
        ensure(value and urlsplit(value).hostname in {"localhost", "127.0.0.1", "::1"},
               name + " must be an explicit loopback endpoint")
    ensure(environment.get("KIND_EVIDENCE_DIR"), "KIND_EVIDENCE_DIR is required")
    ensure(re.fullmatch(r"[0-9a-f]{40}", environment.get("KIND_SOURCE_SHA", "")),
           "KIND_SOURCE_SHA must be the tested 40-character Git revision")
    for name in ("KIND_API_IMAGE", "KIND_WORKER_IMAGE"):
        ensure(re.search(r"@sha256:[0-9a-f]{64}$", environment.get(name, "")),
               name + " must be an immutable sha256 image reference")
    return "kind-" + cluster


def validate_kubeconfig(config, expected_context):
    ensure(config.get("current-context") == expected_context, "Current context is not this disposable kind cluster")
    contexts = config.get("contexts", [])
    ensure(len(contexts) == 1 and contexts[0].get("name") == expected_context,
           "Minified kubeconfig must identify exactly the guarded kind context")
    clusters = config.get("clusters", [])
    ensure(len(clusters) == 1, "Guarded kubeconfig must resolve one cluster")
    cluster = clusters[0].get("cluster", {})
    server = urlsplit(cluster.get("server", ""))
    ensure(server.scheme == "https" and server.hostname in {"127.0.0.1", "localhost", "::1"},
           "The Kubernetes API server must be loopback; EKS endpoints are forbidden")
    users = config.get("users", [])
    ensure(len(users) == 1 and not users[0].get("user", {}).get("exec")
           and not users[0].get("user", {}).get("auth-provider"),
           "External/cloud credential plugins are forbidden for fault injection")
    ensure(not cluster.get("insecure-skip-tls-verify"), "Kubernetes API TLS verification is required")


def validate_namespace(namespace):
    ensure(namespace.get("metadata", {}).get("name") == NAMESPACE, "Unexpected namespace")
    labels = namespace.get("metadata", {}).get("labels", {})
    ensure(labels.get("app.kubernetes.io/part-of") == PART_OF,
           "Namespace must belong to Photoplatform")
    ensure(labels.get("photoplatform.io/disposable") == "true", "Namespace must be explicitly disposable")
    ensure(labels.get("photoplatform.io/environment") == "dev", "Namespace must be explicitly a dev fixture")


def validate_worker_container(worker, inspected):
    """Require the CRI process to be the exact claimed pod before signalling it."""
    containers = [c for c in worker["containers"] if c["name"] == "media-worker"]
    ensure(len(containers) == 1, "Claimed pod must have exactly one media-worker container")
    match = re.fullmatch(r"containerd://([0-9a-f]{64})", containers[0].get("container_id", ""))
    ensure(match, "Expected a complete containerd worker container ID")
    status, info = inspected.get("status", {}), inspected.get("info", {})
    labels = status.get("labels", {})
    ensure(status.get("id") == match[1] and status.get("state") == "CONTAINER_RUNNING",
           "CRI container identity/state differs from the claimed worker")
    ensure(labels.get("io.kubernetes.pod.uid") == worker["uid"]
           and labels.get("io.kubernetes.pod.name") == worker["name"]
           and labels.get("io.kubernetes.pod.namespace") == NAMESPACE
           and status.get("metadata", {}).get("name") == "media-worker",
           "CRI container does not belong to this exact disposable worker pod")
    ensure(type(info.get("pid")) is int and info["pid"] > 1, "CRI worker PID must be a child of the guarded kind node")
    return match[1], info["pid"]


class Evidence:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.started = time.monotonic()
        self.timeline = self.directory / "kind-timeline.jsonl"
        self.timeline.write_text("")
        self.secrets = set()
        for key, value in os.environ.items():
            if value and ("PASSWORD" in key or "SECRET" in key or "TOKEN" in key):
                self.secrets.add(value)

    def safe(self, value):
        text = str(value)
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        text = re.sub(r'(postgres(?:ql)?://)[^\s"<>]+', r"\1[REDACTED]", text)
        text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[JWT REDACTED]", text)
        return text

    def record(self, event, **data):
        item = {"utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "elapsed_seconds": round(time.monotonic() - self.started, 3), "event": event, **data}
        line = self.safe(json.dumps(item, default=str, sort_keys=True))
        with self.timeline.open("a") as stream:
            stream.write(line + "\n")
        return item


class Forward:
    def __init__(self, harness, pod, remote_port, local_port=None):
        self.harness, self.pod = harness, pod
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", local_port or 0))
            self.port = listener.getsockname()[1]
        self.log = (harness.evidence.directory / f"forward-{pod}-{self.port}.log").open("w")
        self.process = subprocess.Popen(harness.kubectl + ["port-forward", "--address=127.0.0.1",
            "pod/" + pod, f"{self.port}:{remote_port}"], stdout=self.log, stderr=self.log)
        harness.forwards.append(self)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("Pod port-forward exited for " + pod)
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=.5):
                    harness.evidence.record("port_forward_started", pod=pod, local_port=self.port, remote_port=remote_port)
                    return
            except OSError:
                time.sleep(.2)
        raise TimeoutError("Pod port-forward did not open for " + pod)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.log.close()


class Harness:
    def __init__(self, evidence, context):
        self.evidence, self.context = evidence, context
        self.kubectl = ["kubectl", "--context=" + context, "--namespace=" + NAMESPACE, "--request-timeout=15s"]
        self.forwards, self.restores = [], {}
        self.apis = {}
        self.database_forward = None
        self.original_db = os.environ["TEST_DATABASE_URL"]
        self.integration = None
        self.test = None
        self.namespace_uid = None

    def command(self, *args, check=True, timeout=45):
        result = subprocess.run(self.kubectl + list(args), capture_output=True, text=True, timeout=timeout)
        # Avoid persisting full pod specs or Secret/config output. Observations are selected below.
        self.evidence.record("kubectl", args=list(args), returncode=result.returncode,
                             error=self.evidence.safe(result.stderr[-1500:]) if result.returncode else "")
        if check and result.returncode:
            raise RuntimeError("kubectl " + " ".join(args[:3]) + " failed: " + self.evidence.safe(result.stderr[-1500:]))
        return result

    def data(self, *args):
        return json.loads(self.command(*args, "-o", "json").stdout)

    def guard(self):
        config = self.data("config", "view", "--minify")
        validate_kubeconfig(config, self.context)
        namespace = self.data("get", "namespace", NAMESPACE)
        validate_namespace(namespace)
        uid = namespace["metadata"]["uid"]
        ensure(not self.namespace_uid or uid == self.namespace_uid, "Guarded disposable namespace was replaced")
        self.namespace_uid = uid
        self.evidence.record("guard_pass", context=self.context, namespace=NAMESPACE,
                             api_server="verified loopback HTTPS", namespace_disposable=True)

    def pods(self, component, dependency=False):
        selector = ("app.kubernetes.io/part-of=photoplatform" if dependency else "app.kubernetes.io/instance=photo")
        label = "dependency=" if dependency else "app.kubernetes.io/component="
        return self.data("get", "pods", "-l", selector + "," + label + component)["items"]

    @staticmethod
    def snapshot(pods):
        result = []
        for pod in pods:
            status = pod.get("status", {})
            result.append({"name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
                "deletion_timestamp": pod["metadata"].get("deletionTimestamp"),
                "ready": any(x.get("type") == "Ready" and x.get("status") == "True" for x in status.get("conditions", [])),
                "phase": status.get("phase"),
                "containers": [{"name": x["name"], "restarts": x.get("restartCount", 0), "image_id": x.get("imageID", ""),
                                "container_id": x.get("containerID", ""),
                                "previous_exit_code": x.get("lastState", {}).get("terminated", {}).get("exitCode"),
                                "running": bool(x.get("state", {}).get("running")),
                                "ready": x.get("ready", False)} for x in status.get("containerStatuses", [])],
                "images": [x["image"] for x in pod["spec"]["containers"]],
                "node": pod["spec"].get("nodeName"),
                "source_sha": pod["metadata"].get("annotations", {}).get("photoplatform.io/revision")})
        return sorted(result, key=lambda x: x["name"])

    def wait(self, predicate, message, timeout=180, interval=2):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            result = predicate()
            if result:
                return result
            time.sleep(interval)
        raise TimeoutError(message)

    def ready_apps(self):
        api, workers = self.snapshot(self.pods("api")), self.snapshot(self.pods("media-worker"))
        return (api, workers) if len(api) == 2 and workers and all(p["ready"] for p in api + workers) else None

    def refresh_apis(self):
        pods = self.wait(lambda: self.ready_apps(), "API/worker pods did not become ready", timeout=240)[0]
        for pod in pods:
            name = pod["name"]
            if name not in self.apis or self.apis[name].process.poll() is not None:
                self.apis[name] = Forward(self, name, 8080)
        active = {p["name"] for p in pods}
        self.apis = {name: forward for name, forward in self.apis.items() if name in active}
        if self.integration:
            self.integration.API = next(iter(self.apis.values())).url
        return pods

    def dependency(self, component):
        items = self.data("get", "statefulsets", "-l",
                          "app.kubernetes.io/part-of=photoplatform,dependency=" + component)["items"]
        ensure(len(items) == 1, "Expected one labelled disposable " + component + " StatefulSet")
        return items[0]

    def database(self):
        pods = self.wait(lambda: [p for p in self.pods("postgres", dependency=True)
                                if self.snapshot([p])[0]["ready"]], "PostgreSQL did not become ready", timeout=180)
        if self.database_forward:
            self.database_forward.close()
        self.database_forward = Forward(self, pods[0]["metadata"]["name"], 5432)
        parsed = urlsplit(self.original_db)
        credentials = parsed.netloc.rsplit("@", 1)[0] + "@" if "@" in parsed.netloc else ""
        self.integration.DB = urlunsplit((parsed.scheme, credentials + f"127.0.0.1:{self.database_forward.port}",
                                         parsed.path, parsed.query, parsed.fragment))

    def setup_fixtures(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import integration_test as integration
        self.integration = integration
        self.refresh_apis()
        self.database()
        storage_pods = self.wait(lambda: [p for p in self.pods("minio", dependency=True)
                                         if self.snapshot([p])[0]["ready"]], "MinIO did not become ready", timeout=180)
        storage_port = urlsplit(os.environ["TEST_STORAGE_ENDPOINT"]).port
        ensure(storage_port, "TEST_STORAGE_ENDPOINT must contain the signed-upload URL port")
        Forward(self, storage_pods[0]["metadata"]["name"], 9000, local_port=storage_port)
        integration.PipelineIntegration.setUpClass()
        self.test = integration.PipelineIntegration()
        self.evidence.secrets.update((self.test.owner, self.test.other, self.test.admin))
        self.evidence.record("disposable_fixtures_created", account_suffix=self.test.suffix)

    def scale_down(self, component):
        self.guard()  # Re-check boundary immediately before each destructive mutation.
        dependency = self.dependency(component)
        name, replicas = dependency["metadata"]["name"], dependency["spec"].get("replicas", 1)
        ensure(replicas == 1, "Fault fixture must have exactly one " + component + " replica")
        self.restores[name] = replicas
        self.command("scale", "statefulset/" + name, "--replicas=0")
        self.wait(lambda: not self.pods(component, dependency=True), component + " did not stop", timeout=150)
        self.evidence.record("dependency_outage_started", component=component, statefulset=name)
        return name

    def restore(self, name):
        self.command("scale", "statefulset/" + name, "--replicas=" + str(self.restores[name]))
        self.command("rollout", "status", "statefulset/" + name, "--timeout=180s", timeout=195)
        del self.restores[name]
        self.evidence.record("dependency_restored", statefulset=name)

    def request(self, forward, method, path, token=None, **kwargs):
        import requests
        headers = kwargs.pop("headers", {})
        if token:
            headers["Authorization"] = "Bearer " + token
        return requests.request(method, forward.url + path, headers=headers, timeout=12, **kwargs)

    def pod_health(self, pod, mode):
        return self.command("exec", pod, "--", "python", "-m", "app.health", "--mode", mode,
                            check=False, timeout=25).returncode

    def worker_heartbeat(self, pod):
        source = ("import json,os,time;from app.health import live_path;"
                  "p=json.loads(live_path().read_text());"
                  "p['observed_at']=time.time();"
                  "p['maximum_age_seconds']=float(os.getenv('WORKER_LIVE_MAX_AGE_SECONDS','180'));"
                  "print(json.dumps(p))")
        row = json.loads(self.command("exec", pod, "--", "python", "-c", source, timeout=25).stdout)
        ensure(type(row.get("updated_at")) in (int, float) and row["updated_at"] > 0,
               "Worker heartbeat does not contain a real timestamp")
        return row

    def signal_worker(self, worker):
        self.guard()
        cluster = os.environ["KIND_CLUSTER"]
        node = worker["node"]
        image = os.environ["KIND_WORKER_IMAGE"]
        ensure(image in worker["images"] and worker["source_sha"] == os.environ["KIND_SOURCE_SHA"],
               "Claimed worker image/revision differs from the tested release")
        ensure(any(image.rsplit("@", 1)[1] in c["image_id"] for c in worker["containers"]),
               "Claimed worker runtime image digest differs from the tested release")
        def host(*args):
            result = subprocess.run(list(args), capture_output=True, text=True, timeout=25)
            self.evidence.record("kind_host_command", args=list(args), returncode=result.returncode,
                                 error=self.evidence.safe(result.stderr[-1000:]) if result.returncode else "")
            ensure(result.returncode == 0, "Guarded kind-node operation failed: " + " ".join(args[:3]))
            return result.stdout
        ensure(node in host("kind", "get", "nodes", "--name", cluster).splitlines(),
               "Claimed worker node is not a member of this disposable kind cluster")
        node_labels = json.loads(host("docker", "inspect", "--format", "{{json .Config.Labels}}", node))
        ensure(node_labels.get("io.x-k8s.kind.cluster") == cluster, "Docker node does not belong to this kind cluster")
        containers = [c for c in worker["containers"] if c["name"] == "media-worker"]
        ensure(len(containers) == 1, "Claimed pod must have one media-worker container")
        cid = containers[0].get("container_id", "")
        ensure(re.fullmatch(r"containerd://[0-9a-f]{64}", cid), "Invalid claimed container ID")
        inspected = json.loads(host("docker", "exec", node, "crictl", "inspect", cid.split("://", 1)[1]))
        container_id, pid = validate_worker_container(worker, inspected)
        self.evidence.record("worker_sigkill_target_verified", pod=worker["name"], uid=worker["uid"],
                             node=node, container_id=container_id, node_namespace_pid=pid)
        # Namespace init ignores SIGKILL sent by a peer in its own PID namespace.
        # Signal through the ancestor kind-node runtime, targeting the exact task ID.
        # https://man7.org/linux/man-pages/man7/pid_namespaces.7.html
        host("docker", "exec", node, "ctr", "--namespace", "k8s.io", "tasks", "kill", "--signal", "SIGKILL", container_id)
        return time.monotonic()

    @staticmethod
    def identity(snapshot):
        return {p["name"]: (p["uid"], tuple((c["name"], c["restarts"]) for c in p["containers"])) for p in snapshot}

    def observe_outage(self, dependency, baseline):
        started, samples = time.monotonic(), 0
        expected_api = 200 if dependency == "rabbitmq" else 503
        workers = baseline[1]
        self.wait(lambda: all(self.pod_health(p["name"], "readiness") == 1 for p in workers),
                  "Workers did not report dependency readiness failure", timeout=70, interval=3)
        self.wait(lambda: all(not p["ready"] for p in self.snapshot(self.pods("media-worker"))),
                  "Kubernetes did not remove worker readiness", timeout=60, interval=3)
        first_heartbeats = {p["name"]: self.worker_heartbeat(p["name"]) for p in workers}
        duration = max(outage_observation_seconds(row["maximum_age_seconds"]) for row in first_heartbeats.values())
        last_heartbeats = first_heartbeats
        if dependency == "postgres":
            self.wait(lambda: all(not p["ready"] for p in self.snapshot(self.pods("api"))),
                      "Kubernetes did not remove API readiness during database outage", timeout=75, interval=3)
        # Start the duration after readiness converges: no credit for graceful dependency shutdown.
        started = time.monotonic()
        while time.monotonic() - started < duration:
            current = self.snapshot(self.pods("api")), self.snapshot(self.pods("media-worker"))
            ensure(self.identity(current[0]) == self.identity(baseline[0]), "API pods restarted/replaced during dependency outage")
            ensure(self.identity(current[1]) == self.identity(baseline[1]), "Worker pods restarted/replaced during dependency outage")
            ensure(all(p["ready"] == (dependency == "rabbitmq") for p in current[0]),
                   "API Kubernetes readiness differs from the dependency outage contract")
            api_probes = []
            for pod in baseline[0]:
                forward = self.apis[pod["name"]]
                live = self.request(forward, "GET", "/livez").status_code
                ready = self.request(forward, "GET", "/readyz").status_code
                api_probes.append({"pod": pod["name"], "live_status": live, "ready_status": ready})
                ensure(live == 200 and ready == expected_api, "Unexpected API liveness/readiness during " + dependency + " outage")
            worker_probes = []
            last_heartbeats = {}
            for pod in workers:
                live, ready = self.pod_health(pod["name"], "live"), self.pod_health(pod["name"], "readiness")
                heartbeat = self.worker_heartbeat(pod["name"])
                last_heartbeats[pod["name"]] = heartbeat
                worker_probes.append({"pod": pod["name"], "live_exit": live, "ready_exit": ready, "heartbeat": heartbeat})
                ensure(live == 0 and ready == 1, "Unexpected worker liveness/readiness during " + dependency + " outage")
            ensure(all(not p["ready"] for p in current[1]), "Worker unexpectedly Kubernetes-ready during outage")
            self.evidence.record("outage_sample", dependency=dependency, outage_seconds=round(time.monotonic() - started, 3),
                                 api_probes=api_probes, worker_probes=worker_probes, api=current[0], workers=current[1])
            samples += 1
            time.sleep(4)
        elapsed = time.monotonic() - started
        ensure(elapsed >= duration and samples >= 3, "Insufficient outage observation beyond the heartbeat budget")
        progress = {name: last_heartbeats[name]["updated_at"] - row["updated_at"] for name, row in first_heartbeats.items()}
        ensure(all(value > 0 for value in progress.values()), "Worker I/O heartbeat did not progress during the dependency outage")
        return {"observed_outage_seconds": round(elapsed, 3), "required_observation_seconds": duration,
                "heartbeat_max_age_seconds": {name: row["maximum_age_seconds"] for name, row in first_heartbeats.items()},
                "heartbeat_progress_seconds": progress, "samples": samples, "uid_restart_changes": 0}

    def replicas_images_revision(self):
        api, workers = self.wait(self.ready_apps, "Expected two ready API pods and ready workers", timeout=240)
        ensure(len({p["uid"] for p in api}) == 2, "API replicas do not have distinct pod UIDs")
        for component, pods, image in (("api", api, os.environ["KIND_API_IMAGE"]),
                                       ("media-worker", workers, os.environ["KIND_WORKER_IMAGE"])):
            digest = image.rsplit("@", 1)[1]
            for pod in pods:
                ensure(image in pod["images"], component + " does not use expected immutable image")
                ensure(any(digest in c["image_id"] for c in pod["containers"]), component + " runtime imageID digest differs")
                ensure(pod["source_sha"] == os.environ["KIND_SOURCE_SHA"], component + " pod revision annotation differs")
        self.evidence.record("replicas_verified", api=api, workers=workers)
        return {"api_replicas": 2, "api_distinct_uids": 2, "worker_replicas": len(workers), "runtime_digest_verified": True,
                "source_sha": os.environ["KIND_SOURCE_SHA"]}

    def cross_pod_auth_websocket_reconnect(self):
        import websocket
        api = self.refresh_apis()
        first, second = [self.apis[p["name"]] for p in api]
        suffix = uuid.uuid4().hex[:10]
        register = self.request(first, "POST", "/api/auth/register", json={"name": "Kind socket owner",
                    "email": "socket-" + suffix + "@test.example", "password": "test-password-123"})
        ensure(register.status_code == 200, "Registration on pod A failed")
        token = register.json()["token"]
        owner_id = register.json()["user"]["id"]
        self.evidence.secrets.add(token)
        ensure(self.request(second, "GET", "/api/auth/me", token).status_code == 200, "Pod A JWT was rejected by pod B")
        team = self.request(first, "POST", "/api/teams", token, json={"name": "Kind socket " + suffix, "description": "disposable test"})
        ensure(team.status_code == 200, "Team creation failed")
        team_id = team.json()["id"]
        other_email = "other-" + self.test.suffix + "@test.example"
        invite = self.request(first, "POST", f"/api/teams/{team_id}/members", token, json={"email": other_email})
        ensure(invite.status_code == 200, "Second user could not be invited to the team")
        other_login = self.request(first, "POST", "/api/auth/login", json={"email": other_email, "password": "test-password-123"})
        ensure(other_login.status_code == 200, "Second user could not authenticate on pod A")
        other_token, other_id = other_login.json()["token"], other_login.json()["user"]["id"]
        self.evidence.secrets.add(other_token)
        ensure(owner_id != other_id, "Cross-pod WebSocket case must use two distinct users")
        ensure(self.request(second, "GET", "/api/auth/me", other_token).status_code == 200,
               "Second user's pod A JWT was rejected by pod B")
        sockets = []
        def connect(issuer, target, actor_token=token):
            response = self.request(issuer, "POST", f"/api/teams/{team_id}/socket-ticket", actor_token)
            ensure(response.status_code == 200, "Socket ticket could not be issued")
            ticket = response.json()["ticket"]
            self.evidence.secrets.add(ticket)
            url = target.url.replace("http://", "ws://") + f"/ws/teams/{team_id}?ticket=" + quote(ticket, safe="")
            ws = websocket.create_connection(url, timeout=12, origin=os.getenv("KIND_WS_ORIGIN", "http://localhost:8080"),
                                             http_proxy_host=None, http_proxy_port=None)
            sockets.append(ws)
            return ws
        def relay(sender, receiver):
            marker = "kind-cross-pod-" + uuid.uuid4().hex
            sender.send(marker)
            deadline = time.monotonic() + 18
            receiver.settimeout(3)
            while time.monotonic() < deadline:
                try:
                    message = receiver.recv()
                except websocket.WebSocketTimeoutException:
                    sender.send(marker)
                    continue
                ensure(message, "WebSocket closed before cross-pod notification")
                event = json.loads(message)
                if event.get("type") == "NOTE" and marker in event.get("message", ""):
                    self.evidence.record("cross_pod_note", team_id=team_id, event_type=event["type"], marker=marker)
                    return
            raise TimeoutError("NOTE did not travel between distinct API pods")
        try:
            socket_a, socket_b = connect(first, first), connect(first, second, other_token)
            relay(socket_a, socket_b)
            old_uid = api[0]["uid"]
            self.guard()
            self.command("delete", "pod", first.pod, "--wait=false")
            replacement = self.wait(lambda: next((p for p in self.snapshot(self.pods("api"))
                          if p["name"] != second.pod and p["uid"] != old_uid and p["ready"]), None),
                          "Replacement API replica never became ready", timeout=240)
            # Check that the surviving replica continues authorizing the original JWT.
            ensure(self.request(second, "GET", "/api/auth/me", token).status_code == 200, "Surviving API rejected original JWT")
            self.refresh_apis()
            third = self.apis[replacement["name"]]
            socket_c = connect(second, third)  # Explicit new ticket, not an expired/reused handshake.
            relay(socket_c, socket_b)
            ensure(self.request(third, "GET", "/api/auth/me", token).status_code == 200, "Replacement API rejected original JWT")
            self.evidence.record("api_replacement_reconnect", deleted_pod=first.pod, deleted_uid=old_uid,
                                 surviving_pod=second.pod, replacement=replacement, fresh_ticket=True,
                                 distinct_user_ids=[owner_id, other_id])
            return {"jwt_cross_pod": True, "ticket_cross_pod": True, "note_cross_pod": True,
                    "distinct_users": 2, "api_deleted": first.pod, "replacement_uid": replacement["uid"],
                    "fresh_ticket_reconnected": True}
        finally:
            for ws in sockets:
                with contextlib.suppress(Exception):
                    ws.close()

    def broker_outage_outbox_liveness(self):
        import psycopg
        self.refresh_apis()
        baseline = self.ready_apps()
        name = self.scale_down("rabbitmq")
        upload = None
        try:
            # Initiate AND complete while broker is actually absent.
            upload, _, payload = self.test.create()
            self.test.put(upload, payload)
            self.test.complete(upload)
            with psycopg.connect(self.integration.DB, connect_timeout=5) as conn:
                row = conn.execute("""SELECT j.status,o.last_published_at FROM media_processing_jobs j
                      JOIN media_outbox o ON o.job_id=j.id WHERE j.media_id=%s AND j.job_type='MEDIA_PROCESS'""",
                                   (upload["mediaId"],)).fetchone()
            ensure(row == ("QUEUED", None), "Broker outage did not leave an unpublished durable outbox record")
            self.evidence.record("broker_outage_durable_upload", media_id=upload["mediaId"], job_status=row[0],
                                 last_published_at=row[1], completion_accepted=True)
            result = self.observe_outage("rabbitmq", baseline)
        finally:
            self.restore(name)
        self.wait(self.ready_apps, "Workers did not reconnect to restored broker", timeout=180)
        self.test.wait(upload, timeout=180)
        with psycopg.connect(self.integration.DB, connect_timeout=5) as conn:
            job = conn.execute("SELECT status FROM media_processing_jobs WHERE media_id=%s AND job_type='MEDIA_PROCESS'",
                               (upload["mediaId"],)).fetchone()
            variants = conn.execute("SELECT count(*) FROM media_variants WHERE media_id=%s", (upload["mediaId"],)).fetchone()[0]
        ensure(job == ("DONE",) and variants == 5, "Durable outbox upload did not recover to five variants")
        return {**result, "upload_completed_during_outage": True, "outbox_preserved": True,
                "recovered_job_status": job[0], "variants": variants}

    def database_outage_readiness_liveness(self):
        import psycopg
        self.refresh_apis()
        baseline = self.ready_apps()
        before_pvcs = {x["metadata"]["name"]: x["metadata"]["uid"] for x in self.data("get", "pvc")["items"]}
        with psycopg.connect(self.integration.DB, connect_timeout=5) as conn:
            accounts = conn.execute("SELECT count(*) FROM user_accounts WHERE email LIKE %s", ("%" + self.test.suffix + "@test.example",)).fetchone()[0]
        name = self.scale_down("postgres")
        try:
            result = self.observe_outage("postgres", baseline)
        finally:
            self.restore(name)
            self.database()  # A pod-bound old port-forward terminates with PostgreSQL.
        self.wait(self.ready_apps, "API/worker readiness did not recover with database", timeout=240)
        after_pvcs = {x["metadata"]["name"]: x["metadata"]["uid"] for x in self.data("get", "pvc")["items"]}
        ensure(before_pvcs == after_pvcs and before_pvcs, "Dependency outage replaced or lost a PVC")
        with psycopg.connect(self.integration.DB, connect_timeout=5) as conn:
            remaining = conn.execute("SELECT count(*) FROM user_accounts WHERE email LIKE %s", ("%" + self.test.suffix + "@test.example",)).fetchone()[0]
        ensure(accounts == remaining == 3, "Database records did not survive StatefulSet restart")
        return {**result, "retained_pvc_uids": before_pvcs, "persistent_test_accounts": remaining,
                "api_readiness_outage_status": 503, "recovered_readiness": True}

    def claimed_worker_sigkill_recovery(self):
        import psycopg
        self.refresh_apis()
        upload, _, payload = self.test.create()
        self.test.put(upload, payload)
        media_id = upload["mediaId"]
        # This barrier uses ordinary DB locks, not synthetic RUNNING/expired status.
        # The worker must acquire its own real durable claim before PID 1 is killed.
        with psycopg.connect(self.integration.DB, autocommit=True, connect_timeout=5) as barrier:
            barrier.execute("SELECT pg_advisory_lock(%s)", (media_id,))
            try:
                self.test.complete(upload)
                with barrier.transaction():
                    barrier.execute("SET LOCAL statement_timeout='25s'")
                    barrier.execute("SELECT id FROM image_assets WHERE id=%s FOR UPDATE", (media_id,))
                    barrier.execute("SELECT pg_advisory_unlock(%s)", (media_id,))
                    # A busy advisory-lock delivery can already have been ACKed.
                    # Re-publish the test job through its real outbox, leaving claim/lease untouched.
                    with psycopg.connect(self.integration.DB, autocommit=True, connect_timeout=5) as control:
                        control.execute("UPDATE media_outbox SET last_published_at=NULL WHERE job_id IN "
                                        "(SELECT id FROM media_processing_jobs WHERE media_id=%s AND job_type='MEDIA_PROCESS')", (media_id,))
                        def claim():
                            row = control.execute("""SELECT id,status,attempt,worker_id,claim_token,
                              greatest(0,extract(epoch FROM lease_until-now())) FROM media_processing_jobs
                              WHERE media_id=%s AND job_type='MEDIA_PROCESS'""", (media_id,)).fetchone()
                            return row if row and row[1] == "RUNNING" and row[4] else None
                        row = self.wait(claim, "Worker did not acquire a real RUNNING claim", timeout=90, interval=.15)
                        worker = next((p for p in self.snapshot(self.pods("media-worker")) if p["name"] == row[3]), None)
                        ensure(worker, "Claimed worker_id does not identify this release's pod")
                        self.evidence.record("worker_claim_observed", media_id=media_id, job_id=row[0], status=row[1],
                                             attempt=row[2], worker=worker, claim_token=row[4], lease_remaining_seconds=float(row[5]))
                        killed_at = self.signal_worker(worker)
            finally:
                with contextlib.suppress(psycopg.Error):
                    barrier.execute("SELECT pg_advisory_unlock(%s)", (media_id,))
        before_restarts = sum(c["restarts"] for c in worker["containers"])
        def restarted():
            current = self.snapshot(self.pods("media-worker"))
            return next((p for p in current if p["name"] == worker["name"] and p["uid"] == worker["uid"]
                         and p["ready"] and sum(c["restarts"] for c in p["containers"]) > before_restarts), None)
        replacement = self.wait(restarted, "SIGKILL did not cause a ready restarted worker container", timeout=180)
        new_container = next(c for c in replacement["containers"] if c["name"] == "media-worker")
        old_container = next(c for c in worker["containers"] if c["name"] == "media-worker")
        ensure(new_container["previous_exit_code"] == 137 and new_container["container_id"] != old_container["container_id"],
               "Worker restart does not attest to a SIGKILL exit and a replacement container")
        # Natural broker redelivery + durable lease/outbox watchdog. No lease/status rewrites.
        self.test.wait(upload, timeout=max(390, float(row[5]) + 330))
        with psycopg.connect(self.integration.DB, connect_timeout=5) as conn:
            recovered = conn.execute("SELECT status,attempt,claim_token FROM media_processing_jobs WHERE id=%s", (row[0],)).fetchone()
            variants = conn.execute("SELECT count(*) FROM media_variants WHERE media_id=%s", (media_id,)).fetchone()[0]
        ensure(recovered[0] == "DONE" and recovered[1] >= row[2] + 1 and recovered[2] != row[4] and variants == 5,
               "Killed claim did not recover with a distinct claim and one variant set")
        elapsed = round(time.monotonic() - killed_at, 3)
        self.evidence.record("worker_sigkill_recovered", media_id=media_id, job_status=recovered[0], attempt=recovered[1],
                             claim_changed=True, variants=variants, worker=replacement, recovery_seconds=elapsed)
        return {"real_claim_before_sigkill": True, "container_uid_retained": True, "restart_count_increased": True,
                "sigkill_source": "ancestor disposable kind-node containerd task signal", "killed_container_exit_code": 137,
                "original_attempt": row[2], "recovered_attempt": recovered[1], "claim_token_changed": True,
                "variants": variants, "natural_recovery_seconds": elapsed,
                "test_barrier": "image row lock and advisory lock; one outbox due reset before kill",
                "lease_or_job_status_rewritten": False}

    def cleanup(self):
        errors = []
        for name in list(self.restores):
            try:
                self.restore(name)
            except Exception as exc:
                errors.append(self.evidence.safe(type(exc).__name__ + ": " + str(exc)))
        for forward in self.forwards:
            with contextlib.suppress(Exception):
                forward.close()
        self.evidence.record("cleanup", dependency_restore_errors=errors, owned_port_forwards_stopped=True)
        return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guard-only", action="store_true", help="Validate boundaries without mutation or fixtures")
    args = parser.parse_args()
    evidence = Evidence(os.getenv("KIND_EVIDENCE_DIR", "work/kind-evidence"))
    results, harness, cleanup_errors = [], None, []
    try:
        context = validate_environment(os.environ)
        harness = Harness(evidence, context)
        harness.guard()
        if args.guard_only:
            evidence.record("guard_only_pass")
            print("Disposable kind guard: PASS")
            return 0
        harness.setup_fixtures()
        for case in CASES:
            started = time.monotonic()
            evidence.record("case_started", case=case)
            try:
                detail = getattr(harness, case)()
                result = {"case": case, "status": "PASS", "detail": detail}
            except Exception as exc:
                result = {"case": case, "status": "FAIL", "error": evidence.safe(type(exc).__name__ + ": " + str(exc))}
                # Continue only after restoring failed dependencies; each mandatory case remains visible.
                for name in list(harness.restores):
                    try:
                        harness.restore(name)
                        if "postgres" in name:
                            harness.database()
                    except Exception as restore_error:
                        result["restore_error"] = evidence.safe(str(restore_error))
            result["elapsed_seconds"] = round(time.monotonic() - started, 3)
            results.append(result)
            evidence.record("case_finished", **result)
            print(case + ": " + result["status"], flush=True)
    except Exception as exc:
        error = evidence.safe(type(exc).__name__ + ": " + str(exc))
        evidence.record("setup_failed", error=error)
        for case in CASES:
            if case not in {r["case"] for r in results}:
                results.append({"case": case, "status": "FAIL", "error": "Setup/boundary validation failed: " + error})
    finally:
        if harness:
            cleanup_errors = harness.cleanup()
    summary = {"environment": "disposable kind on GitHub runner; Kubernetes runtime only", "denominator": len(CASES),
               "passed": sum(r["status"] == "PASS" for r in results), "failed": sum(r["status"] == "FAIL" for r in results),
               "skipped": 0, "cases": results, "cleanup_errors": cleanup_errors,
               "not_validated": ["AWS EKS control plane", "AWS Pod Identity/IAM", "AWS ALB/Ingress TLS", "AWS VPC/security policies",
                                 "RDS/managed RabbitMQ availability", "public production deployment"],
               "timeline": str(evidence.timeline.name), "source_sha": os.getenv("KIND_SOURCE_SHA", "")}
    (evidence.directory / "kind-summary.json").write_text(evidence.safe(json.dumps(summary, indent=2, default=str)) + "\n")
    print(json.dumps({"passed": summary["passed"], "denominator": summary["denominator"], "failed": summary["failed"],
                      "skipped": summary["skipped"], "cleanup_errors": len(cleanup_errors)}), flush=True)
    return 0 if summary["passed"] == len(CASES) and not cleanup_errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
