"""Read-only JSON-RPC client with shared deadlines, retries and rate limit."""

import json
import os
import re
import subprocess
import threading
import time

from evidence import QueryError, invalid_constant, unique_object
from rpc_transport import HTTPPool, TransportError

READ_METHODS = frozenset(
    {
        "eth_chainId",
        "eth_blockNumber",
        "eth_getBlockByNumber",
        "eth_getTransactionReceipt",
        "eth_getCode",
    }
)


class Client:
    def __init__(
        self,
        url,
        timeout=15,
        retries=2,
        total_timeout=120,
        transport="curl",
        concurrency=4,
    ):
        self.url, self.timeout, self.retries = url, timeout, retries
        self.lock = threading.Lock()
        self.next_request = 0.0
        self.deadline = time.monotonic() + total_timeout
        if transport == "auto":
            proxy = any(
                os.environ.get(key)
                for key in (
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "ALL_PROXY",
                    "http_proxy",
                    "https_proxy",
                    "all_proxy",
                )
            )
            transport = "curl" if proxy else "http"
        self.transport = transport
        self.pool = HTTPPool(url, concurrency) if transport == "http" else None
        self.stats_lock = threading.Lock()
        self.stats = {
            "transport": transport,
            "requests": 0,
            "retries": 0,
            "methods": {},
        }

    def close(self):
        if self.pool:
            self.pool.close()

    def diagnostics(self):
        with self.stats_lock:
            return {**self.stats, **(self.pool.stats if self.pool else {})}

    def remaining(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise QueryError("整次查询已达到总耗时上限", "deadline_exceeded")
        return left

    def pause(self, seconds):
        time.sleep(min(seconds, self.remaining()))
        self.remaining()

    def call(self, method, params):
        if method not in READ_METHODS:
            raise QueryError("本工具只允许已定义的只读 RPC 方法", "invalid_method")
        attempt_errors = []
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        config = "url = " + json.dumps(self.url) + "\n"
        # Shared start-rate cap of 5 requests/s, including retries; no batch calls.
        for attempt in range(self.retries + 1):
            try:
                with self.lock:
                    self.pause(max(0, self.next_request - time.monotonic()))
                    self.next_request = time.monotonic() + 0.2
                request_timeout = min(self.timeout, self.remaining())
            except QueryError:
                raise QueryError(
                    "整次查询已达到总耗时上限", "deadline_exceeded", attempt_errors
                ) from None
            # URL via stdin config, not command line; suppress raw curl/RPC errors
            # because providers may echo credential-bearing URLs.
            try:
                with self.stats_lock:
                    self.stats["requests"] += 1
                    self.stats["retries"] += int(attempt > 0)
                    self.stats["methods"][method] = self.stats["methods"].get(method, 0) + 1
                if self.pool:
                    body, http_status = self.pool.request(payload, request_timeout)
                else:
                    r = subprocess.run(
                        [
                            "curl",
                            "--disable",
                            "--proto",
                            "=http,https",
                            "--config",
                            "-",
                            "--silent",
                            "--show-error",
                            "--fail",
                            "--write-out",
                            "\n%{http_code}",
                            "--max-time",
                            str(request_timeout),
                            "--max-filesize",
                            "33554432",
                            "--header",
                            "Content-Type: application/json",
                            "--data-binary",
                            payload,
                        ],
                        input=config,
                        capture_output=True,
                        text=True,
                        timeout=request_timeout,
                    )
                    body, sep, status_text = r.stdout.rpartition("\n")
                    http_status = (
                        int(status_text) if sep and re.fullmatch(r"[0-9]{3}", status_text) else None
                    )
                    if http_status is None:
                        body = r.stdout
                    if r.returncode:
                        category = {
                            28: "timeout",
                            6: "dns_failure",
                            7: "connection_failure",
                            60: "tls_failure",
                        }.get(r.returncode, "transport_failure")
                        if http_status == 429:
                            category = "rate_limited"
                        elif http_status and http_status >= 400:
                            category = "http_error"
                        message = f"transport failure (curl code {r.returncode}, HTTP {http_status or 'unknown'})"
                        raise QueryError(message, category)
                if http_status is not None and not 200 <= http_status < 300:
                    raise QueryError(
                        f"HTTP response {http_status} is not successful",
                        "rate_limited" if http_status == 429 else "http_error",
                    )
                data = json.loads(
                    body,
                    object_pairs_hook=unique_object,
                    parse_constant=invalid_constant,
                )
                if (
                    not isinstance(data, dict)
                    or data.get("jsonrpc") != "2.0"
                    or type(data.get("id")) is not int
                    or data["id"] != 1
                ):
                    raise QueryError("invalid RPC envelope", "invalid_response")
                if "error" in data:
                    if "result" in data or not isinstance(data["error"], dict):
                        raise QueryError("invalid RPC error envelope", "invalid_response")
                    rpc_error = data["error"]
                    code = rpc_error.get("code")
                    if type(code) is not int:
                        raise QueryError("invalid RPC error code", "invalid_response")
                    raise QueryError(
                        f"RPC error code {code}",
                        "rpc_error",
                    )
                if "result" not in data:
                    raise QueryError("invalid RPC envelope", "invalid_response")
                return data["result"]
            except (
                OSError,
                subprocess.TimeoutExpired,
                ValueError,
                RecursionError,
                QueryError,
                TransportError,
            ) as e:
                if isinstance(e, (QueryError, TransportError)):
                    error, category = str(e), e.category
                else:
                    error = type(e).__name__
                    category = (
                        "timeout"
                        if isinstance(e, subprocess.TimeoutExpired)
                        else "invalid_response"
                        if isinstance(e, (ValueError, RecursionError))
                        else "transport_failure"
                    )
                attempt_errors.append(
                    {"attempt": attempt + 1, "category": category, "error": error}
                )
                if time.monotonic() >= self.deadline:
                    raise QueryError(
                        "整次查询已达到总耗时上限", "deadline_exceeded", attempt_errors
                    )
                if category in ("invalid_response", "tls_failure"):
                    break
                if attempt < self.retries:
                    try:
                        self.pause(0.5 * (2**attempt))
                    except QueryError:
                        raise QueryError(
                            "整次查询已达到总耗时上限",
                            "deadline_exceeded",
                            attempt_errors,
                        ) from None
        raise QueryError(error, category, attempt_errors)
