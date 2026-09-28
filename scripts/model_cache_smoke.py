#!/usr/bin/env python3
"""Exercise real ML-image volume ownership without downloading/loading CLIP."""
import argparse
import json
from pathlib import Path
import subprocess
import uuid


def docker(*arguments, check=True):
    result = subprocess.run(["docker", *arguments], text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError("Docker filesystem smoke failed: " + result.stderr.strip())
    return result


def mounted(image, volume, code, *, root=False, readonly_volume=False):
    mount = f"type=volume,src={volume},dst=/home/worker/.cache"
    if readonly_volume:
        mount += ",readonly"
    arguments = ["run", "--rm", "--network", "none", "--read-only",
                 "--tmpfs", "/tmp:rw,nosuid,size=16m", "--mount", mount,
                 "--env", "XDG_CACHE_HOME=/home/worker/.cache",
                 "--env", "HF_HOME=/home/worker/.cache/huggingface",
                 "--env", "TORCH_HOME=/home/worker/.cache/torch"]
    if root:
        arguments += ["--user", "0:0"]
    return docker(*arguments, image, "python", "-c", code)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    run_id = uuid.uuid4().hex
    old = "photo-cache-smoke-old-" + run_id
    fresh = "photo-cache-smoke-v2-" + run_id
    volumes = []
    try:
        for name in (old, fresh):
            docker("volume", "create", "--label", "photoplatform.test=model-cache-smoke", name)
            volumes.append(name)
        # A retained pre-upgrade volume is deliberately root-owned. Only these
        # uniquely generated fixture volumes are ever mutated by this script.
        mounted(args.image, old, """
from pathlib import Path
import os
root=Path('/home/worker/.cache')
(root/'retained-marker').write_text('preserve old model cache')
os.chown(root,0,0)
os.chmod(root,0o755)
""", root=True)
        reproduction = mounted(args.image, old, """
import os
from app.model import MODEL_CACHE
assert os.getuid()==10001 and os.getgid()==10001
try:
    MODEL_CACHE.mkdir(parents=True,exist_ok=True)
except PermissionError:
    print('Retained root-owned cache correctly reproduces PermissionError')
else:
    raise AssertionError('Expected retained root-owned volume to reject nonroot writes')
""")
        fresh_result = mounted(args.image, fresh, """
import json,os
from pathlib import Path
from app.model import MODEL_CACHE
assert os.getuid()==10001 and os.getgid()==10001
root=Path('/home/worker/.cache')
assert root.stat().st_uid==10001 and root.stat().st_gid==10001
MODEL_CACHE.mkdir(parents=True,exist_ok=True)
probe=MODEL_CACHE/'filesystem-smoke.json'
probe.write_text(json.dumps({'uid':os.getuid(),'gid':os.getgid()}))
assert json.loads(probe.read_text())=={'uid':10001,'gid':10001}
probe.unlink()
for variable in ('HF_HOME','TORCH_HOME'):
    Path(os.environ[variable]).mkdir(parents=True,exist_ok=True)
print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),'cache':str(MODEL_CACHE),
                  'cache_uid':root.stat().st_uid,'cache_gid':root.stat().st_gid}))
""")
        mounted(args.image, old, """
from pathlib import Path
root=Path('/home/worker/.cache')
assert root.stat().st_uid==0 and root.stat().st_gid==0
assert (root/'retained-marker').read_text()=='preserve old model cache'
assert not (root/'photoplatform-clip').exists()
""", root=True, readonly_volume=True)
        image = json.loads(docker("image", "inspect", args.image).stdout)[0]
        evidence = {"passed": True, "image": args.image, "image_id": image["Id"],
                    "image_user": image["Config"]["User"], "network": "none",
                    "root_filesystem": "read-only", "fresh_cache": json.loads(fresh_result.stdout.strip()),
                    "retained_root_owned_cache_failure_reproduced": bool(reproduction.stdout.strip()),
                    "retained_cache_unchanged": True, "model_downloaded": False,
                    "model_loaded": False, "fixture_volumes_removed": True}
    finally:
        for name in reversed(volumes):
            docker("volume", "rm", name)
    print(json.dumps(evidence, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2) + "\n")


if __name__ == "__main__":
    main()
