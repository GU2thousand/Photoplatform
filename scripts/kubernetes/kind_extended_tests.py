#!/usr/bin/env python3
"""Extended real kind faults, guarded against every external/cloud target.

Long-running jobs use an explicit file barrier in the tested worker, while its
original claim/session and broker I/O remain alive. No test rewrites job status,
claim tokens, lease expiry or garbage-collection cursors. A rollback may use the
pinned baseline business binary plus the documented container/probe adapter;
that distinction is retained in every result.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from kind_tests import Evidence, Harness, NAMESPACE, ensure, validate_environment

ROOT = Path(__file__).resolve().parents[2]
HOOK_DIR = "/tmp/kind-worker-barriers"
HOOK_ENV = {"WORKER_TEST_HOOK_DIR": HOOK_DIR,
            "WORKER_TEST_HOOK_ENVIRONMENT": "kubernetes-local",
            "DISPOSABLE_ENVIRONMENT": "true"}
CASES = ("real_job_over_five_minutes_continuous_claim",
         "sigterm_active_job_graceful_drain",
         "post_s3_session_loss_fencing_and_reconciliation",
         "helm_baseline_application_rollback_preserves_media")


def validate_marker(marker, job, worker, stage):
    """Never accept a marker left by another pod, claim, media or job."""
    ensure(str(marker.get("job_id")) == str(job["id"]), "Barrier marker job differs from real claim")
    ensure(str(marker.get("claim_token")) == str(job["claim_token"]), "Barrier marker claim differs")
    ensure(marker.get("media_id") == job["media_id"], "Barrier marker media differs")
    ensure(marker.get("stage") == stage, "Barrier marker stage differs")
    ensure(marker.get("pod_name", marker.get("pod")) == worker["name"], "Barrier marker pod differs")
    ensure(marker.get("pod_uid") == worker["uid"], "Barrier marker pod UID differs")
    ensure(type(marker.get("session_pid")) is int and marker["session_pid"] > 1,
           "Barrier marker must identify the original PostgreSQL session")
    return marker


def validate_helm_files(environment):
    import yaml
    for key in ("KIND_HELM_VALUES_FILE", "KIND_HELM_LOCAL_VALUES_FILE"):
        ensure(environment.get(key) and Path(environment[key]).is_file(), key + " is required")
    local = yaml.safe_load(Path(environment["KIND_HELM_LOCAL_VALUES_FILE"]).read_text())
    ensure(local.get("runtimeMode") == "local" and local.get("environment") == "dev"
           and local.get("secrets", {}).get("provider") == "kubernetes"
           and local.get("ingress", {}).get("enabled") is False,
           "Only the isolated local Helm overlay is permitted")
    current = json.loads(Path(environment["KIND_HELM_VALUES_FILE"]).read_text())
    ensure(current.get("migration", {}).get("enabled") is False, "Rollback must never run a migration")
    ensure(current.get("release", {}).get("commitSha") == environment["KIND_SOURCE_SHA"],
           "Helm overlay revision differs from tested code")
    for name, key in (("api", "KIND_API_IMAGE"), ("mediaWorker", "KIND_WORKER_IMAGE")):
        image = current.get("images", {}).get(name, {})
        ensure(image.get("repository", "") + "@" + image.get("digest", "") == environment[key],
               "Helm overlay contains an unexpected image")
    ensure(re.search(r"@sha256:[a-f0-9]{64}$", environment.get("KIND_BASELINE_API_IMAGE", "")),
           "A separately built immutable baseline API image is required")
    ensure(environment["KIND_BASELINE_API_IMAGE"] != environment["KIND_API_IMAGE"],
           "Rollback must change the application binary digest")
    ensure(re.fullmatch(r"[a-f0-9]{40}", environment.get("KIND_BASELINE_SOURCE_SHA", "")),
           "Pinned baseline source revision is required")
    ensure(environment["KIND_BASELINE_SOURCE_SHA"] != environment["KIND_SOURCE_SHA"],
           "Baseline code must differ from upgraded code")
    ensure(re.fullmatch(r"[a-f0-9]{64}", environment.get("KIND_BASELINE_ADAPTER_SHA", "")),
           "Baseline packaging/probe adapter hash is required")
    return current


class ExtendedHarness(Harness):
    def __init__(self, evidence, context):
        super().__init__(evidence, context)
        self.hooks_installed = False
        self.current_overlay = None
        self.restore_revision = None
        self.owned_barriers = set()
        self.owned_barrier_pods = {}
        self.cordoned_nodes = set()
        self.worker_layout_restore = None
        self.temporary_pdb = None

    def guard_worker(self, worker):
        self.guard()
        pod = self.data("get", "pod", worker["name"])
        labels = pod["metadata"].get("labels", {})
        ensure(labels.get("app.kubernetes.io/instance") == "photo"
               and labels.get("app.kubernetes.io/component") == "media-worker",
               "Hook target is not this release's worker")
        actual = self.snapshot([pod])[0]
        ensure(actual["uid"] == worker["uid"], "Hook target worker pod was replaced")
        image = os.environ["KIND_WORKER_IMAGE"]
        ensure(image in actual["images"] and actual["source_sha"] == os.environ["KIND_SOURCE_SHA"],
               "Hook target worker image/revision differs")
        ensure(any(image.rsplit("@", 1)[1] in c["image_id"] for c in actual["containers"]),
               "Hook target runtime image digest differs")
        return actual

    def enable_hooks(self):
        self.guard()
        deployment = self.data("get", "deployment", "photo-media-worker")
        labels = deployment["metadata"].get("labels", {})
        ensure(labels.get("app.kubernetes.io/instance") == "photo"
               and labels.get("app.kubernetes.io/component") == "media-worker",
               "Unexpected worker deployment")
        containers = [c for c in deployment["spec"]["template"]["spec"]["containers"]
                      if c["name"] == "media-worker"]
        ensure(len(containers) == 1 and containers[0]["image"] == os.environ["KIND_WORKER_IMAGE"],
               "Hook deployment does not use tested worker image")
        ensure(not any(v["name"] in HOOK_ENV for v in containers[0].get("env", [])),
               "Refusing to overwrite pre-existing hook overrides")
        config = self.data("get", "configmap", "photo-runtime")["data"]
        ensure(config.get("STORAGE_PROVIDER") == "minio", "Worker barriers require explicit MinIO; AWS forbidden")
        self.hooks_installed = True  # cleanup is required even if rollout fails
        self.command("set", "env", "deployment/photo-media-worker", "--containers=media-worker",
                     *[k + "=" + v for k, v in HOOK_ENV.items()])
        self.command("rollout", "status", "deployment/photo-media-worker", "--timeout=240s", timeout=255)
        self.wait(self.ready_apps, "Hook-enabled worker never became ready", timeout=240)
        self.evidence.record("worker_barriers_enabled", provider="minio", disposable=True,
                             environment="kubernetes-local", worker_image=os.environ["KIND_WORKER_IMAGE"])

    def pod_python(self, worker, source, timeout=25, check=True):
        self.guard_worker(worker)
        return self.command("exec", worker["name"], "--", "python", "-c", source,
                            timeout=timeout, check=check)

    def hook_file(self, worker, filename, action="touch"):
        ensure(re.fullmatch(r"(?:media-[1-9][0-9]*\.(?:before_write|after_write)\.block|"
                            r"[a-f0-9-]{36}\.release)", filename), "Invalid hook filename")
        path = HOOK_DIR + "/" + filename
        source = "from pathlib import Path;p=Path(" + repr(path) + ");"
        source += ("p.parent.mkdir(parents=True,exist_ok=True);p.touch()" if action == "touch"
                   else "p.unlink(missing_ok=True)")
        self.pod_python(worker, source)
        if filename.endswith(".block"):
            if action == "touch":
                self.owned_barriers.add(filename)
                self.owned_barrier_pods.setdefault(filename, set()).add(worker["uid"])
            else:
                self.owned_barrier_pods.get(filename, set()).discard(worker["uid"])
                if not self.owned_barrier_pods.get(filename):
                    self.owned_barriers.discard(filename)
                    self.owned_barrier_pods.pop(filename, None)

    def release_owned_barriers(self):
        errors = []
        filenames = list(self.owned_barriers)
        if not filenames:
            return errors
        try:
            workers = self.snapshot(self.pods("media-worker"))
        except Exception as exc:
            return [self.evidence.safe(str(exc))]
        present = {worker["uid"] for worker in workers}
        for filename in filenames:
            self.owned_barrier_pods.get(filename, set()).intersection_update(present)
            if not self.owned_barrier_pods.get(filename):
                self.owned_barriers.discard(filename)
                self.owned_barrier_pods.pop(filename, None)
        for worker in workers:
            for filename in filenames:
                if worker["uid"] not in self.owned_barrier_pods.get(filename, set()):
                    continue
                try:
                    self.hook_file(worker, filename, "remove")
                except Exception as exc:
                    errors.append(self.evidence.safe(str(exc)))
        return errors

    def marker(self, worker, job_id, stage):
        ensure(re.fullmatch(r"[a-f0-9-]{36}", str(job_id)), "Job ID must be UUID")
        path = HOOK_DIR + "/" + str(job_id) + "." + stage + ".observed.json"
        output = self.pod_python(worker, "from pathlib import Path;p=Path(" + repr(path)
                                 + ");print(p.read_text() if p.is_file() else '{}')").stdout
        return json.loads(output)

    def fenced_marker(self, worker, job_id):
        path = HOOK_DIR + "/" + str(job_id) + ".fenced.json"
        output = self.pod_python(worker, "from pathlib import Path;p=Path(" + repr(path)
                                 + ");print(p.read_text() if p.is_file() else '{}')").stdout
        return json.loads(output)

    def job(self, media_id):
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(self.integration.DB, connect_timeout=5, row_factory=dict_row) as conn:
            return conn.execute("""SELECT j.id,j.media_id,j.status,j.attempt,j.worker_id,j.claim_token,
                 j.lease_until,j.updated_at,j.finished_at,
                 extract(epoch FROM j.lease_until-now()) AS lease_remaining_seconds,
                 i.processing_status,(SELECT count(*) FROM media_variants v WHERE v.media_id=j.media_id) AS variants
                 FROM media_processing_jobs j JOIN image_assets i ON i.id=j.media_id
                 WHERE j.media_id=%s AND j.job_type='MEDIA_PROCESS'""", (media_id,)).fetchone()

    def create_blocked(self, stage):
        self.refresh_apis()
        upload, _, payload = self.test.create()
        self.test.put(upload, payload)
        filename = f"media-{upload['mediaId']}.{stage}.block"
        workers = self.snapshot(self.pods("media-worker"))
        ensure(workers and all(p["ready"] for p in workers), "No ready worker for barrier")
        for worker in workers:
            self.hook_file(worker, filename)
        self.test.complete(upload)
        job = self.wait(lambda: (row if (row := self.job(upload["mediaId"]))
                      and row["status"] == "RUNNING" and row["claim_token"] else None),
                      "Worker never acquired real RUNNING claim", timeout=100, interval=.3)
        worker = next((p for p in workers if p["name"] == job["worker_id"]), None)
        ensure(worker, "Real claim worker_id does not name a guarded worker")
        observed = self.wait(lambda: self.marker(worker, job["id"], stage),
                             "Real worker barrier marker was not observed", timeout=50, interval=1)
        validate_marker(observed, job, worker, stage)
        self.evidence.record("real_job_barrier", job=job, worker=worker, marker=observed)
        return upload, job, worker, observed, filename

    def release(self, worker, job, filename):
        # Clear idle peers first. The claimed terminating worker can exit as soon
        # as its one remaining block is removed; do not require a second exec.
        for peer in self.snapshot(self.pods("media-worker")):
            if peer["uid"] != worker["uid"] and peer["uid"] in self.owned_barrier_pods.get(filename, set()):
                self.hook_file(peer, filename, "remove")
        self.hook_file(worker, filename, "remove")

    def prepare_worker_drain_layout(self):
        self.guard()
        deployment = self.data("get", "deployment", "photo-media-worker")
        self.worker_layout_restore = {
            "uid": deployment["metadata"]["uid"], "replicas": deployment["spec"]["replicas"],
            "affinity": deployment["spec"]["template"]["spec"].get("affinity")}
        ensure(self.worker_layout_restore["replicas"] == 1, "Expected one baseline worker before drain fixture")
        affinity = {"podAntiAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": [
            {"weight": 100, "podAffinityTerm": {"topologyKey": "kubernetes.io/hostname",
              "labelSelector": {"matchLabels": {"app.kubernetes.io/instance": "photo",
                                                 "app.kubernetes.io/component": "media-worker"}}}}]}}
        self.command("patch", "deployment/photo-media-worker", "--type=json", "-p", json.dumps([
            {"op": "test", "path": "/metadata/uid", "value": self.worker_layout_restore["uid"]},
            {"op": "add", "path": "/spec/template/spec/affinity", "value": affinity}]))
        self.command("rollout", "status", "deployment/photo-media-worker", "--timeout=240s", timeout=255)
        self.command("scale", "deployment/photo-media-worker", "--replicas=2")
        self.command("rollout", "status", "deployment/photo-media-worker", "--timeout=240s", timeout=255)
        workers = self.wait(lambda: (pods if len(pods := self.snapshot(self.pods("media-worker"))) == 2
                                    and all(p["ready"] for p in pods) else None),
                            "Two drain fixture workers never became ready", timeout=240)
        ensure(len({p["node"] for p in workers}) == 2, "Drain fixture workers must occupy distinct nodes")
        for worker in workers:
            self.guard_worker(worker)
        name = "photo-extended-worker-drain-" + uuid.uuid4().hex[:8]
        resource = {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget",
                    "metadata": {"name": name, "namespace": NAMESPACE,
                                 "labels": {"app.kubernetes.io/part-of": "photoplatform",
                                            "photoplatform.io/disposable": "true"}},
                    "spec": {"minAvailable": 1, "selector": {"matchLabels": {
                        "app.kubernetes.io/instance": "photo", "app.kubernetes.io/component": "media-worker"}}}}
        path = self.evidence.directory / (name + ".json")
        path.write_text(json.dumps(resource))
        self.command("create", "-f", str(path))
        state = self.data("get", "pdb", name)
        self.temporary_pdb = {"name": name, "uid": state["metadata"]["uid"]}
        self.wait(lambda: self.data("get", "pdb", name).get("status", {}).get("disruptionsAllowed", 0) >= 1,
                  "Worker PDB never permitted one safe eviction", timeout=45)
        self.evidence.record("node_drain_layout_ready", workers=workers, pdb=self.temporary_pdb,
                             worker_nodes_distinct=True, pdb_min_available=1)

    def restore_worker_drain_layout(self):
        self.guard()
        if self.temporary_pdb:
            state = self.data("get", "pdb", self.temporary_pdb["name"])
            ensure(state["metadata"]["uid"] == self.temporary_pdb["uid"], "Temporary worker PDB was replaced")
            self.command("delete", "pdb", self.temporary_pdb["name"], "--wait=true")
            self.temporary_pdb = None
        if self.worker_layout_restore:
            original = self.worker_layout_restore
            self.command("patch", "deployment/photo-media-worker", "--type=json", "-p", json.dumps([
                {"op": "test", "path": "/metadata/uid", "value": original["uid"]},
                {"op": "replace", "path": "/spec/replicas", "value": original["replicas"]},
                {"op": "add", "path": "/spec/template/spec/affinity", "value": original["affinity"] or {}}]))
            self.command("rollout", "status", "deployment/photo-media-worker", "--timeout=240s", timeout=255)
            self.worker_layout_restore = None

    def real_job_over_five_minutes_continuous_claim(self):
        upload, original, worker, marker, filename = self.create_blocked("before_write")
        started, samples, last = time.monotonic(), 0, original
        try:
            while True:
                elapsed = time.monotonic() - started
                last = self.job(upload["mediaId"])
                ensure(last["status"] == "RUNNING" and last["attempt"] == original["attempt"]
                       and last["claim_token"] == original["claim_token"] and last["worker_id"] == worker["name"],
                       "Long job was reclaimed, lost its claim or completed before release")
                ensure(last["lease_remaining_seconds"] > 0 and last["variants"] == 0,
                       "Long-running real claim expired or published metadata through its barrier")
                current = self.guard_worker(worker)
                ensure(current["ready"] and current["containers"] == worker["containers"],
                       "Long-running worker restarted or became unready")
                live_exit = self.pod_health(worker["name"], "live")
                ensure(live_exit == 0, "Long-running worker liveness failed")
                self.evidence.record("long_job_lease_sample", real_elapsed_seconds=round(elapsed, 3),
                                     job=last, worker_uid=current["uid"], worker_ready=current["ready"],
                                     live_exit=live_exit)
                samples += 1
                if elapsed >= 310:
                    break
                time.sleep(min(20, max(.1, 310 - elapsed)))
            elapsed = time.monotonic() - started
            extended = (last["lease_until"] - original["lease_until"]).total_seconds()
            ensure(elapsed > 300 and samples >= 15 and extended >= 300,
                   "Insufficient real wall-clock/continuous lease evidence")
        finally:
            self.release(worker, original, filename)
        self.test.wait(upload, timeout=90)
        done = self.job(upload["mediaId"])
        ensure(done["status"] == "DONE" and done["claim_token"] == original["claim_token"]
               and done["attempt"] == original["attempt"] and done["variants"] == 5,
               "Long job did not complete on its original real claim")
        return {"real_active_seconds": round(elapsed, 3), "samples": samples,
                "lease_extended_seconds": round(extended, 3), "unchanged_claim_and_attempt": True,
                "variants": 5, "barrier": "guarded worker before_write, original session remains active",
                "lease_or_job_status_rewritten": False}

    def sigterm_active_job_graceful_drain(self):
        process, log, node = None, None, None
        try:
            self.prepare_worker_drain_layout()
            upload, original, worker, marker, filename = self.create_blocked("before_write")
            self.guard_worker(worker)
            pod = self.data("get", "pod", worker["name"])
            grace = pod["spec"]["terminationGracePeriodSeconds"]
            ensure(grace == 150, "Expected tested 150-second Kubernetes termination grace")
            node, cluster = worker["node"], os.environ["KIND_CLUSTER"]
            members = subprocess.run(["kind", "get", "nodes", "--name", cluster],
                                     capture_output=True, text=True, timeout=25)
            ensure(members.returncode == 0 and node in members.stdout.splitlines(),
                   "Worker node is not a member of the disposable kind cluster")
            inspected = subprocess.run(["docker", "inspect", "--format", "{{json .Config.Labels}}", node],
                                       capture_output=True, text=True, timeout=25)
            ensure(inspected.returncode == 0 and json.loads(inspected.stdout).get("io.x-k8s.kind.cluster") == cluster,
                   "Worker node Docker container does not belong to this kind cluster")
            node_data = self.data("get", "node", node)
            ensure(not node_data["spec"].get("unschedulable"), "Refusing to alter an already cordoned node")
            ensure("node-role.kubernetes.io/control-plane" not in node_data["metadata"].get("labels", {}),
                   "Full drain may only target an application worker node")
            self.guard()
            self.cordoned_nodes.add(node)  # restore even if drain times out after cordoning
            started = time.monotonic()
            log = (self.evidence.directory / "full-worker-node-drain.log").open("w")
            process = subprocess.Popen(self.kubectl + ["drain", node, "--ignore-daemonsets",
                "--delete-emptydir-data", "--timeout=300s"], stdout=log, stderr=log)
            self.wait(lambda: self.data("get", "pod", worker["name"])["metadata"].get("deletionTimestamp"),
                      "Full node drain did not evict the exact active worker", timeout=90, interval=1)
            terminated = time.monotonic()
            # The claim remains RUNNING after kubelet TERM, while another worker
            # stays ready under its real PDB. Drain runs concurrently with release.
            time.sleep(3)
            active = self.job(upload["mediaId"])
            ensure(active["status"] == "RUNNING" and active["claim_token"] == original["claim_token"],
                   "SIGTERM active-job fixture was not active during node drain")
            survivors = [p for p in self.snapshot(self.pods("media-worker")) if p["node"] != node and p["ready"]]
            ensure(survivors, "Worker PDB drain did not retain a ready worker on another node")
            self.evidence.record("sigterm_active_claim_during_node_drain", original_uid=worker["uid"],
                                 drained_node=node, job=active, grace_seconds=grace, ready_survivors=survivors)
            self.release(worker, original, filename)
            done = self.wait(lambda: (row if (row := self.job(upload["mediaId"])) and row["status"] == "DONE" else None),
                             "SIGTERM original job did not complete during full node drain", timeout=250, interval=.5)
            ensure(done["claim_token"] == original["claim_token"] and done["attempt"] == original["attempt"]
                   and done["variants"] == 5, "SIGTERM did not gracefully complete the original active claim")
            elapsed = time.monotonic() - terminated
            ensure(elapsed < grace, "Active job did not complete inside Kubernetes drain grace")
            result = self.wait(lambda: ({"returncode": process.poll()} if process.poll() is not None else None),
                               "Full node drain did not complete", timeout=320, interval=2)
            ensure(result["returncode"] == 0, "Full node drain returned failure; raw log retained")
            remaining = self.data("get", "pods", "--all-namespaces", "--field-selector", "spec.nodeName=" + node)["items"]
            non_daemons = [p for p in remaining if not any(r.get("kind") == "DaemonSet"
                           for r in p["metadata"].get("ownerReferences", []))]
            ensure(not non_daemons, "Full drain left non-DaemonSet pods on its cordoned node")
            replacement = self.wait(lambda: next((p for p in self.snapshot(self.pods("media-worker"))
                                  if p["uid"] not in {worker["uid"], *[s["uid"] for s in survivors]}
                                  and p["node"] != node and p["ready"]), None),
                                  "Evicted worker did not recover ready on the other worker node", timeout=250)
            self.guard_worker(replacement)
            self.refresh_apis()  # old pod-bound API forwards may end during full drain
            self.test.wait(upload, timeout=30)
            self.evidence.record("full_node_drain_completed", drained_node=node,
                                 kubectl_exit_code=result["returncode"], remaining_non_daemon_pods=0,
                                 remaining_daemon_pods=[p["metadata"]["name"] for p in remaining],
                                 original_worker_uid=worker["uid"], replacement=replacement,
                                 job=done, active_job_drain_seconds=round(elapsed, 3),
                                 full_node_drain_seconds=round(time.monotonic() - started, 3))
            return {"active_before_and_after_term": True, "natural_kubelet_SIGTERM": True,
                    "termination_grace_seconds": grace, "active_job_drain_seconds": round(elapsed, 3),
                    "original_claim_completed": True, "replacement_uid": replacement["uid"], "variants": 5,
                    "full_kubectl_node_drain": True, "drain_exit_code": result["returncode"],
                    "remaining_non_daemon_pods": 0, "worker_pdb_min_available": 1,
                    "ready_worker_replacement_on_other_node": True,
                    "scope": "real full application worker-node drain in isolated three-node kind; AWS drain untested"}
        finally:
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if log:
                log.close()
            if node in self.cordoned_nodes:
                self.guard()
                self.command("uncordon", node)
                self.cordoned_nodes.discard(node)
            self.restore_worker_drain_layout()

    def objects(self, relative_prefix):
        ensure(re.fullmatch(r"media/[1-9][0-9]*/v[1-9][0-9]*/[^/]+/[a-f0-9-]{36}/", relative_prefix),
               "Object inspection prefix must identify one real claim")
        worker = next(p for p in self.snapshot(self.pods("media-worker")) if p["ready"])
        source = ("import json;from app.runtime import Worker,object_key;w=Worker();rows=[];"
                  "pages=w.s3.get_paginator('list_object_versions').paginate(Bucket=w.bucket,Prefix=object_key("
                  + repr(relative_prefix) + "));"
                  "[(rows.extend([{'key':v['Key'],'version_id':v['VersionId'],'delete_marker':False} "
                  "for v in p.get('Versions',[])]),rows.extend([{'key':v['Key'],'version_id':v['VersionId'],"
                  "'delete_marker':True} for v in p.get('DeleteMarkers',[])])) for p in pages];"
                  "print(json.dumps(rows))")
        return json.loads(self.pod_python(worker, source, timeout=50).stdout)

    def post_s3_session_loss_fencing_and_reconciliation(self):
        import psycopg
        from psycopg.rows import dict_row
        upload, original, worker, marker, filename = self.create_blocked("after_write")
        media_id, pid = upload["mediaId"], marker["session_pid"]
        ensure(original["variants"] == 0 and original["processing_status"] != "READY",
               "after_write gate must precede metadata publication")
        with psycopg.connect(self.integration.DB, autocommit=True, connect_timeout=5, row_factory=dict_row) as control:
            attempt = control.execute("SELECT object_prefix FROM media_object_attempts WHERE claim_token=%s AND job_id=%s",
                                      (original["claim_token"], original["id"])).fetchone()
            ensure(attempt, "Real storage attempt was not durably registered")
            prefix = attempt["object_prefix"]
            before = self.objects(prefix)
            ensure(len(before) == 5 and len(marker.get("object_keys", [])) == 5,
                   "Expected five actual pre-publication variant PUTs")
            # Exact session AND exact session advisory lock, under the same DB user.
            target = control.execute("""SELECT a.pid,a.application_name,a.usename,a.client_addr::text,
                 a.backend_start,l.classid::bigint,l.objid::bigint,l.objsubid,l.granted
                 FROM pg_stat_activity a JOIN pg_locks l ON l.pid=a.pid
                 WHERE a.pid=%s AND a.application_name='photoplatform-worker'
                   AND a.usename=current_user AND a.datname=current_database()
                   AND l.locktype='advisory' AND l.granted
                   AND l.classid=(%s::bigint >> 32)::oid AND l.objid=(%s::bigint & 4294967295)::oid
                   AND l.objsubid=1""", (pid, media_id, media_id)).fetchall()
            ensure(len(target) == 1, "Session-loss target is not the exact claimed worker advisory-lock session")
            self.guard_worker(worker)
            self.evidence.record("post_s3_session_verified", target=target[0], marker=marker,
                                 registered_prefix=prefix, actual_object_versions=before, job=original)
            killed = control.execute("SELECT pg_terminate_backend(%s) AS terminated", (pid,)).fetchone()
            ensure(killed["terminated"], "Exact runtime worker PostgreSQL session was not terminated")
            terminated_at = time.monotonic()
            self.wait(lambda: not control.execute("SELECT 1 FROM pg_stat_activity WHERE pid=%s", (pid,)).fetchone(),
                      "Target worker session remains present", timeout=15, interval=.5)
            fenced = self.wait(lambda: self.fenced_marker(worker, original["id"]),
                               "Stale worker did not attest to original-session fencing", timeout=50, interval=1)
            ensure(str(fenced.get("job_id")) == str(original["id"])
                   and str(fenced.get("claim_token")) == str(original["claim_token"])
                   and fenced.get("session_pid") == pid and fenced.get("pod_uid") == worker["uid"],
                   "Fencing marker does not identify the terminated claim/session/pod")
            stale = self.job(media_id)
            ensure(stale["status"] == "RUNNING" and stale["claim_token"] == original["claim_token"]
                   and stale["variants"] == 0 and stale["processing_status"] != "READY",
                   "Stale claim published DONE/metadata after its original session was lost")
            self.evidence.record("post_s3_old_claim_fenced", marker=fenced, job=stale,
                                 seconds_since_session_loss=round(time.monotonic() - terminated_at, 3))
        # Remove the gate after the stale session has demonstrably stopped writing;
        # let real expiry, broker redelivery and the watchdog acquire a new claim.
        self.release(worker, original, filename)
        self.test.wait(upload, timeout=600)
        recovered = self.job(media_id)
        ensure(recovered["status"] == "DONE" and recovered["attempt"] > original["attempt"]
               and recovered["claim_token"] != original["claim_token"] and recovered["variants"] == 5,
               "Session-lost real claim did not naturally recover with a new token")
        with psycopg.connect(self.integration.DB, connect_timeout=5) as conn:
            refs = conn.execute("SELECT object_key FROM media_variants WHERE media_id=%s ORDER BY variant", (media_id,)).fetchall()
        ensure(all(str(original["claim_token"]) not in row[0]
                   and str(recovered["claim_token"]) in row[0] for row in refs),
               "Published variants reference the stale claim")
        self.wait(lambda: not self.objects(prefix), "Abandoned real S3 attempt was not naturally reconciled", timeout=240, interval=10)
        self.evidence.record("post_s3_reconciled", old_prefix=prefix, old_object_versions_remaining=0,
                             job=recovered, published_keys=[r[0] for r in refs],
                             total_seconds=round(time.monotonic() - terminated_at, 3))
        return {"real_variant_puts_before_session_loss": len(before), "exact_original_session_terminated": pid,
                "stale_claim_fenced_before_metadata": True, "new_claim_completed": True,
                "original_attempt": original["attempt"], "recovered_attempt": recovered["attempt"],
                "variants": 5, "abandoned_object_versions_reconciled": True,
                "lease_or_job_status_or_gc_cursor_rewritten": False,
                "natural_recovery_seconds": round(time.monotonic() - terminated_at, 3)}

    def helm(self, *args, timeout=570):
        self.guard()
        result = subprocess.run(["helm", "--kube-context", self.context, "--namespace", NAMESPACE, *args],
                                capture_output=True, text=True, timeout=timeout)
        self.evidence.record("guarded_helm", args=list(args[:4]), returncode=result.returncode,
                             error=self.evidence.safe(result.stderr[-1500:]) if result.returncode else "")
        ensure(result.returncode == 0, "Guarded Helm operation failed: " + self.evidence.safe(result.stderr[-1500:]))
        return result.stdout

    def revision(self):
        history = json.loads(self.helm("history", "photo", "--output", "json"))
        deployed = [x for x in history if x["status"] == "deployed"]
        ensure(len(deployed) == 1, "Helm history must contain one deployed revision")
        return int(deployed[0]["revision"])

    def media_snapshot(self, upload):
        import psycopg
        import requests
        self.refresh_apis()
        forward = next(iter(self.apis.values()))
        response = self.request(forward, "GET", "/api/auth/me", self.test.owner)
        ensure(response.status_code == 200, "Existing JWT was rejected by rollback API")
        delivery = self.request(forward, "GET", f"/api/files/{upload['mediaId']}/url?variant=thumbnail", self.test.owner)
        ensure(delivery.status_code == 200, "Existing media cannot be signed by rollback API")
        media = requests.get(delivery.json()["url"], timeout=25)
        ensure(media.status_code == 200 and media.content, "Existing media is not readable after release change")
        with psycopg.connect(self.integration.DB, connect_timeout=5) as conn:
            rows = conn.execute("SELECT variant,object_key,content_sha256 FROM media_variants WHERE media_id=%s ORDER BY variant",
                                (upload["mediaId"],)).fetchall()
            account = conn.execute("SELECT id FROM user_accounts WHERE email=%s", (f"owner-{self.test.suffix}@test.example",)).fetchone()
        # The runtime role intentionally cannot inspect or change Flyway history.
        # Use the exact disposable DB pod's local migrator only for this SELECT.
        self.guard()
        postgres = self.pods("postgres", dependency=True)
        ensure(len(postgres) == 1 and postgres[0]["metadata"].get("labels", {}).get("dependency") == "postgres",
               "Expected one guarded disposable PostgreSQL pod for schema history")
        migrations = self.command("exec", postgres[0]["metadata"]["name"], "--", "psql",
                                  "-U", "photomigrator", "-d", "generatecloud", "-v", "ON_ERROR_STOP=1",
                                  "-A", "-t", "-c", "SELECT installed_rank,version,description,type,script,checksum,success "
                                  "FROM flyway_schema_history ORDER BY installed_rank").stdout.strip()
        ensure(len(rows) == 5 and account, "Existing database media/account missing after release change")
        pvcs = {x["metadata"]["name"]: x["metadata"]["uid"] for x in self.data("get", "pvc")["items"]}
        ensure(pvcs, "No dependency PVCs present")
        return {"media_id": upload["mediaId"], "thumbnail_sha256": hashlib.sha256(media.content).hexdigest(),
                "variant_rows": rows, "owner_id": account[0], "migration_history": migrations, "pvc_uids": pvcs}

    def assert_api_image(self, expected, source_sha):
        pods = self.refresh_apis()
        for pod in pods:
            ensure(expected in pod["images"] and pod["source_sha"] == source_sha,
                   "Rollback API image/source annotation differs")
            ensure(any(expected.rsplit("@", 1)[1] in c["image_id"] for c in pod["containers"]),
                   "Rollback API runtime digest differs")
        return pods

    def helm_baseline_application_rollback_preserves_media(self):
        self.current_overlay = validate_helm_files(os.environ)
        upload = self.test.ready()
        baseline_data = self.media_snapshot(upload)
        initial_revision = self.revision()
        initial_pods = self.assert_api_image(os.environ["KIND_API_IMAGE"], os.environ["KIND_SOURCE_SHA"])
        baseline_image, baseline_sha = os.environ["KIND_BASELINE_API_IMAGE"], os.environ["KIND_BASELINE_SOURCE_SHA"]
        adapted = json.loads(json.dumps(self.current_overlay))
        repository, digest = baseline_image.split("@")
        adapted["images"]["api"] = {"repository": repository, "digest": digest}
        adapted["release"]["commitSha"] = baseline_sha
        baseline_values = self.evidence.directory / "baseline-rollback-overlay.json"
        # Only non-secret release/runtime parameters; external runtime credentials remain Secret refs.
        baseline_values.write_text(json.dumps(adapted, indent=2) + "\n")
        options = ["--values", os.environ["KIND_HELM_LOCAL_VALUES_FILE"]]
        self.restore_revision = initial_revision
        try:
            self.helm("upgrade", "photo", str(ROOT / "deploy/helm/photoplatform"), *options,
                      "--values", str(baseline_values), "--wait", "--timeout", "8m")
            baseline_revision = self.revision()
            baseline_pods = self.assert_api_image(baseline_image, baseline_sha)
            ensure({p["uid"] for p in baseline_pods}.isdisjoint({p["uid"] for p in initial_pods}),
                   "Baseline API binary did not replace upgraded pods")
            ensure(self.media_snapshot(upload) == baseline_data, "Baseline binary changed/lost upgraded database/media data")
            self.helm("upgrade", "photo", str(ROOT / "deploy/helm/photoplatform"), *options,
                      "--values", os.environ["KIND_HELM_VALUES_FILE"], "--wait", "--timeout", "8m")
            current_revision = self.revision()
            self.restore_revision = current_revision
            current_pods = self.assert_api_image(os.environ["KIND_API_IMAGE"], os.environ["KIND_SOURCE_SHA"])
            ensure(self.media_snapshot(upload) == baseline_data, "Upgrade back to current binary changed persisted data")
            self.helm("rollback", "photo", str(baseline_revision), "--wait", "--timeout", "8m")
            rollback_revision = self.revision()
            rolled_pods = self.assert_api_image(baseline_image, baseline_sha)
            ensure({p["uid"] for p in rolled_pods}.isdisjoint({p["uid"] for p in current_pods}),
                   "Helm rollback did not replace API binary pods")
            ensure(self.media_snapshot(upload) == baseline_data, "Helm baseline application rollback lost data/media")
            self.evidence.record("helm_baseline_rollback_verified", initial_revision=initial_revision,
                                 baseline_revision=baseline_revision, current_revision=current_revision,
                                 rollback_revision=rollback_revision, baseline_source_sha=baseline_sha,
                                 baseline_adapter_sha256=os.environ["KIND_BASELINE_ADAPTER_SHA"],
                                 initial_pods=initial_pods, baseline_pods=baseline_pods,
                                 current_pods=current_pods, rollback_pods=rolled_pods, data=baseline_data)
        finally:
            self.helm("rollback", "photo", str(self.restore_revision), "--wait", "--timeout", "8m")
            self.restore_revision = None
            self.assert_api_image(os.environ["KIND_API_IMAGE"], os.environ["KIND_SOURCE_SHA"])
        ensure(self.media_snapshot(upload) == baseline_data, "Final upgraded release restore changed persisted data")
        return {"initial_revision": initial_revision, "baseline_revision": baseline_revision,
                "current_revision": current_revision, "rolled_back_release_revision": rollback_revision,
                "restored_current_revision": self.revision(), "baseline_source_sha": baseline_sha,
                "baseline_api_image": baseline_image, "baseline_adapter_sha256": os.environ["KIND_BASELINE_ADAPTER_SHA"],
                "binary_digest_changed_by_rollback": True, "existing_jwt_and_private_media_readable": True,
                "five_variant_rows_and_pvc_uids_retained": True, "schema_migrations_during_rollback": 0,
                "scope": "pinned baseline business binary with explicit packaging/probe compatibility adapter",
                "not_validated": "unmodified historical container or arbitrary older schema compatibility"}

    def cleanup(self):
        errors = self.release_owned_barriers()
        for node in list(self.cordoned_nodes):
            try:
                self.guard()
                self.command("uncordon", node)
                self.cordoned_nodes.discard(node)
            except Exception as exc:
                errors.append(self.evidence.safe(str(exc)))
        if self.worker_layout_restore or self.temporary_pdb:
            try:
                self.restore_worker_drain_layout()
            except Exception as exc:
                errors.append(self.evidence.safe(str(exc)))
        if self.restore_revision:
            try:
                self.helm("rollback", "photo", str(self.restore_revision), "--wait", "--timeout", "8m")
            except Exception as exc:
                errors.append(self.evidence.safe(str(exc)))
        if self.hooks_installed:
            try:
                self.guard()
                self.command("set", "env", "deployment/photo-media-worker", "--containers=media-worker",
                             *[k + "-" for k in HOOK_ENV])
                self.command("rollout", "status", "deployment/photo-media-worker", "--timeout=240s", timeout=255)
            except Exception as exc:
                errors.append(self.evidence.safe(str(exc)))
        errors.extend(super().cleanup())
        return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guard-only", action="store_true")
    args = parser.parse_args()
    directory = Path(os.getenv("KIND_EVIDENCE_DIR", "work/kind-evidence"))
    evidence = Evidence(directory / "extended")
    results, harness, cleanup_errors = [], None, []
    try:
        context = validate_environment(os.environ)
        validate_helm_files(os.environ)
        harness = ExtendedHarness(evidence, context)
        harness.guard()
        if args.guard_only:
            evidence.record("extended_guard_only_pass")
            print("Extended disposable kind guard: PASS")
            return 0
        harness.setup_fixtures()
        harness.replicas_images_revision()
        harness.enable_hooks()
        for case in CASES:
            started = time.monotonic()
            evidence.record("case_started", case=case)
            try:
                detail = getattr(harness, case)()
                result = {"case": case, "status": "PASS", "detail": detail}
            except Exception as exc:
                result = {"case": case, "status": "FAIL", "error": evidence.safe(type(exc).__name__ + ": " + str(exc))}
                barrier_errors = harness.release_owned_barriers()
                if barrier_errors:
                    result["barrier_release_errors"] = barrier_errors
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
    summary = {"scope": "disposable kind real extended worker/application rollback acceptance",
               "source_sha": os.getenv("KIND_SOURCE_SHA", ""), "denominator": len(CASES),
               "passed": sum(r["status"] == "PASS" for r in results),
               "failed": sum(r["status"] == "FAIL" for r in results), "skipped": 0,
               "cases": results, "cleanup_errors": cleanup_errors,
               "timeline": "extended/" + evidence.timeline.name,
               "not_validated": ["AWS EKS/IAM/ALB/VPC", "managed dependencies", "public production endpoint",
                                 "unadapted historical binary or arbitrary schema downgrade"]}
    (directory / "kind-extended-summary.json").write_text(evidence.safe(json.dumps(summary, indent=2, default=str)) + "\n")
    print(json.dumps({k: summary[k] for k in ("denominator", "passed", "failed", "skipped")}), flush=True)
    return 0 if summary["passed"] == len(CASES) and not cleanup_errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
