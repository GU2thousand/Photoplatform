"""Pinned artifact verification with fixture bytes, no ML imports or network."""
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from app import model


class ModelArtifactTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "fixture.pt"
        self.fixture = b"verified model fixture without real model weights"
        self.fixture_digest = hashlib.sha256(self.fixture).hexdigest()
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        cache = patch("app.model.MODEL_CACHE", self.root / "cache")
        cache.start()
        self.addCleanup(cache.stop)

    def test_registry_constants_pin_official_checkpoint_and_canonical_version(self):
        self.assertEqual(model.MODEL_VERSION, "clip-vit-b32-openai-v1")
        self.assertEqual(model.MODEL_SHA256,
                         "40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af")
        self.assertEqual(model.MODEL_URL, "https://openaipublic.azureedge.net/clip/models/" +
                         model.MODEL_SHA256 + "/ViT-B-32.pt")

    def test_unsupported_version_and_arbitrary_expected_hash_fail_before_io(self):
        for env in ({"CLIP_MODEL_VERSION": "renamed-other-model"}, {"CLIP_MODEL_SHA256": "0" * 64}):
            with self.subTest(env=env), patch.dict(os.environ, env), \
                 patch("app.model.urlopen") as download, patch("app.model._load_libraries") as libraries, \
                 self.assertRaises(ValueError):
                model.Encoder()
            download.assert_not_called()
            libraries.assert_not_called()

    def test_missing_explicit_checkpoint_has_no_download_or_ml_fallback(self):
        with patch.dict(os.environ, {"CLIP_MODEL_PATH": str(self.path)}), \
             patch("app.model.urlopen") as download, patch("app.model._load_libraries") as libraries, \
             self.assertRaises(FileNotFoundError):
            model.Encoder()
        download.assert_not_called()
        libraries.assert_not_called()

    def test_tampered_explicit_file_rejects_before_ml_deserialization(self):
        self.path.write_bytes(self.fixture)
        with patch.dict(os.environ, {"CLIP_MODEL_PATH": str(self.path)}), \
             patch("app.model.urlopen") as download, patch("app.model._load_libraries") as libraries, \
             self.assertRaises(ValueError):
            model.Encoder()
        download.assert_not_called()
        libraries.assert_not_called()

    def test_sha256_streams_complete_file(self):
        payload = self.fixture * 50000
        self.path.write_bytes(payload)
        self.assertEqual(model._sha256(self.path), hashlib.sha256(payload).hexdigest())

    def test_verified_fixture_loads_local_path_instead_of_floating_pretrained_tag(self):
        self.path.write_bytes(self.fixture)
        clip, torch = MagicMock(), MagicMock()
        loaded, preprocess = MagicMock(), MagicMock()
        clip.create_model_and_transforms.return_value = (loaded, None, preprocess)
        # Substitute the expected digest solely for deterministic fixture bytes.
        # The registry-constant test independently pins the production digest.
        with patch("app.model.MODEL_SHA256", self.fixture_digest), \
             patch.dict(os.environ, {"CLIP_MODEL_PATH": str(self.path)}), \
             patch("app.model._load_libraries", return_value=(clip, torch)), \
             patch("app.model.urlopen") as download:
            encoder = model.Encoder()
        download.assert_not_called()
        args, kwargs = clip.create_model_and_transforms.call_args
        self.assertEqual(args, ("ViT-B-32",))
        self.assertEqual(kwargs["pretrained"], str(self.path))
        self.assertTrue(kwargs["require_pretrained"])
        self.assertTrue(kwargs["force_quick_gelu"])
        self.assertFalse(kwargs["weights_only"])  # Verified official TorchScript format.
        self.assertEqual(encoder.artifact_sha256, self.fixture_digest)
        self.assertEqual(encoder.model_version, model.MODEL_VERSION)
        self.assertTrue(encoder.artifact_verified)
        loaded.eval.assert_called_once_with()

    def test_default_download_uses_fixed_url_and_verified_cache_then_reuses_it(self):
        with patch("app.model.MODEL_SHA256", self.fixture_digest), \
             patch("app.model.urlopen", return_value=io.BytesIO(self.fixture)) as download:
            result = model.verified_checkpoint()
            self.assertEqual(result.read_bytes(), self.fixture)
            self.assertEqual(result.parent, model.MODEL_CACHE)
            self.assertEqual(model.verified_checkpoint(), result)
        download.assert_called_once_with(model.MODEL_URL, timeout=30)
        self.assertFalse(list(result.parent.glob(".download-*")))

    def test_tampered_cached_checkpoint_fails_without_replacing_or_redownloading(self):
        model.MODEL_CACHE.mkdir()
        cached = model.MODEL_CACHE / "ViT-B-32.pt"
        cached.write_bytes(b"tampered")
        with patch("app.model.urlopen") as download, \
             patch("app.model._load_libraries") as libraries, self.assertRaises(ValueError):
            model.Encoder()
        download.assert_not_called()
        libraries.assert_not_called()
        self.assertEqual(cached.read_bytes(), b"tampered")

    def test_failed_download_checksum_leaves_no_loadable_or_partial_artifact(self):
        with patch("app.model.urlopen", return_value=io.BytesIO(b"tampered")), \
             patch("app.model._load_libraries") as libraries, self.assertRaises(ValueError):
            model.Encoder()
        libraries.assert_not_called()
        self.assertFalse((model.MODEL_CACHE / "ViT-B-32.pt").exists())
        self.assertFalse(list(model.MODEL_CACHE.glob(".download-*")))


if __name__ == "__main__":
    unittest.main()
