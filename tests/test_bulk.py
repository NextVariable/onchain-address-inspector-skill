"""Deterministic synthetic campaigns. Seeds are cases, not live users/transactions."""

import copy
import os
import random
import subprocess
import time
import unittest
from unittest.mock import patch

from test_query import A, B, C, Fake, q, rpc_client, transaction


def digest(n):
    return "0x" + format(n, "064x")


class Dataset:
    def __init__(self, seed, blocks=12):
        rng = random.Random(seed)
        self.blocks, self.receipts, self.rows = {}, {}, []
        serial = 100000
        for n in range(1, blocks + 1):
            txs = []
            for _ in range(rng.randrange(9)):
                serial += 1
                sender, recipient = rng.choice([A, B, C]), rng.choice([A, B, C, None])
                value = rng.choice([0, 1, 10**18 + 1, 2**256 - 1])
                tx = transaction(n, sender, recipient, value, digest(serial))
                tx["blockHash"] = digest(n)
                tx["input"] = rng.choice(["0x", "0xabcd", None])
                txs.append(tx)
                status = rng.choice(["success", "failed", "unknown"])
                self.receipts[tx["hash"]] = (
                    None
                    if status == "unknown"
                    else {
                        "transactionHash": tx["hash"],
                        "blockHash": digest(n),
                        "blockNumber": hex(n),
                        "status": "0x1" if status == "success" else "0x0",
                    }
                )
                if sender == A or recipient == A:
                    self.rows.append((sender, recipient, value, status, tx["input"]))
            self.blocks[n] = {
                "number": hex(n),
                "hash": digest(n),
                "parentHash": digest(n - 1),
                "timestamp": hex(n),
                "transactions": txs,
            }

    def call(self, method, params):
        if method == "eth_chainId":
            return hex(q.CHAIN_ID)
        if method == "eth_blockNumber":
            return hex(max(self.blocks))
        if method == "eth_getCode":
            return "0x"
        if method == "eth_getBlockByNumber":
            return copy.deepcopy(self.blocks[int(params[0], 16)])
        if method == "eth_getTransactionReceipt":
            return copy.deepcopy(self.receipts[params[0]])
        raise AssertionError(method)


class ImmediateExecutor:
    """Ledger tests need real Future semantics, not thousands of OS threads.

    Actual threading/rate/deadline behavior is covered separately by local HTTP.
    """

    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def submit(self, function, *args):
        future = q.concurrent.futures.Future()
        try:
            future.set_result(function(*args))
        except Exception as error:
            future.set_exception(error)
        return future


class BulkTests(unittest.TestCase):
    def args(self, end=12, concurrency=4):
        return q.parser().parse_args(
            [
                "--address",
                A,
                "--start-block",
                "1",
                "--end-block",
                str(end),
                "--concurrency",
                str(concurrency),
            ]
        )

    def test_seeded_scan_against_independent_ledger(self):
        # Expected sums/statuses derive from the input ledger, never query.metrics.
        seeds = int(os.environ.get("MONAD_TEST_SEEDS", "1000"))
        if not 1 <= seeds <= 10000:
            raise ValueError("MONAD_TEST_SEEDS must be between 1 and 10000")
        for seed in range(seeds):
            with self.subTest(seed=seed):
                data = Dataset(seed)
                with patch.object(q.concurrent.futures, "ThreadPoolExecutor", ImmediateExecutor):
                    result = q.scan(self.args(concurrency=1 + seed % 8), data)
                m = result["metrics"]
                self.assertIsNotNone(m)
                rows = data.rows
                self.assertEqual(m["direct_transactions"], len(rows))
                for status in ["success", "failed", "unknown"]:
                    self.assertEqual(m[status], sum(row[3] == status for row in rows))
                for direction, index in [("in", 1), ("out", 0)]:
                    expected = sum(
                        row[2] for row in rows if row[index] == A and row[3] == "success"
                    )
                    self.assertEqual(
                        m["native_value_successful_top_level_only"][direction + "_wei"],
                        str(expected),
                    )
                incoming = [row for row in rows if row[1] == A]
                self.assertEqual(m["direct_to_transactions"], len(incoming))
                self.assertEqual(m["distinct_direct_initiators"], len({row[0] for row in incoming}))
                counts = {
                    sender: sum(row[0] == sender for row in incoming)
                    for sender in {row[0] for row in incoming}
                }
                expected_ranking = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
                self.assertEqual(
                    [(r["address"], r["count"]) for r in m["caller_ranking"]],
                    expected_ranking,
                )
                self.assertEqual(
                    m["repeated_direct_initiators"],
                    sum(count >= 2 for count in counts.values()),
                )
                for rank in m["caller_ranking"]:
                    self.assertEqual(rank["share_denominator"], len(incoming))
                    self.assertEqual(rank["share_numerator"], counts[rank["address"]])
                self.assertEqual(
                    m["direct_to_with_input_data"]["transactions"],
                    sum(row[4] == "0xabcd" for row in incoming),
                )
                self.assertEqual(
                    m["direct_to_without_input_data"],
                    sum(row[4] == "0x" for row in incoming),
                )
                self.assertEqual(
                    m["direct_to_unknown_input_data"],
                    sum(row[4] is None for row in incoming),
                )
                self.assertEqual(
                    result["coverage"]["complete"],
                    all(row[3] != "unknown" for row in rows),
                )

    def test_mutated_blocks_never_count_as_complete(self):
        fields = ["number", "hash", "parentHash", "timestamp", "transactions"]
        invalid = [None, False, 3, {}, "", "0x", "bad"]
        for field in fields:
            for value in invalid:
                with self.subTest(field=field, value=value):
                    data = Dataset(20, blocks=1)
                    data.blocks[1][field] = value
                    r = q.scan(self.args(end=1), data)
                    self.assertFalse(r["coverage"]["complete"])
                    self.assertIsNone(r["metrics"])

    def test_mutated_receipts_preserve_unknown_and_do_not_add_value(self):
        for field in ["transactionHash", "blockHash", "blockNumber", "status"]:
            for value in [None, False, 3, {}, "", "0x", "bad", "0x2"]:
                with self.subTest(field=field, value=value):

                    class BadReceipt(Fake):
                        def call(self, method, params):
                            result = super().call(method, params)
                            if method == "eth_getTransactionReceipt":
                                result[field] = value
                            return result

                    r = q.scan(self.args(end=2), BadReceipt())
                    self.assertFalse(r["coverage"]["complete"])
                    self.assertEqual(r["metrics"]["unknown"], 1)
                    self.assertEqual(
                        r["metrics"]["native_value_successful_top_level_only"]["in_wei"],
                        "0",
                    )
                    self.assertEqual(len(r["coverage"]["receipt_failures"]), 1)

    def test_recent_defaults_and_genesis_boundaries(self):
        class Empty(Dataset):
            def __init__(self):
                super().__init__(1, blocks=150)
                for block in self.blocks.values():
                    block["transactions"] = []
                self.blocks[0] = {
                    "number": "0x0",
                    "hash": digest(0),
                    "parentHash": digest(0),
                    "timestamp": "0x0",
                    "transactions": [],
                }

        for recent, expected_start in [(None, 51), (1, 150), (1000, 0)]:
            args = ["--address", A]
            if recent is not None:
                args += ["--recent-blocks", str(recent)]
            r = q.scan(q.parser().parse_args(args), Empty())
            self.assertTrue(r["coverage"]["complete"])
            self.assertEqual(r["requested_range"]["resolved"]["start_block"], expected_start)
            self.assertEqual(r["actual_scanned_range"]["successful_count"], 151 - expected_start)

    def test_over_limit_range_stops_before_any_rpc(self):
        f = Fake()
        r = q.scan(self.args(end=1001), f)
        self.assertFalse(r["coverage"]["complete"])
        self.assertEqual(f.calls, [])

    def test_concurrency_limits_stop_before_any_rpc(self):
        for concurrency in [0, 9]:
            f = Fake()
            r = q.scan(self.args(concurrency=concurrency), f)
            self.assertFalse(r["coverage"]["complete"])
            self.assertEqual(f.calls, [])

    def test_mutated_transactions_never_count_as_complete(self):
        fields = ["hash", "from", "to", "blockNumber", "blockHash", "value"]
        invalid = [False, 3, {}, "", "0x", "bad"]
        for field in fields:
            for value in invalid:
                with self.subTest(field=field, value=value):
                    data = Dataset(20, blocks=1)
                    tx = transaction()
                    tx["blockHash"] = digest(1)
                    tx[field] = value
                    data.blocks[1]["transactions"] = [tx]
                    r = q.scan(self.args(end=1), data)
                    self.assertFalse(r["coverage"]["complete"])
                    self.assertIsNone(r["metrics"])

    def test_rpc_nested_response_is_structured_error(self):
        body = "[" * 1200 + "0" + "]" * 1200
        response = subprocess.CompletedProcess([], 0, body + "\n200", "")
        with patch.object(rpc_client.subprocess, "run", return_value=response):
            with self.assertRaises(q.QueryError) as err:
                q.Client("http://invalid", retries=0).call("eth_chainId", [])
        self.assertEqual(err.exception.category, "invalid_response")

    def test_noncanonical_and_overwide_quantities_rejected(self):
        for value in ["0x00", "0x01", "0x" + "1" * 65]:
            with self.subTest(value=value), self.assertRaises(q.QueryError):
                q.quantity(value)

    def test_nonstandard_json_constants_rejected(self):
        for value in ["NaN", "Infinity", "-Infinity"]:
            body = '{"jsonrpc":"2.0","id":1,"result":' + value + "}\n200"
            with (
                self.subTest(value=value),
                patch.object(
                    rpc_client.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, body, ""),
                ),
            ):
                with self.assertRaises(q.QueryError):
                    q.Client("http://invalid", retries=0).call("eth_chainId", [])

    def test_null_error_field_is_not_a_valid_success(self):
        for body in [
            '{"jsonrpc":"2.0","id":1,"result":"0x279f","error":null}',
            '{"jsonrpc":"2.0","id":1,"error":null}',
        ]:
            with (
                self.subTest(body=body),
                patch.object(
                    rpc_client.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, body + "\n200", ""),
                ),
            ):
                with self.assertRaises(q.QueryError):
                    q.Client("http://invalid", retries=0).call("eth_chainId", [])

    def test_redirect_body_is_not_rpc_success(self):
        body = '{"jsonrpc":"2.0","id":1,"result":"0x279f"}\n302'
        with patch.object(
            rpc_client.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, body, ""),
        ):
            with self.assertRaises(q.QueryError):
                q.Client("http://invalid", retries=0).call("eth_chainId", [])

    def test_completion_order_does_not_change_duplicate_evidence(self):
        class Reverse(Fake):
            def call(self, method, params):
                if method == "eth_getBlockByNumber" and params[0] == "0x1":
                    time.sleep(0.015)
                r = super().call(method, params)
                if method == "eth_getBlockByNumber":
                    r["transactions"] = [transaction(n=int(params[0], 16))]
                return r

        r = q.scan(self.args(end=2), Reverse())
        self.assertEqual(r["coverage"]["data_anomalies"][0]["blocks"], [1, 2])

    def test_maximum_block_and_receipt_budget(self):
        data = Dataset(1, blocks=1000)
        # Make exactly 6000 relevant txs, 5000 receipts fetched, 1000 unknown.
        for n, block in data.blocks.items():
            block["transactions"] = []
            for i in range(6):
                h = digest(100000 + n * 6 + i)
                tx = transaction(n, B, A, 1, h)
                tx["blockHash"] = digest(n)
                block["transactions"].append(tx)
                data.receipts[h] = {
                    "transactionHash": h,
                    "blockHash": digest(n),
                    "blockNumber": hex(n),
                    "status": "0x1",
                }
        r = q.scan(self.args(end=1000), data)
        self.assertEqual(len(r["coverage"]["successful_blocks"]), 1000)
        self.assertEqual(len(r["transactions"]), 6000)
        self.assertEqual(r["metrics"]["success"], 5000)
        self.assertEqual(r["metrics"]["unknown"], 1000)
        self.assertEqual(r["metrics"]["native_value_successful_top_level_only"]["in_wei"], "5000")
        self.assertFalse(r["coverage"]["complete"])
        self.assertEqual(len(r["coverage"]["receipt_failures"]), 1000)
        self.assertTrue(any("超过当前总预算" in warning for warning in r["warnings"]))


if __name__ == "__main__":
    unittest.main()
