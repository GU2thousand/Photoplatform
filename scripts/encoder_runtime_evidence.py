#!/usr/bin/env python3
"""Read-only provenance of the warmed encoder in one fixed Compose project.

The helper reads local probe endpoints and hashes existing checkpoint bytes.
It never reads environment/secrets, imports ML libraries, or downloads/loads a model.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

PROJECT = "photo-cloud-runtime"
EXPECTED_VERSION = "clip-vit-b32-openai-v1"
EXPECTED_SHA256 = "40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af"
EXPECTED_URL = f"https://openaipublic.azureedge.net/clip/models/{EXPECTED_SHA256}/ViT-B-32.pt"
EXPECTED_PATH = "/home/worker/.cache/photoplatform-clip/ViT-B-32.pt"
EXPECTED_PACKAGES = {"torch": "2.14.0+cpu", "torchvision": "0.29.0+cpu",
                     "open-clip-torch": "3.3.0", "Pillow": "12.3.0", "fastapi": "0.141.1"}
HEALTH_FIELDS = {"ready", "modelVersion", "artifactSha256", "expectedArtifactSha256",
                 "provider", "architecture", "weights", "artifactUrl", "artifactVerified"}
IDENTITY_FIELDS = {"container_id", "image_id", "running", "paused", "restarting",
                   "started_at", "pid", "restart_count", "project", "service", "configured_user", "cache_mounts"}
STAT_FIELDS = {"dev", "ino", "size", "mtime_ns"}
RUNTIME_FIELDS = {"health_status", "ready_status", "health", "ready", "path", "file_sha256", "file_size",
                  "file_regular", "file_uid", "file_gid", "uid", "gid", "python", "packages", "stat_before",
                  "stat_after", "platform_system", "platform_machine"}

# Go template deliberately selects no Config.Env, credentials, logs, arguments,
# mount Source paths, or arbitrary labels from Docker's inspect document.
INSPECT_FORMAT = """{"container_id":{{json .Id}},"image_id":{{json .Image}},"running":{{json .State.Running}},"paused":{{json .State.Paused}},"restarting":{{json .State.Restarting}},"started_at":{{json .State.StartedAt}},"pid":{{json .State.Pid}},"restart_count":{{json .RestartCount}},"project":{{json (index .Config.Labels "com.docker.compose.project")}},"service":{{json (index .Config.Labels "com.docker.compose.service")}},"configured_user":{{json .Config.User}},"cache_mounts":[{{range .Mounts}}{{if eq .Destination "/home/worker/.cache"}}{"type":{{json .Type}},"name":{{json .Name}},"destination":{{json .Destination}},"rw":{{json .RW}}}{{end}}{{end}}]}"""

CONTAINER_SAMPLE = r'''
import hashlib, importlib.metadata, json, os, platform, stat, sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

allowed = {'ready','modelVersion','artifactSha256','expectedArtifactSha256','provider',
           'architecture','weights','artifactUrl','artifactVerified'}
def probe(name):
    try:
        response=urlopen('http://127.0.0.1:8090/'+name, timeout=5)
    except HTTPError as error:
        response=error
    with response:
        status=response.code
        data=json.load(response)
    if not isinstance(data,dict) or set(data)!=allowed:
        raise ValueError('Unexpected probe metadata schema')
    return status,data
def file_stat(value):
    return {'dev':value.st_dev,'ino':value.st_ino,'size':value.st_size,'mtime_ns':value.st_mtime_ns}
try:
    health_status,health=probe('health')
    ready_status,ready=probe('readyz')
    path=Path('/home/worker/.cache/photoplatform-clip/ViT-B-32.pt')
    if path.resolve(strict=True)!=path:
        raise ValueError('Checkpoint escapes the fixed cache path through a symlink')
    # No symlink following: the captured path must be the actual regular cache file.
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    digest=hashlib.sha256()
    with os.fdopen(descriptor,'rb') as stream:
        before=os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('Checkpoint is not a regular file')
        for block in iter(lambda:stream.read(1024*1024),b''):
            digest.update(block)
        after_descriptor=os.fstat(stream.fileno())
    after=os.stat(path,follow_symlinks=False)
    if (file_stat(after_descriptor)!=file_stat(after) or not stat.S_ISREG(after.st_mode)
            or path.resolve(strict=True)!=path):
        raise ValueError('Checkpoint path changed while hashing')
    packages={name:importlib.metadata.version(name) for name in
              ('torch','torchvision','open-clip-torch','Pillow','fastapi')}
    print(json.dumps({'health_status':health_status,'ready_status':ready_status,'health':health,'ready':ready,
        'path':str(path),'file_sha256':digest.hexdigest(),'file_size':before.st_size,'file_regular':True,
        'file_uid':before.st_uid,'file_gid':before.st_gid,'uid':os.getuid(),'gid':os.getgid(),
        'python':platform.python_version(),'packages':packages,'stat_before':file_stat(before),
        'stat_after':file_stat(after),'platform_system':platform.system(),'platform_machine':platform.machine()}))
except Exception as error:
    # Endpoint/data/dependency errors can contain sensitive text. Export only their class.
    print(json.dumps({'error_type':type(error).__name__}))
    raise SystemExit(1)
'''


class EvidenceError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise EvidenceError(message)


def validate_identity(identity):
    require(isinstance(identity, dict) and set(identity) == IDENTITY_FIELDS, "Unexpected container identity schema")
    require(identity["project"] == PROJECT and identity["service"] == "encoder", "Encoder scope mismatch")
    require(identity["running"] is True and identity["paused"] is False and identity["restarting"] is False,
            "Encoder container is not stably running")
    require(isinstance(identity["container_id"], str) and re.fullmatch(r"[a-f0-9]{64}", identity["container_id"]) is not None,
            "Invalid encoder container ID")
    require(isinstance(identity["image_id"], str) and re.fullmatch(r"sha256:[a-f0-9]{64}", identity["image_id"]) is not None,
            "Invalid encoder image ID")
    require(type(identity["pid"]) is int and identity["pid"] > 0, "Invalid running container PID")
    require(type(identity["restart_count"]) is int and identity["restart_count"] >= 0, "Invalid restart count")
    require(isinstance(identity["started_at"], str) and identity["started_at"].endswith("Z"), "Invalid container start time")
    require(identity["configured_user"] == "10001:10001", "Encoder container must use UID/GID10001")
    require(identity["cache_mounts"] == [{"type": "volume", "name": PROJECT + "_model-cache-v2",
            "destination": "/home/worker/.cache", "rw": True}], "Encoder does not use the expected v2 cache volume")
    require(identity["cache_mounts"][0]["rw"] is True, "Encoder cache mount must be writable")
    return identity


def validate_runtime(runtime):
    require(isinstance(runtime, dict) and set(runtime) == RUNTIME_FIELDS, "Unexpected encoder runtime schema")
    require(runtime["health_status"] == 200 and type(runtime["health_status"]) is int
            and runtime["ready_status"] == 200 and type(runtime["ready_status"]) is int, "Encoder probes must return HTTP200")
    for name in ("health", "ready"):
        value = runtime[name]
        require(isinstance(value, dict) and set(value) == HEALTH_FIELDS, "Unexpected encoder probe schema")
        require(value["ready"] is True and value["artifactVerified"] is True, "Encoder has not verified and warmed its model")
        require(value["modelVersion"] == EXPECTED_VERSION, "Encoder model version mismatch")
        require(value["artifactSha256"] == EXPECTED_SHA256 and value["expectedArtifactSha256"] == EXPECTED_SHA256,
                "Encoder loaded artifact digest mismatch")
        require(value["provider"] == "OpenAI" and value["architecture"] == "ViT-B-32"
                and value["weights"] == "OpenAI CLIP ViT-B/32" and value["artifactUrl"] == EXPECTED_URL,
                "Encoder artifact provider or source mismatch")
    require(runtime["health"] == runtime["ready"], "Encoder probe identity changed during collection")
    require(runtime["path"] == EXPECTED_PATH and runtime["file_regular"] is True, "Unexpected checkpoint file path or type")
    require(runtime["file_sha256"] == EXPECTED_SHA256, "Actual checkpoint bytes do not match the official artifact")
    require(type(runtime["file_size"]) is int and runtime["file_size"] > 0, "Invalid checkpoint file size")
    require(type(runtime["uid"]) is int and runtime["uid"] == 10001
            and type(runtime["gid"]) is int and runtime["gid"] == 10001, "Encoder evidence must run as UID/GID10001")
    require(type(runtime["file_uid"]) is int and runtime["file_uid"] >= 0
            and type(runtime["file_gid"]) is int and runtime["file_gid"] >= 0, "Invalid checkpoint file ownership")
    require(runtime["platform_system"] == "Linux" and isinstance(runtime["platform_machine"], str)
            and bool(runtime["platform_machine"]), "Encoder evidence requires the CPU Linux image")
    require(isinstance(runtime["python"], str) and re.fullmatch(r"3\.12\.\d+", runtime["python"]) is not None,
            "Encoder runtime does not use Python3.12")
    require(runtime["packages"] == EXPECTED_PACKAGES, "Encoder installed ML versions differ from the committed CPU Linux pins")
    for name in ("stat_before", "stat_after"):
        value = runtime[name]
        require(isinstance(value, dict) and set(value) == STAT_FIELDS
                and all(type(number) is int and number >= 0 for number in value.values()), "Invalid checkpoint stat metadata")
    require(runtime["stat_before"] == runtime["stat_after"] and runtime["stat_before"]["size"] == runtime["file_size"],
            "Checkpoint changed while hashing")
    return runtime


def validate_stability(before, after):
    validate_identity(before)
    validate_identity(after)
    require(before == after, "Encoder container, image, process or restart identity changed during collection")


def run(arguments):
    try:
        result = subprocess.run(arguments, text=True, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        raise EvidenceError("Encoder evidence command unavailable or timed out") from None
    require(result.returncode == 0, "Encoder evidence command failed")
    return result.stdout


def collect(runner=None):
    runner = runner or run
    ids = runner(["docker", "compose", "--project-name", PROJECT, "--profile", "search",
                  "ps", "--status", "running", "--no-trunc", "--quiet", "encoder"]).split()
    require(len(ids) == 1, "Expected exactly one running encoder in the fixed Compose project")
    require(re.fullmatch(r"[a-f0-9]{64}", ids[0]) is not None, "Invalid selected encoder container ID")
    command = ["docker", "inspect", "--type", "container", "--format", INSPECT_FORMAT, ids[0]]
    try:
        before = json.loads(runner(command))
        validate_identity(before)
        require(before["container_id"] == ids[0], "Selected encoder identity mismatch")
        runtime = json.loads(runner(["docker", "exec", ids[0], "python", "-c", CONTAINER_SAMPLE]))
        validate_runtime(runtime)
        after = json.loads(runner(command))
        validate_stability(before, after)
    except (json.JSONDecodeError, TypeError, KeyError, ValueError):
        raise EvidenceError("Encoder evidence returned invalid metadata") from None
    return {"schema_version": 1, "captured_at": datetime.now(timezone.utc).isoformat(),
            "passed": True, "compose_project": PROJECT, "container_before": before,
            "container_after": after, "runtime": runtime,
            "checks": {"warmed_verified_model": True, "actual_checkpoint_sha256_matches": True,
                       "fixed_provider_and_source": True, "pinned_cpu_linux_packages": True,
                       "nonroot_process": True, "checkpoint_stable": True,
                       "container_image_process_and_restart_count_stable": True},
            "helper_downloaded_model": False, "helper_loaded_model": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        evidence = collect()
    except EvidenceError as error:
        parser.exit(1, str(error) + "\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(json.dumps(evidence, indent=2) + "\n")
    temporary.replace(args.output)
    print("Verified warmed encoder identity and official checkpoint bytes; metadata saved")


if __name__ == "__main__":
    main()
