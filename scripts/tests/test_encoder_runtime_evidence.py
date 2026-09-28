"""Evidence validation contracts using synthetic records, no Docker or model IO."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import ast
import io
import json
import unittest
from unittest.mock import Mock

from scripts import encoder_runtime_evidence as evidence


EXPECTED_SHA = "40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af"
OFFICIAL_URL = "https://openaipublic.azureedge.net/clip/models/" + EXPECTED_SHA + "/ViT-B-32.pt"


def identity():
    return {"container_id": "b" * 64, "image_id": "sha256:" + "a" * 64,
            "running": True, "paused": False, "restarting": False,
            "started_at": "2026-09-28T12:00:00.000000000Z", "pid": 4321,
            "restart_count": 0, "project": "photo-cloud-runtime", "service": "encoder",
            "configured_user": "10001:10001", "cache_mounts": [{"type": "volume",
                "name": "photo-cloud-runtime_model-cache-v2", "destination": "/home/worker/.cache",
                "rw": True}]}


def runtime():
    health = {"ready": True, "artifactVerified": True,
              "modelVersion": "clip-vit-b32-openai-v1", "artifactSha256": EXPECTED_SHA,
              "expectedArtifactSha256": EXPECTED_SHA, "provider": "OpenAI",
              "architecture": "ViT-B-32", "weights": "OpenAI CLIP ViT-B/32",
              "artifactUrl": OFFICIAL_URL}
    stat = {"dev": 11, "ino": 22, "size": 12345, "mtime_ns": 1750000000000000000}
    return {"health_status": 200, "ready_status": 200, "health": health,
            "ready": deepcopy(health), "path": "/home/worker/.cache/photoplatform-clip/ViT-B-32.pt",
            "file_sha256": EXPECTED_SHA, "file_size": 12345, "file_regular": True,
            "file_uid": 10001, "file_gid": 10001,
            "uid": 10001, "gid": 10001, "platform_system": "Linux", "platform_machine": "x86_64",
            "python": "3.12.12", "packages": {"torch": "2.14.0+cpu", "torchvision": "0.29.0+cpu",
                "open-clip-torch": "3.3.0", "Pillow": "12.3.0", "fastapi": "0.141.1"},
            "stat_before": stat, "stat_after": deepcopy(stat)}


class EncoderRuntimeEvidenceTests(unittest.TestCase):
    def test_constants_pin_project_version_and_official_checkpoint(self):
        self.assertEqual(evidence.PROJECT, "photo-cloud-runtime")
        self.assertEqual(evidence.EXPECTED_VERSION, "clip-vit-b32-openai-v1")
        self.assertEqual(evidence.EXPECTED_SHA256, EXPECTED_SHA)

    def test_valid_synthetic_identity_and_loaded_runtime_are_accepted(self):
        evidence.validate_identity(identity())
        evidence.validate_runtime(runtime())

    def test_identity_requires_exact_container_scope_and_nonroot_user(self):
        cases = {"project": ("other-project", ""), "service": ("worker", "embedding-worker"),
                 "configured_user": ("0:0", "root", "10001"),
                 "container_id": ("short", "g" * 64, "sha256:" + "b" * 64),
                 "image_id": ("latest", "a" * 64, "sha256:" + "z" * 64),
                 "pid": (0, -1, True, "4321"), "restart_count": (-1, True, "0"),
                 "running": (False, "true", 1), "paused": (True, "false", 0),
                 "restarting": (True, "false", 0), "started_at": ("", None)}
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    record = identity()
                    record[field] = value
                    with self.assertRaises(evidence.EvidenceError):
                        evidence.validate_identity(record)
        for field in identity():
            with self.subTest(missing=field):
                record = identity()
                del record[field]
                with self.assertRaises(evidence.EvidenceError):
                    evidence.validate_identity(record)

    def test_cache_mount_requires_exact_v2_named_volume_and_no_extra_mounts(self):
        for field, value in (("type", "bind"), ("name", "photo-cloud-runtime_model-cache"),
                             ("destination", "/tmp"), ("rw", False), ("rw", "true")):
            with self.subTest(field=field):
                record = identity()
                record["cache_mounts"][0][field] = value
                with self.assertRaises(evidence.EvidenceError):
                    evidence.validate_identity(record)
        for mounts in ([], None, [identity()["cache_mounts"][0]] * 2):
            with self.subTest(mounts=mounts):
                record = identity()
                record["cache_mounts"] = mounts
                with self.assertRaises(evidence.EvidenceError):
                    evidence.validate_identity(record)

    def test_cold_health_and_readiness_cannot_establish_loaded_model_evidence(self):
        for endpoint in ("health", "ready"):
            for field, value in (("ready", False), ("artifactVerified", False),
                                 ("artifactSha256", None), ("modelVersion", "other-model"),
                                 ("artifactSha256", "0" * 64), ("expectedArtifactSha256", "0" * 64),
                                 ("provider", "other-provider"), ("architecture", "ViT-L-14"),
                                 ("weights", "arbitrary"), ("artifactUrl", "https://example.invalid/model")):
                with self.subTest(endpoint=endpoint, field=field):
                    record = runtime()
                    record[endpoint][field] = value
                    with self.assertRaises(evidence.EvidenceError):
                        evidence.validate_runtime(record)
            for field in runtime()[endpoint]:
                with self.subTest(endpoint=endpoint, missing=field):
                    record = runtime()
                    del record[endpoint][field]
                    with self.assertRaises(evidence.EvidenceError):
                        evidence.validate_runtime(record)
        for field in ("health_status", "ready_status"):
            for value in (503, 500, None, "200", True):
                with self.subTest(field=field, value=value):
                    record = runtime()
                    record[field] = value
                    with self.assertRaises(evidence.EvidenceError):
                        evidence.validate_runtime(record)

    def test_file_path_digest_size_runtime_user_and_package_provenance_are_required(self):
        cases = {"path": ("/tmp/another-checkpoint.pt", "", None),
                 "file_sha256": ("0" * 64, "", None), "file_size": (0, -1, True, "12345"),
                 "uid": (0, "10001", True), "gid": (0, "10001", True), "python": ("", None),
                 "file_uid": (-1, "10001", True), "file_gid": (-1, "10001", True),
                 "file_regular": (False, 1, "true"), "platform_system": ("Darwin", "", None),
                 "platform_machine": ("", None)}
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    record = runtime()
                    record[field] = value
                    with self.assertRaises(evidence.EvidenceError):
                        evidence.validate_runtime(record)
        for package in runtime()["packages"]:
            for mode in ("missing", "empty", "wrong-type", "wrong-version"):
                with self.subTest(package=package, mode=mode):
                    record = runtime()
                    if mode == "missing":
                        del record["packages"][package]
                    else:
                        record["packages"][package] = ("" if mode == "empty" else
                                                        "1.0.0" if mode == "wrong-version" else 123)
                    with self.assertRaises(evidence.EvidenceError):
                        evidence.validate_runtime(record)
        record = runtime()
        record["packages"]["unexpected"] = "1.0"
        with self.assertRaises(evidence.EvidenceError):
            evidence.validate_runtime(record)

    def test_checkpoint_stat_changes_or_size_disagreement_invalidate_evidence(self):
        for field in ("dev", "ino", "size", "mtime_ns"):
            with self.subTest(changed=field):
                record = runtime()
                record["stat_after"][field] += 1
                with self.assertRaises(evidence.EvidenceError):
                    evidence.validate_runtime(record)
        record = runtime()
        record["stat_before"]["size"] = record["stat_after"]["size"] = record["file_size"] + 1
        with self.assertRaises(evidence.EvidenceError):
            evidence.validate_runtime(record)
        for field in ("stat_before", "stat_after"):
            with self.subTest(missing=field):
                record = runtime()
                record[field] = None
                with self.assertRaises(evidence.EvidenceError):
                    evidence.validate_runtime(record)

    def test_stability_accepts_identical_valid_identity_without_mutation(self):
        before = identity()
        after = deepcopy(before)
        evidence.validate_stability(before, after)
        self.assertEqual(before, identity())
        self.assertEqual(after, before)

    def test_container_image_pid_and_restart_rotation_invalidate_stability(self):
        cases = {"container_id": "c" * 64, "image_id": "sha256:" + "d" * 64,
                 "started_at": "2026-09-28T12:01:00.000000000Z", "pid": 4322,
                 "restart_count": 1, "running": False, "paused": True, "restarting": True,
                 "project": "other-project", "service": "worker", "configured_user": "0:0"}
        for field, value in cases.items():
            with self.subTest(field=field):
                before = identity()
                after = deepcopy(before)
                after[field] = value
                with self.assertRaises(evidence.EvidenceError):
                    evidence.validate_stability(before, after)
        invalid = identity()
        invalid["configured_user"] = "0:0"
        with self.assertRaises(evidence.EvidenceError):
            evidence.validate_stability(invalid, deepcopy(invalid))

    def test_invalid_records_never_echo_secret_values_or_extra_environment(self):
        sentinel = "ENCODER_TOKEN=do-not-emit-secret"
        bad_identity = identity()
        bad_identity["configured_user"] = sentinel
        bad_identity["env"] = [sentinel]
        bad_runtime = runtime()
        bad_runtime["health"]["artifactSha256"] = sentinel
        bad_runtime["env"] = [sentinel]
        for validator, record in ((evidence.validate_identity, bad_identity),
                                  (evidence.validate_runtime, bad_runtime)):
            with self.subTest(validator=validator.__name__):
                output = io.StringIO()
                with redirect_stdout(output), redirect_stderr(output), \
                     self.assertRaises(evidence.EvidenceError) as caught:
                    validator(record)
                self.assertNotIn(sentinel, str(caught.exception))
                self.assertNotIn("do-not-emit-secret", output.getvalue())

    def test_raw_health_and_readiness_reject_unrecognized_secret_bearing_fields(self):
        sentinel = "ENCODER_TOKEN=do-not-emit-secret"
        for endpoint in ("health", "ready"):
            with self.subTest(endpoint=endpoint):
                record = runtime()
                record[endpoint]["env"] = {"ENCODER_TOKEN": sentinel}
                with self.assertRaises(evidence.EvidenceError) as caught:
                    evidence.validate_runtime(record)
                self.assertNotIn(sentinel, str(caught.exception))

    def test_collect_uses_exactly_four_readonly_docker_commands_in_fixed_scope(self):
        before = identity()
        runner = Mock(side_effect=[before["container_id"] + "\n", json.dumps(before),
                                   json.dumps(runtime()), json.dumps(before)])
        result = evidence.collect(runner=runner)
        commands = [call.args[0] for call in runner.call_args_list]
        self.assertEqual(len(commands), 4)
        self.assertEqual(commands[0], ["docker", "compose", "--project-name", "photo-cloud-runtime",
            "--profile", "search", "ps", "--status", "running", "--no-trunc", "--quiet", "encoder"])
        self.assertEqual(commands[1][:5], ["docker", "inspect", "--type", "container", "--format"])
        self.assertEqual(commands[1][-1], before["container_id"])
        self.assertEqual(commands[3], commands[1])
        self.assertEqual(commands[2][:5], ["docker", "exec", before["container_id"], "python", "-c"])
        selected_inspect = commands[1][5]
        self.assertNotIn("Config.Env", selected_inspect)
        self.assertNotIn(".Source", selected_inspect)
        self.assertNotIn(".Args", selected_inspect)
        self.assertNotIn(".Config.Cmd", selected_inspect)
        sample = commands[2][5]
        tree = ast.parse(sample)
        imported = set()
        probes = []
        urls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "probe":
                probes.append(node.args[0].value)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("http"):
                urls.append(node.value)
        self.assertFalse(imported & {"torch", "torchvision", "open_clip"})
        self.assertEqual(sorted(probes), ["health", "readyz"])
        self.assertEqual(urls, ["http://127.0.0.1:8090/"])
        self.assertNotIn("os.environ", sample)
        self.assertNotIn("os.getenv", sample)
        self.assertNotIn("torch.load", sample)
        self.assertNotIn("create_model", sample)
        self.assertNotIn("/encode", sample)
        self.assertIn("os.O_RDONLY|os.O_NOFOLLOW", sample)
        self.assertTrue(result["passed"])
        self.assertFalse(result["helper_downloaded_model"])
        self.assertFalse(result["helper_loaded_model"])
        self.assertEqual(result["container_before"], result["container_after"])

    def test_collect_rejects_container_rotation_after_readonly_sample(self):
        before = identity()
        after = deepcopy(before)
        after["container_id"] = "c" * 64
        runner = Mock(side_effect=[before["container_id"] + "\n", json.dumps(before),
                                   json.dumps(runtime()), json.dumps(after)])
        with self.assertRaises(evidence.EvidenceError) as caught:
            evidence.collect(runner=runner)
        self.assertEqual(runner.call_count, 4)
        self.assertNotIn(after["container_id"], str(caught.exception))


if __name__ == "__main__":
    unittest.main()
