"""Failure contracts exercised independently of live RPC availability."""

import contextlib
import io
import json
import subprocess
import time
import unittest
from unittest.mock import patch

from test_query import A, Fake, q, rpc_client, transaction


class AdversarialTests(unittest.TestCase):
    def args(self, url="https://rpc.invalid"):
        return q.parser().parse_args(
            ["--address", A, "--start-block", "1", "--end-block", "2", "--rpc", url]
        )

    def test_invalid_rpc_urls_stop_before_any_request(self):
        urls = [
            "https://rpc.invalid:secret-port/key",
            "https://rpc.invalid:65536/key",
            "https://rpc.invalid:0/key",
            "https://rpc.invalid/key#secret",
            "https://rpc.invalid/\tsecret",
            "https://rpc.invalid/ secret",
            "https://rpc.invalid/\x00secret",
            "https://rpc.invalid/\x7fsecret",
            "https://[broken/key",
            "https://rpc.invalid\nsecret",
            "file:///secret",
        ]
        for url in urls:
            with self.subTest(url=repr(url)):
                fake = Fake()
                result = q.scan(self.args(url), fake)
                self.assertFalse(result["coverage"]["complete"])
                self.assertEqual(fake.calls, [])
                self.assertNotIn("secret", json.dumps(result))

    def test_impossible_timestamp_invalidates_even_empty_block(self):
        class InvalidTimestamp(Fake):
            def call(self, method, params):
                result = super().call(method, params)
                if method == "eth_getBlockByNumber":
                    result["timestamp"] = hex(2**256 - 1)
                    result["transactions"] = []
                return result

        result = q.scan(self.args(), InvalidTimestamp())
        self.assertFalse(result["coverage"]["complete"])
        self.assertIsNone(result["metrics"])
        self.assertEqual(result["coverage"]["failed_blocks"], [1, 2])

    def test_rpc_error_requires_matching_envelope_and_integer_code(self):
        valid = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32000, "message": "secret"},
        }
        cases = []
        for field, value in [
            ("jsonrpc", None),
            ("jsonrpc", "1.0"),
            ("id", 2),
            ("id", True),
        ]:
            cases.append({**valid, field: value})
        for code in [True, "secret", None, 3.5]:
            cases.append({**valid, "error": {"code": code, "message": "secret"}})
        for case in cases:
            response = subprocess.CompletedProcess([], 0, json.dumps(case) + "\n200", "")
            with (
                self.subTest(case=case),
                patch.object(rpc_client.subprocess, "run", return_value=response) as run,
            ):
                with self.assertRaises(q.QueryError) as error:
                    q.Client("https://rpc.invalid", retries=2).call("eth_chainId", [])
                self.assertEqual(error.exception.category, "invalid_response")
                self.assertEqual(run.call_count, 1)
                self.assertNotIn("secret", str(error.exception))

    def test_retry_backoff_keeps_attempt_evidence_at_deadline(self):
        response = subprocess.CompletedProcess([], 28, "", "secret")
        clock = [0.0]

        def advance(seconds):
            clock[0] += seconds

        # A deterministic clock isolates backoff semantics from CI scheduling.
        # Actual wall-clock deadlines are checked separately with local sockets.
        with (
            patch.object(rpc_client.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(rpc_client.time, "sleep", side_effect=advance),
            patch.object(rpc_client.subprocess, "run", return_value=response),
        ):
            client = q.Client("https://rpc.invalid", retries=2, total_timeout=0.02)
            with self.assertRaises(q.QueryError) as error:
                client.call("eth_chainId", [])
        self.assertEqual(error.exception.category, "deadline_exceeded")
        self.assertEqual(len(error.exception.attempts), 1)
        self.assertEqual(error.exception.attempts[0]["category"], "timeout")

    def test_write_method_is_rejected_without_transport(self):
        with patch.object(rpc_client.subprocess, "run") as run:
            with self.assertRaises(q.QueryError):
                q.Client("https://rpc.invalid").call("eth_sendRawTransaction", ["0x00"])
            run.assert_not_called()

    def test_invalid_runtime_budgets_never_start_network(self):
        for option, value in [
            ("--timeout", "0"),
            ("--timeout", "61"),
            ("--retries", "-1"),
            ("--retries", "3"),
            ("--total-timeout", "0"),
            ("--total-timeout", "601"),
        ]:
            with self.subTest(option=option, value=value):
                args = q.parser().parse_args(["--address", A, option, value])
                fake = Fake()
                result = q.scan(args, fake)
                self.assertFalse(result["coverage"]["complete"])
                self.assertEqual(fake.calls, [])

    def test_third_conflicting_record_cannot_restore_excluded_hash(self):
        class Conflict(Fake):
            def call(self, method, params):
                result = super().call(method, params)
                if method == "eth_getBlockByNumber" and params[0] == "0x1":
                    result["transactions"] = [transaction(value=v) for v in (1, 2, 1)]
                return result

        fake = Conflict()
        result = q.scan(self.args(), fake)
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["transactions"], [])
        self.assertIsNone(result["metrics"])
        self.assertNotIn("eth_getTransactionReceipt", fake.calls)

    def test_invalid_optional_code_does_not_invalidate_transaction_evidence(self):
        class InvalidCode(Fake):
            def call(self, method, params):
                return "not-code" if method == "eth_getCode" else super().call(method, params)

        result = q.scan(self.args(), InvalidCode())
        self.assertTrue(result["coverage"]["complete"])
        self.assertIsNone(result["code_at_end_block"])
        self.assertEqual(result["metrics"]["success"], 1)
        self.assertTrue(any("代码查询失败" in warning for warning in result["warnings"]))

    def test_valid_rpc_error_preserves_code_and_redacts_provider_message(self):
        body = '{"jsonrpc":"2.0","id":1,"error":{"code":-32005,"message":"secret"}}\n200'
        response = subprocess.CompletedProcess([], 0, body, "")
        with patch.object(rpc_client.subprocess, "run", return_value=response):
            with self.assertRaises(q.QueryError) as error:
                q.Client("https://rpc.invalid", retries=0).call("eth_chainId", [])
        self.assertEqual(error.exception.category, "rpc_error")
        self.assertIn("-32005", str(error.exception))
        self.assertNotIn("secret", json.dumps(error.exception.detail()))

    def test_stalled_progress_emits_heartbeat_and_stops_cleanly(self):
        stderr = io.StringIO()
        progress = q.Progress(enabled=True)
        with contextlib.redirect_stderr(stderr):
            progress.start()
            try:
                time.sleep(5.15)
            finally:
                progress.finish()
        events = [json.loads(line)["progress"] for line in stderr.getvalue().splitlines()]
        self.assertGreaterEqual(len(events), 3)
        self.assertGreaterEqual(events[1]["elapsed_seconds"], 5)
        self.assertFalse(progress.thread.is_alive())
