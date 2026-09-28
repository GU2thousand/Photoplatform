"""Container launcher command and fail-closed secret contracts."""
import sys
import unittest
from unittest.mock import patch

from app.secrets import main


class SecretLauncherTests(unittest.TestCase):
    def test_default_command_replaces_the_launcher_process(self):
        with patch.object(sys, "argv", ["secrets"]), patch("app.secrets.load_secret_files"), \
             patch("app.secrets.os.execvp") as execute:
            main()
        execute.assert_called_once_with(sys.executable, [sys.executable, "-m", "app.consumer"])

    def test_arbitrary_encoder_command_preserves_exact_arguments(self):
        command = ["uvicorn", "app.encoder:app", "--host", "0.0.0.0", "--port", "8090"]
        with patch.object(sys, "argv", ["secrets", *command]), patch("app.secrets.load_secret_files"), \
             patch("app.secrets.os.execvp") as execute:
            main()
        execute.assert_called_once_with("uvicorn", command)

    def test_missing_secret_returns_configuration_exit_before_starting_service(self):
        with patch("app.secrets.load_secret_files", side_effect=ValueError("Cannot read mounted secret for ENCODER_TOKEN")), \
             patch("app.secrets.os.execvp") as execute, patch("builtins.print"):
            self.assertEqual(main(), 78)
        execute.assert_not_called()
