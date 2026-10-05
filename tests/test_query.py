"""Synthetic unit/integration cases only; never live chain evidence."""

import importlib.util
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import patch

path = pathlib.Path(__file__).parents[1] / "skills/onchain-address-inspector/scripts/query.py"
sys.path.insert(0, str(path.parent))
spec = importlib.util.spec_from_file_location("query", path)
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)
rpc_client = importlib.import_module("rpc_client")
evidence = importlib.import_module("evidence")

A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40
H = "0x" + "1" * 64


def transaction(n=1, f=B, to=A, value=10**18, h=H):
    return {
        "hash": h,
        "from": f,
        "to": to,
        "value": hex(value),
        "blockNumber": hex(n),
        "blockHash": "0x" + str(n) * 64,
    }


class Fake:
    def __init__(self, chain=10143, fail_block=None, missing=False, mismatch=False):
        self.chain = chain
        self.fail_block = fail_block
        self.missing = missing
        self.mismatch = mismatch
        self.calls = []

    def call(self, m, p):
        self.calls.append(m)
        if m == "eth_chainId":
            return hex(self.chain)
        if m == "eth_blockNumber":
            return "0x2"
        if m == "eth_getCode":
            return "0x"
        if m == "eth_getBlockByNumber":
            n = int(p[0], 16)
            if n == self.fail_block:
                raise q.QueryError("fake block failure")
            return {
                "number": hex(n),
                "hash": "0x" + str(n) * 64,
                "parentHash": "0x" + str(n - 1) * 64,
                "timestamp": "0x1",
                "transactions": [transaction()] if n == 1 else [],
            }
        if m == "eth_getTransactionReceipt":
            if self.missing:
                return None
            return {
                "transactionHash": H,
                "blockNumber": "0x1",
                "blockHash": "0x" + ("2" if self.mismatch else "1") * 64,
                "status": "0x1",
            }
        raise AssertionError(m)


class Tests(unittest.TestCase):
    def args(self, *extra):
        return q.parser().parse_args(
            ["--address", A, "--start-block", "1", "--end-block", "2", *extra]
        )

    def test_complete_and_precision(self):
        r = q.scan(self.args(), Fake())
        self.assertTrue(r["coverage"]["complete"])
        self.assertEqual(
            r["metrics"]["native_value_successful_top_level_only"]["in_wei"],
            str(10**18),
        )
        self.assertEqual(evidence.mon(10**30 + 1), "1000000000000.000000000000000001")

    def test_invalid_address_ranges(self):
        for extra in [
            ["--address", "bad"],
            ["--start-block", "-1"],
            ["--end-block", "0"],
            ["--end-block", "1001"],
            ["--recent-blocks", "5"],
        ]:
            f = Fake()
            r = q.scan(self.args(*extra), f)
            self.assertTrue(r["errors"])
            self.assertEqual(f.calls, [])
        for args in [
            ["--address", A, "--recent-blocks", "0"],
            ["--address", A, "--start-block", "1"],
        ]:
            with self.assertRaises(q.QueryError):
                q.validate(q.parser().parse_args(args))

    def test_argument_error_redaction(self):
        with self.assertRaises(q.QueryError) as e:
            q.parser().parse_args(["--address", A, "--recent-blocks", "secret-key"])
        self.assertNotIn("secret-key", str(e.exception))

    def test_identical_duplicate_does_not_inflate(self):
        class Duplicate(Fake):
            def call(self, m, p):
                r = super().call(m, p)
                if m == "eth_getBlockByNumber" and int(p[0], 16) == 1:
                    r["transactions"] *= 2
                return r

        f = Duplicate()
        r = q.scan(self.args(), f)
        self.assertEqual(r["metrics"]["direct_transactions"], 1)
        self.assertEqual(len(r["transactions"]), 1)
        self.assertEqual(
            r["metrics"]["native_value_successful_top_level_only"]["in_wei"],
            str(10**18),
        )
        self.assertEqual(f.calls.count("eth_getTransactionReceipt"), 1)
        self.assertFalse(r["coverage"]["complete"])
        self.assertEqual(r["coverage"]["data_anomalies"][0]["category"], "duplicate_transaction")

    def test_conflicting_duplicate_excluded(self):
        class Conflict(Fake):
            def call(self, m, p):
                r = super().call(m, p)
                if m == "eth_getBlockByNumber" and int(p[0], 16) == 1:
                    r["transactions"].append(transaction(value=2 * 10**18))
                return r

        r = q.scan(self.args(), Conflict())
        self.assertFalse(r["coverage"]["complete"])
        self.assertIsNone(r["metrics"])
        self.assertEqual(r["coverage"]["data_anomalies"][0]["category"], "conflicting_duplicate")
        self.assertFalse(any("没有找到相关" in w for w in r["warnings"]))

    def test_receipt_failure_categories(self):
        for f, expected in [
            (Fake(missing=True), "receipt_not_found"),
            (Fake(mismatch=True), "receipt_mismatch"),
        ]:
            r = q.scan(self.args(), f)
            self.assertEqual(r["coverage"]["receipt_failures"][0]["category"], expected)
            self.assertEqual(r["errors"][0]["hash"], H)

        class Timeout(Fake):
            def call(self, m, p):
                if m == "eth_getTransactionReceipt":
                    raise q.QueryError(
                        "request timed out",
                        "timeout",
                        [{"attempt": 1, "category": "timeout"}],
                    )
                return super().call(m, p)

        r = q.scan(self.args(), Timeout())
        self.assertEqual(r["coverage"]["receipt_failures"][0]["category"], "timeout")
        self.assertEqual(len(r["coverage"]["receipt_failures"][0]["attempts"]), 1)

    def test_http_rate_limit_redaction(self):
        bad = subprocess.CompletedProcess([], 22, "secret-body\n429", "secret-url")
        with patch.object(rpc_client.subprocess, "run", return_value=bad):
            with self.assertRaises(q.QueryError) as e:
                q.Client("https://rpc.invalid/private-secret", retries=0).call("eth_chainId", [])
        self.assertEqual(e.exception.category, "rate_limited")
        self.assertNotIn("secret", q.json.dumps(e.exception.detail()))

    def test_receipt_budget(self):
        with patch.object(q, "MAX_RECEIPTS", 0):
            r = q.scan(self.args(), Fake())
        self.assertFalse(r["coverage"]["complete"])
        self.assertEqual(
            r["coverage"]["receipt_failures"][0]["category"], "receipt_budget_exceeded"
        )

    def test_input_subset(self):
        txs = [
            {
                "from": B,
                "to": A,
                "value_wei": "0",
                "status": "success",
                "has_input_data": v,
            }
            for v in [True, True, False, None]
        ]
        m = q.metrics(txs, A)
        self.assertEqual(m["direct_to_with_input_data"]["transactions"], 2)
        self.assertEqual(m["direct_to_with_input_data"]["repeated_initiators"], 1)
        self.assertEqual(m["direct_to_without_input_data"], 1)
        self.assertEqual(m["direct_to_unknown_input_data"], 1)

    def test_progress_does_not_pollute_stdout(self):
        import contextlib
        import io

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            q.scan(self.args("--progress"), Fake())
        self.assertEqual(stdout.getvalue(), "")
        events = [q.json.loads(line)["progress"] for line in stderr.getvalue().splitlines()]
        self.assertEqual(events[0]["phase"], "network")
        self.assertEqual(events[-1]["phase"], "done")
        self.assertTrue(any(e["phase"] == "receipts" for e in events))

    def test_duplicate_hash_across_blocks_excluded(self):
        class AcrossBlocks(Fake):
            def call(self, m, p):
                r = super().call(m, p)
                if m == "eth_getBlockByNumber":
                    r["transactions"] = [transaction(n=int(p[0], 16))]
                return r

        r = q.scan(self.args(), AcrossBlocks())
        self.assertFalse(r["coverage"]["complete"])
        self.assertIsNone(r["metrics"])
        self.assertEqual(r["coverage"]["data_anomalies"][0]["blocks"], [1, 2])

    def test_failed_receipt_not_counted_as_value(self):
        class Reverted(Fake):
            def call(self, m, p):
                r = super().call(m, p)
                if m == "eth_getTransactionReceipt":
                    r["status"] = "0x0"
                return r

        r = q.scan(self.args(), Reverted())
        self.assertTrue(r["coverage"]["complete"])
        self.assertEqual(r["metrics"]["failed"], 1)
        self.assertEqual(r["metrics"]["native_value_successful_top_level_only"]["in_wei"], "0")
        self.assertEqual(r["transactions"][0]["value_wei"], str(10**18))

    def test_same_hash_different_calldata_conflicts(self):
        class PayloadConflict(Fake):
            def call(self, m, p):
                r = super().call(m, p)
                if m == "eth_getBlockByNumber" and int(p[0], 16) == 1:
                    one = transaction()
                    one["input"] = "0x1111"
                    two = transaction()
                    two["input"] = "0x2222"
                    r["transactions"] = [one, two]
                return r

        r = q.scan(self.args(), PayloadConflict())
        self.assertIsNone(r["metrics"])
        self.assertFalse(r["coverage"]["complete"])
        self.assertEqual(r["coverage"]["data_anomalies"][0]["category"], "conflicting_duplicate")

    def test_missing_or_empty_to_is_not_creation(self):
        for mode in ("missing", "empty", "wrong_type"):

            class Malformed(Fake):
                def call(self, m, p):
                    r = super().call(m, p)
                    if m == "eth_getBlockByNumber" and int(p[0], 16) == 1:
                        r["transactions"] = [transaction(f=A)]
                        if mode == "missing":
                            del r["transactions"][0]["to"]
                        else:
                            r["transactions"][0]["to"] = "" if mode == "empty" else False
                    return r

            r = q.scan(self.args(), Malformed())
            self.assertFalse(r["coverage"]["complete"])
            self.assertEqual(r["coverage"]["failed_blocks"], [1])
            self.assertEqual(r["transactions"], [])

    def test_deadline_covers_retries_and_subprocess(self):
        import time

        def delayed(*args, **kwargs):
            time.sleep(kwargs["timeout"])
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

        c = q.Client("https://rpc.invalid", timeout=15, retries=2, total_timeout=0.08)
        started = time.monotonic()
        with patch.object(rpc_client.subprocess, "run", side_effect=delayed) as run:
            with self.assertRaises(q.QueryError) as err:
                c.call("eth_chainId", [])
        self.assertEqual(err.exception.category, "deadline_exceeded")
        self.assertEqual(run.call_count, 1)
        self.assertLess(time.monotonic() - started, 0.5)
        with self.assertRaises(q.QueryError):
            c.call("eth_blockNumber", [])

    def test_scan_deadline_retains_partial_records(self):
        class Deadline(Fake):
            def call(self, m, p):
                if m == "eth_getBlockByNumber" and p[0] == "0x2":
                    raise q.QueryError("deadline", "deadline_exceeded")
                return super().call(m, p)

        r = q.scan(self.args(), Deadline())
        self.assertFalse(r["coverage"]["complete"])
        self.assertIsNone(r["metrics"])
        self.assertEqual(len(r["transactions"]), 1)
        self.assertEqual(r["transactions"][0]["status"], "success")

    def test_invalid_transaction_list_and_unmatched_identity(self):
        for mode in ("dict", "foreign"):

            class Invalid(Fake):
                def call(self, m, p):
                    r = super().call(m, p)
                    if m == "eth_getBlockByNumber" and p[0] == "0x1":
                        if mode == "dict":
                            r["transactions"] = {}
                        else:
                            r["transactions"] = [transaction(f=B, to=C)]
                            r["transactions"][0]["blockNumber"] = "0x99"
                    return r

            r = q.scan(self.args(), Invalid())
            self.assertFalse(r["coverage"]["complete"])
            self.assertIn(1, r["coverage"]["failed_blocks"])

    def test_disconnected_chain_suppresses_metrics(self):
        class Fork(Fake):
            def call(self, m, p):
                r = super().call(m, p)
                if m == "eth_getBlockByNumber" and p[0] == "0x2":
                    r["parentHash"] = "0x" + "9" * 64
                return r

        r = q.scan(self.args(), Fork())
        self.assertFalse(r["coverage"]["chain_consistent"])
        self.assertFalse(r["coverage"]["complete"])
        self.assertIsNone(r["metrics"])
        self.assertEqual(len(r["transactions"]), 1)

    def test_end_block_changes_during_scan(self):
        class Reorg(Fake):
            def call(self, m, p):
                r = super().call(m, p)
                if m == "eth_getBlockByNumber" and p[1] is False:
                    r["hash"] = "0x" + "9" * 64
                return r

        r = q.scan(self.args(), Reorg())
        self.assertIsNone(r["metrics"])
        self.assertTrue(any(e["category"] == "chain_mismatch" for e in r["errors"]))

    def test_ambiguous_rpc_envelopes_rejected(self):
        for body in [
            '{"jsonrpc":"2.0","id":true,"result":"0x1"}',
            '{"jsonrpc":"2.0","id":1,"result":"0x1","error":{}}',
            '{"jsonrpc":"2.0","id":1,"result":"0x1","result":"0x2"}',
        ]:
            with patch.object(
                rpc_client.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, body + "\n200", ""),
            ):
                with self.assertRaises(q.QueryError):
                    q.Client("https://rpc.invalid", retries=0).call("eth_chainId", [])

    def test_wrong_chain_stops(self):
        f = Fake(chain=1)
        r = q.scan(self.args(), f)
        self.assertEqual(f.calls, ["eth_chainId"])
        self.assertIsNone(r["metrics"])

    def test_future_range(self):
        r = q.scan(self.args("--end-block", "3"), Fake())
        self.assertIsNone(r["actual_scanned_range"])

    def test_partial_block(self):
        r = q.scan(self.args(), Fake(fail_block=2))
        self.assertFalse(r["coverage"]["complete"])
        self.assertEqual(r["coverage"]["failed_blocks"], [2])
        self.assertIsNone(r["metrics"])
        self.assertEqual(len(r["transactions"]), 1)

    def test_all_blocks_failed_no_metrics(self):
        r = q.scan(self.args("--end-block", "1"), Fake(fail_block=1))
        self.assertIsNone(r["metrics"])
        self.assertFalse(r["coverage"]["complete"])
        self.assertIsNone(r["actual_scanned_range"])

    def test_partial_error_details_and_rpc_profile(self):
        r = q.scan(self.args(), Fake(fail_block=2))
        self.assertEqual(r["errors"][0]["error"], "fake block failure")
        self.assertEqual(r["source"]["rpc_profile"], "Monad Foundation public testnet")
        r = q.scan(self.args("--rpc", "https://rpc.invalid/private-secret"), Fake())
        self.assertEqual(r["source"]["rpc_profile"], "custom endpoint (URL omitted)")
        self.assertNotIn("private-secret", q.json.dumps(r))

    def test_missing_or_reorg_receipt(self):
        for f in [Fake(missing=True), Fake(mismatch=True)]:
            r = q.scan(self.args(), f)
            self.assertFalse(r["coverage"]["complete"])
            self.assertEqual(r["coverage"]["missing_receipts"], [H])
            self.assertEqual(r["metrics"]["unknown"], 1)
            self.assertEqual(r["metrics"]["native_value_successful_top_level_only"]["in_wei"], "0")

    def test_concentration_failed_creation_self(self):
        txs = [
            {"from": f, "to": to, "value_wei": str(v), "status": s}
            for f, to, v, s in [
                (B, A, 10**18 + 1, "success"),
                (B, A, 7, "failed"),
                (C, A, 9, "unknown"),
                (A, None, 11, "success"),
                (A, A, 2, "success"),
            ]
        ]
        m = q.metrics(txs, A)
        self.assertEqual(m["direct_transactions"], 5)
        self.assertEqual(m["distinct_counterparties"], 2)
        self.assertEqual(m["top_caller"]["count"], 2)
        self.assertEqual(m["top_caller"]["share_denominator"], 4)
        self.assertEqual(m["native_value_successful_top_level_only"]["in_wei"], str(10**18 + 3))
        self.assertEqual(m["native_value_successful_top_level_only"]["out_wei"], "13")

    def test_transport_failure_retry_and_redaction(self):
        bad = subprocess.CompletedProcess([], 28, "", "https://user:secret@rpc.invalid/key")
        with (
            patch.object(rpc_client.subprocess, "run", return_value=bad) as run,
            patch.object(q.time, "sleep"),
        ):
            with self.assertRaises(q.QueryError) as e:
                q.Client("https://user:secret@rpc.invalid/key", retries=2).call("eth_chainId", [])
            self.assertEqual(run.call_count, 3)
            self.assertNotIn("secret", str(e.exception))

    def test_rpc_error_and_invalid_json(self):
        for body in [
            '{"error":{"code":-32005,"message":"secret"}}',
            "garbage",
            "[]",
            "null",
            '"secret"',
            '{"error":"secret"}',
        ]:
            with patch.object(
                rpc_client.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, body, ""),
            ):
                with self.assertRaises(q.QueryError) as e:
                    q.Client("https://rpc.invalid", retries=0).call("eth_chainId", [])
                self.assertNotIn("secret", str(e.exception))


if __name__ == "__main__":
    unittest.main()
