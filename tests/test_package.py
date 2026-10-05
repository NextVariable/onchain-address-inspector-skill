"""Execute a copied installation outside the checkout with the actual CLI."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_http_transport import endpoint
from test_query import A

SKILL = Path(__file__).resolve().parents[1] / "skills" / "onchain-address-inspector"


class InstalledPackageTests(unittest.TestCase):
    def run_installed(self, args):
        with tempfile.TemporaryDirectory() as directory:
            installed = Path(directory) / "onchain-address-inspector"
            shutil.copytree(SKILL, installed, ignore=shutil.ignore_patterns("__pycache__"))
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            return subprocess.run(
                [sys.executable, str(installed / "scripts" / "query.py"), *args],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )

    def test_copied_installation_completes_real_local_http_query(self):
        with endpoint() as (url, state):
            process = self.run_installed(
                [
                    "--address",
                    A,
                    "--start-block",
                    "1",
                    "--end-block",
                    "2",
                    "--rpc",
                    url,
                    "--transport",
                    "http",
                    "--progress",
                ]
            )
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["metrics"]["success"], 1)
        self.assertEqual(
            result["metrics"]["native_value_successful_top_level_only"]["in_wei"], str(10**18)
        )
        self.assertEqual(state["requests"], 7)
        self.assertTrue(all("progress" in json.loads(line) for line in process.stderr.splitlines()))

    def test_installed_help_requires_neither_network_nor_credentials(self):
        process = self.run_installed(["--help"])
        self.assertEqual(process.returncode, 0)
        self.assertIn("--total-timeout", process.stdout)
        self.assertIn("--transport", process.stdout)

    def test_invalid_cli_still_returns_redacted_json_and_exit_two(self):
        for args in [[], ["--address", A, "--total-timeout", "secret"], ["--unknown", "secret"]]:
            with self.subTest(args=args):
                process = self.run_installed(args)
                self.assertEqual(process.returncode, 2)
                self.assertNotIn("secret", process.stdout + process.stderr)
                result = json.loads(process.stdout)
                self.assertFalse(result["coverage"]["complete"])
                self.assertIsNone(result["metrics"])
                self.assertTrue(result["errors"])
                self.assertIn("category", result["errors"][0])
