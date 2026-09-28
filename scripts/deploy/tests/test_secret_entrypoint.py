"""Verify CSI contents never become shell commands and failures stop launch."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[3] / "backend" / "start.sh"


class SecretEntrypointTests(unittest.TestCase):
    def launch(self, env, command):
        base = {"PATH": os.environ["PATH"]}
        base.update(env)
        return subprocess.run(["sh",str(SCRIPT),*command],env=base,capture_output=True,text=True)

    def test_multiline_secret_is_literal_and_command_exit_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            secret = Path(root)/"pem"
            marker = Path(root)/"must-not-exist"
            contents=f"-----BEGIN KEY-----\n$(touch {marker})\n`touch {marker}`\n-----END KEY-----"
            secret.write_text(contents)
            result=self.launch({"CDN_PRIVATE_KEY_PEM_FILE":str(secret)},["sh","-c","printf '%s' \"$CDN_PRIVATE_KEY_PEM\"; exit 7"])
            self.assertEqual(result.returncode,7)
            self.assertEqual(result.stdout,contents)
            self.assertFalse(marker.exists())

    def test_missing_file_refuses_application_launch_without_exposing_value(self):
        result=self.launch({"APP_JWT_SECRET_FILE":"/nonexistent/test-secret"},["echo","launched"])
        self.assertNotEqual(result.returncode,0)
        self.assertNotIn("launched",result.stdout)

    def test_conflicting_direct_and_file_secret_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            secret=Path(root)/"jwt";secret.write_text("file-value")
            result=self.launch({"APP_JWT_SECRET_FILE":str(secret),"APP_JWT_SECRET":"direct-value"},["echo","launched"])
            self.assertNotEqual(result.returncode,0)
            self.assertNotIn("file-value",result.stderr)
            self.assertNotIn("direct-value",result.stderr)

    def test_local_storage_credentials_are_explicitly_loaded(self):
        with tempfile.TemporaryDirectory() as root:
            secret=Path(root)/"access";secret.write_text("minio-key")
            result=self.launch({"STORAGE_ACCESS_KEY_FILE":str(secret)},["sh","-c","test \"$STORAGE_ACCESS_KEY\" = minio-key"])
            self.assertEqual(result.returncode,0,result.stderr)


if __name__=="__main__": unittest.main()
