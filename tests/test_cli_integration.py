"""Local synthetic HTTP server; exercises real curl and CLI, not live-chain proof."""

import concurrent.futures
import json
import os
import pathlib
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_query import Fake, q

SCRIPT = pathlib.Path(__file__).parents[1] / "skills/onchain-address-inspector/scripts/query.py"
ADDRESS = "0x" + "a" * 40


class CliIntegration(unittest.TestCase):
    def test_eight_workers_still_share_five_requests_per_second(self):
        arrivals = []
        scheduled = []
        lock = threading.Lock()

        class ObservedClient(q.Client):
            def pause(self, seconds):
                super().pause(seconds)
                # Observed under the shared scheduling lock, before IO.
                scheduled.append(time.monotonic())

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                with lock:
                    arrivals.append(time.monotonic())
                body = b'{"jsonrpc":"2.0","id":1,"result":"0x279f"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        worker.start()
        try:
            client = ObservedClient(
                f"http://127.0.0.1:{server.server_port}",
                timeout=15,
                retries=0,
                total_timeout=60,
                transport="http",
                concurrency=8,
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                values = list(pool.map(lambda _: client.call("eth_chainId", []), range(12)))
            self.assertEqual(values, ["0x279f"] * 12)
            self.assertEqual(len(arrivals), 12)
            self.assertEqual(len(scheduled), 12)
            self.assertGreaterEqual(scheduled[-1] - scheduled[0], 2.15)
            self.assertTrue(all(b - a >= 0.195 for a, b in zip(scheduled, scheduled[1:])))
        finally:
            client.close()
            server.shutdown()
            server.server_close()
            worker.join()

    def run_workflow(self, mode):
        fake = Fake(missing=mode == "missing_receipt")
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(request["method"])
                if mode == "rate_limit" and len(requests) == 1:
                    body = b"provider-secret"
                    status = 429
                else:
                    result = fake.call(request["method"], request["params"])
                    if mode == "scan_deadline" and request["method"] == "eth_blockNumber":
                        result = "0x3e8"
                    if (
                        mode == "reorg"
                        and request["method"] == "eth_getBlockByNumber"
                        and request["params"][1] is False
                    ):
                        result["hash"] = "0x" + "9" * 64
                    body = json.dumps(
                        {"jsonrpc": "2.0", "id": request["id"], "result": result}
                    ).encode()
                    status = 200
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                # curlrc would otherwise overwrite the POST with a failing method.
                pathlib.Path(directory, ".curlrc").write_text('request = "GET"\n')
                env = {**os.environ, "CURL_HOME": directory}
                p = subprocess.run(
                    [
                        "python3",
                        str(SCRIPT),
                        "--address",
                        ADDRESS,
                        "--start-block",
                        "1",
                        "--end-block",
                        "1000" if mode == "scan_deadline" else "2",
                        "--rpc",
                        f"http://127.0.0.1:{server.server_port}",
                        "--transport",
                        "curl",
                        "--total-timeout",
                        "3" if mode == "scan_deadline" else "60",
                        "--retries",
                        "1",
                        "--progress",
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30 if mode == "scan_deadline" else 90,
                )
                return p, json.loads(p.stdout), requests
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_real_curl_complete_workflow_ignores_curlrc(self):
        p, r, requests = self.run_workflow("success")
        self.assertEqual(p.returncode, 0)
        self.assertTrue(r["coverage"]["complete"])
        self.assertEqual(
            r["metrics"]["native_value_successful_top_level_only"]["in_wei"],
            str(10**18),
        )
        self.assertEqual(requests.count("eth_getBlockByNumber"), 3)
        self.assertTrue(
            all(
                m
                in {
                    "eth_chainId",
                    "eth_blockNumber",
                    "eth_getBlockByNumber",
                    "eth_getTransactionReceipt",
                    "eth_getCode",
                }
                for m in requests
            )
        )

    def test_real_curl_retries_429_without_exposing_body(self):
        p, r, requests = self.run_workflow("rate_limit")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(requests[:2], ["eth_chainId", "eth_chainId"])
        self.assertNotIn("provider-secret", p.stdout + p.stderr)

    def test_real_curl_missing_receipt_is_not_chain_failure(self):
        p, r, requests = self.run_workflow("missing_receipt")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(r["metrics"]["unknown"], 1)
        self.assertEqual(r["metrics"]["failed"], 0)
        self.assertEqual(r["metrics"]["native_value_successful_top_level_only"]["in_wei"], "0")

    def test_real_curl_reorg_suppresses_totals(self):
        p, r, requests = self.run_workflow("reorg")
        self.assertEqual(p.returncode, 2)
        self.assertFalse(r["coverage"]["chain_consistent"])
        self.assertIsNone(r["metrics"])

    def test_real_curl_deadline_drains_large_scan_and_retains_evidence(self):
        p, r, requests = self.run_workflow("scan_deadline")
        self.assertEqual(p.returncode, 2)
        # Includes queue draining, JSON/progress formatting and host scheduling;
        # the shared deadline bounds network waits, not hard realtime CPU work.
        self.assertLess(r["elapsed_seconds"], 8)
        self.assertLess(len(requests), 30)
        self.assertGreater(len(r["coverage"]["successful_blocks"]), 0)
        self.assertEqual(
            len(r["coverage"]["successful_blocks"]) + len(r["coverage"]["failed_blocks"]),
            1000,
        )
        self.assertTrue(any(e.get("category") == "deadline_exceeded" for e in r["errors"]))
        self.assertEqual(len(r["transactions"]), 1)
        self.assertEqual(r["transactions"][0]["status"], "unknown")
        self.assertIsNone(r["metrics"])

    def run_case(self, delay=0, chain="0x1"):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                time.sleep(delay)
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": chain}).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        worker.start()
        started = time.monotonic()
        try:
            p = subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "--address",
                    ADDRESS,
                    "--rpc",
                    f"http://127.0.0.1:{server.server_port}",
                    "--transport",
                    "curl",
                    "--total-timeout",
                    "1" if delay else "10",
                    "--progress",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return p, json.loads(p.stdout), time.monotonic() - started
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_real_curl_wrong_network(self):
        p, r, elapsed = self.run_case()
        self.assertEqual(p.returncode, 2)
        self.assertEqual(r["chain_id"], 1)
        self.assertIsNone(r["metrics"])
        self.assertFalse(r["coverage"]["complete"])
        self.assertTrue(all("progress" in json.loads(line) for line in p.stderr.splitlines()))

    def test_real_curl_total_deadline(self):
        p, r, elapsed = self.run_case(delay=2, chain="0x279f")
        self.assertEqual(p.returncode, 2)
        self.assertLess(r["elapsed_seconds"], 10)
        self.assertTrue(any(e.get("category") == "deadline_exceeded" for e in r["errors"]))
        self.assertIsNone(r["metrics"])


if __name__ == "__main__":
    unittest.main()
