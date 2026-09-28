"""Deterministic ASGI contracts: no Torch imports or downloaded model weights."""
import asyncio
import json
import os
import threading
import unittest
from unittest.mock import patch

from app import encoder
from app.model import MODEL_SHA256, MODEL_VERSION, artifact_metadata


class FakeEncoder:
    model_version = MODEL_VERSION
    artifact_sha256 = MODEL_SHA256
    artifact_verified = True

    def __init__(self, vector=None):
        self.vector = vector if vector is not None else [1.0] + [0.0] * 511
        self.calls = []

    def text(self, text):
        self.calls.append(text)
        return self.vector


async def request(method, path, body=None, authorization=None, raw=False):
    """Exercise HTTP without adding httpx to locked worker dependencies."""
    messages = []
    payload = json.dumps(body).encode() if body is not None else b""
    headers = [(b"content-type", b"application/json")]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "headers": headers,
             "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8000)}

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        messages.append(message)

    await encoder.app(scope, receive, send)
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    data = b"".join(message.get("body", b"") for message in messages
                    if message["type"] == "http.response.body")
    return status, data.decode() if raw else json.loads(data)


class EncoderProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"ENCODER_TOKEN": "test-secret", "CLIP_MODEL_VERSION": MODEL_VERSION,
                                     "CLIP_MODEL_SHA256": MODEL_SHA256})
        env.start()
        self.addCleanup(env.stop)
        secrets = patch("app.encoder.load_secret_files")
        secrets.start()
        self.addCleanup(secrets.stop)

    async def wait_for_initialization(self):
        self.assertTrue(await asyncio.to_thread(encoder.initialization_finished.wait, 2),
                        "mock model initialization did not finish")

    async def encode(self, authorization="Bearer test-secret", version=None):
        return await request("POST", "/encode", {"text": "a photo", "modelVersion":
                             encoder.version if version is None else version}, authorization)

    async def test_liveness_is_available_during_nonblocking_model_load(self):
        release = threading.Event()
        entered = threading.Event()
        fake = FakeEncoder()

        def load():
            entered.set()
            if not release.wait(2):
                raise RuntimeError("mock initialization was not released")
            return fake

        with patch("app.encoder.Encoder", side_effect=load):
            context = encoder.lifespan(encoder.app)
            await asyncio.wait_for(context.__aenter__(), 0.5)
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                self.assertEqual(await request("GET", "/livez"), (200, {"status": "alive"}))
                status, payload = await request("GET", "/readyz")
                self.assertEqual(status, 503)
                self.assertFalse(payload["ready"])
                self.assertEqual((await request("GET", "/health"))[0], 200)
                self.assertEqual((await self.encode())[0], 503)
                release.set()
                await self.wait_for_initialization()
                self.assertEqual((await request("GET", "/readyz"))[0], 200)
                self.assertEqual(len(fake.calls), 1)  # Text warmup completed.
            finally:
                release.set()
                await context.__aexit__(None, None, None)

    async def test_authentication_version_and_cache_contract(self):
        fake = FakeEncoder()
        with patch("app.encoder.Encoder", return_value=fake):
            async with encoder.lifespan(encoder.app):
                await self.wait_for_initialization()
                self.assertEqual((await self.encode(authorization=None))[0], 401)
                self.assertEqual((await self.encode(authorization="Bearer wrong"))[0], 401)
                with patch.dict(os.environ, {"ENCODER_TOKEN": ""}):
                    self.assertEqual((await self.encode())[0], 401)
                self.assertEqual((await self.encode(version="wrong-model"))[0], 409)
                for _ in range(2):
                    status, payload = await self.encode()
                    self.assertEqual(status, 200)
                    self.assertEqual(payload["modelVersion"], encoder.version)
                    self.assertEqual(len(payload["embedding"]), 512)
                self.assertEqual(fake.calls, ["Photoplatform readiness warmup", "a photo"])

    async def test_version_mismatch_is_reported_while_cold(self):
        with patch("app.encoder.Encoder", side_effect=RuntimeError("failed")):
            async with encoder.lifespan(encoder.app):
                await self.wait_for_initialization()
                self.assertEqual((await self.encode(version="wrong-model"))[0], 409)
                self.assertEqual((await self.encode())[0], 503)

    async def test_init_failure_keeps_liveness_and_sanitizes_logs(self):
        sensitive = "token=do-not-log-secret https://user:password@example.invalid"
        with patch("app.encoder.Encoder", side_effect=RuntimeError(sensitive)), \
             self.assertLogs("app.encoder", level="ERROR") as logs:
            async with encoder.lifespan(encoder.app):
                await self.wait_for_initialization()
                self.assertEqual((await request("GET", "/livez"))[0], 200)
                self.assertEqual((await request("GET", "/readyz"))[0], 503)
                self.assertEqual((await request("GET", "/health"))[1],
                                 {"ready": False, "modelVersion": encoder.version, **artifact_metadata()})
        self.assertIn("RuntimeError", " ".join(logs.output))
        self.assertNotIn(sensitive, " ".join(logs.output))
        self.assertNotIn("password", " ".join(logs.output))

    async def test_secret_loading_failure_cannot_publish_ready_model(self):
        with patch("app.encoder.load_secret_files", side_effect=ValueError("sensitive contents")), \
             patch("app.encoder.Encoder") as factory:
            async with encoder.lifespan(encoder.app):
                await self.wait_for_initialization()
                factory.assert_not_called()
                self.assertEqual((await request("GET", "/readyz"))[0], 503)

    async def test_warmup_rejects_bad_dimensions_nonfinite_and_unnormalized_vectors(self):
        for vector in ([1.0] * 511, [float("nan")] + [0.0] * 511,
                       [float("inf")] + [0.0] * 511, [0.0] * 512,
                       [2.0] + [0.0] * 511, [True] + [0.0] * 511):
            with self.subTest(first=vector[0], dimensions=len(vector)), \
                 patch("app.encoder.Encoder", return_value=FakeEncoder(vector)):
                async with encoder.lifespan(encoder.app):
                    await self.wait_for_initialization()
                    self.assertEqual((await request("GET", "/readyz"))[0], 503)
                    self.assertEqual((await self.encode())[0], 503)

    async def test_late_initialization_does_not_publish_after_shutdown(self):
        release = threading.Event()

        def load():
            release.wait(2)
            return FakeEncoder()

        with patch("app.encoder.Encoder", side_effect=load):
            async with encoder.lifespan(encoder.app):
                finished = encoder.initialization_finished
                self.assertEqual((await request("GET", "/readyz"))[0], 503)
            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 2))
            self.assertEqual((await request("GET", "/readyz"))[0], 503)

    async def test_cache_is_cleared_when_model_lifecycle_changes(self):
        for value in (1.0, -1.0):
            fake = FakeEncoder([value] + [0.0] * 511)
            with patch("app.encoder.Encoder", return_value=fake):
                async with encoder.lifespan(encoder.app):
                    await self.wait_for_initialization()
                    self.assertEqual((await self.encode())[1]["embedding"][0], value)
                    self.assertEqual(len(fake.calls), 2)

    async def test_unsupported_configured_version_stays_cold_before_loading_model(self):
        with patch.dict(os.environ, {"CLIP_MODEL_VERSION": "relabelled-model"}), \
             patch("app.encoder.Encoder") as factory:
            async with encoder.lifespan(encoder.app):
                await self.wait_for_initialization()
                factory.assert_not_called()
                status, health = await request("GET", "/readyz")
                self.assertEqual(status, 503)
                self.assertEqual(health["modelVersion"], MODEL_VERSION)
                self.assertIsNone(health["artifactSha256"])
                self.assertFalse(health["artifactVerified"])
                self.assertEqual(health["expectedArtifactSha256"], MODEL_SHA256)

    async def test_candidate_artifact_mismatch_is_rejected_before_warmup(self):
        for attribute, value in (("model_version", "relabelled"),
                                 ("artifact_sha256", "0" * 64), ("artifact_verified", False)):
            with self.subTest(attribute=attribute):
                fake = FakeEncoder()
                setattr(fake, attribute, value)
                with patch("app.encoder.Encoder", return_value=fake):
                    async with encoder.lifespan(encoder.app):
                        await self.wait_for_initialization()
                        self.assertEqual((await request("GET", "/readyz"))[0], 503)
                        self.assertFalse(fake.calls)

    async def test_health_provenance_and_prometheus_readiness_and_mismatch_counter(self):
        def counter_value(text):
            return float(next(line.split()[1] for line in text.splitlines()
                              if line.startswith("encoder_model_version_mismatches_total ")))

        fake = FakeEncoder()
        with patch("app.encoder.Encoder", return_value=fake):
            async with encoder.lifespan(encoder.app):
                await self.wait_for_initialization()
                status, health = await request("GET", "/readyz")
                self.assertEqual(status, 200)
                self.assertEqual(health["artifactSha256"], MODEL_SHA256)
                self.assertTrue(health["artifactVerified"])
                self.assertEqual(health["provider"], "OpenAI")
                self.assertEqual(health["architecture"], "ViT-B-32")
                status, before = await request("GET", "/metrics", raw=True)
                self.assertEqual(status, 200)
                self.assertIn(f'encoder_model_ready{{model_version="{MODEL_VERSION}"}} 1.0', before)
                self.assertEqual((await self.encode(version="different-model"))[0], 409)
                _, after = await request("GET", "/metrics", raw=True)
                self.assertEqual(counter_value(after), counter_value(before) + 1)
            _, after_shutdown = await request("GET", "/metrics", raw=True)
            self.assertIn(f'encoder_model_ready{{model_version="{MODEL_VERSION}"}} 0.0', after_shutdown)


if __name__ == "__main__":
    unittest.main()
