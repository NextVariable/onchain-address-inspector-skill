"""Local HTTP/1.1 integration; synthetic data, actual sockets and connection reuse."""

import contextlib
import importlib
import json
import ssl
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from test_query import A, Fake, q

transport = importlib.import_module("rpc_transport")


@contextlib.contextmanager
def endpoint(mode="normal"):
    state = {"connections": 0, "requests": 0, "methods": []}
    lock = threading.Lock()
    fake = Fake(chain=1 if mode == "wrong_chain" else 10143)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            with lock:
                state["connections"] += 1

        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                state["requests"] += 1
                state["methods"].append(request["method"])
                number = state["requests"]
            if mode == "slow_headers":
                time.sleep(2)
            if mode == "drop_once" and number == 1:
                self.connection.close()
                self.close_connection = True
                return
            status = 429 if mode == "429_once" and number == 1 else 200
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": fake.call(request["method"], request["params"]),
                }
            ).encode()
            if mode == "malformed":
                body = b"{broken"
            if mode == "oversized":
                body = b"x" * 1024
            try:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                if mode == "close":
                    self.send_header("Connection", "close")
                self.end_headers()
                if mode == "drip":
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.04)
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    class Server(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
                super().handle_error(request, client_address)

    server = Server(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


class PersistentTransportTests(unittest.TestCase):
    def test_connection_reuse_when_read1_does_not_close_response(self):
        closed = set()
        original_close = transport.http.client.HTTPResponse.close

        def close(response):
            closed.add(id(response))
            return original_close(response)

        # Python 3.10 read1 leaves an exhausted response open. Require explicit
        # closure, independently of later versions' implicit EOF handling.
        with (
            patch.object(
                transport.http.client.HTTPResponse,
                "isclosed",
                lambda response: id(response) in closed,
            ),
            patch.object(transport.http.client.HTTPResponse, "close", close),
            endpoint() as (url, state),
        ):
            client = self.client(url, retries=0)
            try:
                for _ in range(4):
                    self.assertEqual(client.call("eth_chainId", []), "0x279f")
                self.assertEqual(state["connections"], 1)
                self.assertEqual(client.diagnostics()["reused_requests"], 3)
            finally:
                client.close()

    def client(self, url, **kwargs):
        return q.Client(url, transport="http", concurrency=1, **kwargs)

    def test_reuses_one_connection_and_counts_requests(self):
        with endpoint() as (url, state):
            c = self.client(url)
            try:
                for _ in range(4):
                    self.assertEqual(c.call("eth_chainId", []), "0x279f")
                self.assertEqual(state["connections"], 1)
                self.assertEqual(c.diagnostics()["reused_requests"], 3)
                self.assertEqual(c.diagnostics()["requests"], 4)
            finally:
                c.close()

    def test_complete_scan_preserves_evidence(self):
        with endpoint() as (url, state):
            args = q.parser().parse_args(
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
                ]
            )
            r = q.scan(args)
            self.assertTrue(r["coverage"]["complete"])
            self.assertEqual(
                r["metrics"]["native_value_successful_top_level_only"]["in_wei"],
                str(10**18),
            )
            self.assertEqual(r["performance"]["requests"], 7)
            self.assertLessEqual(state["connections"], 4)

    def test_wrong_chain_stops_before_scanning(self):
        with endpoint("wrong_chain") as (url, state):
            args = q.parser().parse_args(["--address", A, "--rpc", url, "--transport", "http"])
            r = q.scan(args)
            self.assertFalse(r["coverage"]["complete"])
            self.assertEqual(state["methods"], ["eth_chainId"])

    def test_429_retries_and_keeps_connection(self):
        with endpoint("429_once") as (url, state):
            c = self.client(url, retries=1)
            try:
                self.assertEqual(c.call("eth_chainId", []), "0x279f")
                self.assertEqual(state["connections"], 1)
                self.assertEqual(c.diagnostics()["retries"], 1)
            finally:
                c.close()

    def test_closed_connection_reconnects(self):
        for mode in ["close", "drop_once"]:
            with self.subTest(mode=mode), endpoint(mode) as (url, state):
                c = self.client(url, retries=1)
                try:
                    for _ in range(2):
                        self.assertEqual(c.call("eth_chainId", []), "0x279f")
                    self.assertEqual(state["connections"], 2)
                finally:
                    c.close()

    def test_headers_and_dripping_body_respect_total_deadline(self):
        for mode in ["slow_headers", "drip"]:
            with self.subTest(mode=mode), endpoint(mode) as (url, state):
                c = self.client(url, total_timeout=0.4, retries=2)
                started = time.monotonic()
                try:
                    with self.assertRaises(q.QueryError) as err:
                        c.call("eth_chainId", [])
                    self.assertEqual(err.exception.category, "deadline_exceeded")
                    self.assertLess(time.monotonic() - started, 2)
                    self.assertEqual(state["requests"], 1)
                finally:
                    c.close()

    def test_late_connect_does_not_send_rpc_after_deadline(self):
        original = transport.http.client.HTTPConnection.connect

        def delayed(connection):
            time.sleep(0.6)
            return original(connection)

        with (
            endpoint() as (url, state),
            patch.object(transport.http.client.HTTPConnection, "connect", delayed),
        ):
            c = self.client(url, total_timeout=0.2, retries=0)
            try:
                with self.assertRaises(q.QueryError):
                    c.call("eth_chainId", [])
                time.sleep(0.7)
                self.assertEqual(state["requests"], 0)
            finally:
                c.close()

    def test_invalid_and_oversized_response_is_not_retried(self):
        for mode in ["malformed", "oversized"]:
            with self.subTest(mode=mode), endpoint(mode) as (url, state):
                c = self.client(url, retries=2)
                if mode == "oversized":
                    c.pool.max_bytes = 64
                try:
                    with self.assertRaises(q.QueryError) as err:
                        c.call("eth_chainId", [])
                    self.assertEqual(err.exception.category, "invalid_response")
                    self.assertEqual(state["requests"], 1)
                finally:
                    c.close()

    def test_tls_validation_and_error_redaction(self):
        with patch.object(
            transport.http.client.HTTPSConnection,
            "connect",
            side_effect=ssl.SSLCertVerificationError("provider-secret"),
        ):
            c = self.client("https://user:provider-secret@rpc.invalid/key", retries=2)
            try:
                self.assertTrue(c.pool.context.check_hostname)
                self.assertEqual(c.pool.context.verify_mode, ssl.CERT_REQUIRED)
                with self.assertRaises(q.QueryError) as err:
                    c.call("eth_chainId", [])
                self.assertEqual(err.exception.category, "tls_failure")
                self.assertNotIn("provider-secret", json.dumps(err.exception.detail()))
                self.assertEqual(c.diagnostics()["requests"], 1)
            finally:
                c.close()

    def test_auto_preserves_proxy_environment(self):
        with patch.dict(q.os.environ, {"HTTPS_PROXY": "http://proxy.invalid"}):
            c = q.Client("https://rpc.invalid", transport="auto")
            self.assertEqual(c.transport, "curl")
            c.close()


if __name__ == "__main__":
    unittest.main()
