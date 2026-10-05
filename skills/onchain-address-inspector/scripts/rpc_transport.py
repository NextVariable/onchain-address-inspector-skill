"""Bounded, reusable HTTP connections. No external dependencies or RPC batches."""

import base64
import concurrent.futures
import http.client
import queue
import socket
import ssl
import threading
import time
import urllib.parse


class TransportError(Exception):
    def __init__(self, message, category):
        super().__init__(message)
        self.category = category


def abort(connection):
    sock = getattr(connection, "sock", None)
    if sock:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    if connection:
        try:
            connection.close()
        except OSError:
            pass


class Job:
    def __init__(self, payload, deadline):
        self.payload, self.deadline = payload, deadline
        self.future = concurrent.futures.Future()
        self.cancelled, self.ack = threading.Event(), threading.Event()
        self.connection = None

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if self.cancelled.is_set() or remaining <= 0:
            raise TransportError("request deadline exceeded", "timeout")
        return remaining


class HTTPPool:
    """One connection per daemon IO worker, reused across both scan phases.

    The caller waits only until the job deadline. Shutdown interrupts socket IO;
    even stalled platform DNS cannot hold the caller indefinitely. A DNS/connect
    job returning late checks cancellation before sending any HTTP request.
    """

    def __init__(self, url, workers=4, max_bytes=33554432):
        self.url = urllib.parse.urlsplit(url)
        self.max_bytes = max_bytes
        self.context = ssl.create_default_context() if self.url.scheme == "https" else None
        self.target = urllib.parse.urlunsplit(("", "", self.url.path or "/", self.url.query, ""))
        self.headers = {
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
        }
        if self.url.username is not None:
            credentials = (
                urllib.parse.unquote(self.url.username)
                + ":"
                + urllib.parse.unquote(self.url.password or "")
            )
            self.headers["Authorization"] = (
                "Basic " + base64.b64encode(credentials.encode()).decode()
            )
        self.jobs = queue.Queue(maxsize=workers * 4)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.connections = set()
        self.stats = {"connections_opened": 0, "reused_requests": 0}
        self.threads = [threading.Thread(target=self.worker, daemon=True) for _ in range(workers)]
        for thread in self.threads:
            thread.start()

    def request(self, payload, timeout):
        if self.stop.is_set():
            raise TransportError("HTTP pool closed", "connection_failure")
        job = Job(payload.encode("utf-8"), time.monotonic() + timeout)
        try:
            self.jobs.put(job, timeout=job.remaining())
            result = job.future.result(timeout=job.remaining())
            job.remaining()
            return result
        except (queue.Full, concurrent.futures.TimeoutError):
            job.cancelled.set()
            abort(job.connection)
            raise TransportError("request deadline exceeded", "timeout")
        finally:
            job.ack.set()

    def worker(self):
        connection = None
        while not self.stop.is_set():
            try:
                job = self.jobs.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                timeout = job.remaining()
                if connection is None:
                    if self.url.scheme == "https":
                        connection = http.client.HTTPSConnection(
                            self.url.hostname,
                            self.url.port,
                            timeout=timeout,
                            context=self.context,
                        )
                    else:
                        connection = http.client.HTTPConnection(
                            self.url.hostname, self.url.port, timeout=timeout
                        )
                    job.connection = connection
                    with self.lock:
                        self.connections.add(connection)
                    connection.connect()
                    # Includes DNS/connect elapsed time; never send a cancelled job.
                    job.remaining()
                    with self.lock:
                        self.stats["connections_opened"] += 1
                else:
                    job.connection = connection
                    with self.lock:
                        self.stats["reused_requests"] += 1
                connection.sock.settimeout(job.remaining())
                if self.stop.is_set():
                    raise TransportError("HTTP pool closed", "connection_failure")
                connection.request("POST", self.target, job.payload, self.headers)
                response = connection.getresponse()
                length = response.getheader("Content-Length")
                if length is not None and (not length.isdigit() or int(length) > self.max_bytes):
                    raise TransportError("invalid or oversized HTTP body", "invalid_response")
                chunks, size = [], 0
                while True:
                    job.remaining()
                    if connection.sock:
                        connection.sock.settimeout(job.remaining())
                    chunk = response.read1(min(65536, self.max_bytes + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise TransportError("oversized HTTP body", "invalid_response")
                if length is not None and size != int(length):
                    raise TransportError("truncated HTTP body", "invalid_response")
                # Python 3.10 read1 does not close an exhausted response; without
                # this, HTTPConnection rejects the next response on reuse.
                response.close()
                job.remaining()
                job.future.set_result((b"".join(chunks).decode("utf-8"), response.status))
                if response.will_close:
                    abort(connection)
                    with self.lock:
                        self.connections.discard(connection)
                    connection = None
            except Exception as error:
                if isinstance(error, TransportError):
                    category = error.category
                elif isinstance(error, ssl.SSLError):
                    category = "tls_failure"
                elif isinstance(error, socket.gaierror):
                    category = "dns_failure"
                elif isinstance(error, TimeoutError):
                    category = "timeout"
                elif isinstance(error, (ValueError, UnicodeError)):
                    category = "invalid_response"
                else:
                    category = "connection_failure"
                job.future.set_exception(
                    TransportError("HTTP transport failure: " + type(error).__name__, category)
                )
                abort(connection)
                with self.lock:
                    self.connections.discard(connection)
                connection = None
            finally:
                # Do not reuse a socket while the caller may still cancel this job.
                while not job.ack.wait(0.05) and not self.stop.is_set():
                    pass
                if job.cancelled.is_set():
                    abort(connection)
                    with self.lock:
                        self.connections.discard(connection)
                    connection = None
                self.jobs.task_done()
        abort(connection)

    def close(self):
        self.stop.set()
        with self.lock:
            connections = list(self.connections)
        for connection in connections:
            abort(connection)
        # DNS may be in an OS resolver; worker is daemon and cannot block exit.
        for thread in self.threads:
            thread.join(timeout=0.1)
