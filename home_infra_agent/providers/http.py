"""Generic bounded HTTP GET/POST provider."""
from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Mapping
from urllib.parse import urlsplit
from urllib.request import HTTPHandler, HTTPSHandler

from ..errors import ConfigError
from .base import TaskProvider


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parts = urlsplit(url)
    default_port = 443 if parts.scheme.lower() == "https" else 80 if parts.scheme.lower() == "http" else None
    return parts.scheme.lower(), parts.hostname.lower() if parts.hostname else None, parts.port or default_port


class _CredentialSafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep normal redirects but do not forward credentials to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and _origin(req.full_url) != _origin(newurl):
            redirected.remove_header("Authorization")
            redirected.remove_header("Proxy-Authorization")
        return redirected


class _RequestDeadline:
    def __init__(self, timeout: float):
        self.expires_at = time.monotonic() + timeout
        self.expired = threading.Event()
        self._lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._timer = threading.Timer(timeout, self._expire)
        self._timer.daemon = True

    def start(self) -> None:
        self._timer.start()

    def remaining(self) -> float:
        remaining = self.expires_at - time.monotonic()
        if remaining <= 0:
            self._expire()
            raise TimeoutError
        return remaining

    def resolve(self, host: str, port: int):
        result = []
        failure = []
        finished = threading.Event()

        def lookup():
            try:
                result.extend(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
            except OSError as exc:
                failure.append(exc)
            finally:
                finished.set()

        resolver = threading.Thread(target=lookup, name="http-dns-lookup", daemon=True)
        resolver.start()
        if not finished.wait(self.remaining()):
            self._expire()
            raise TimeoutError
        self.remaining()
        if failure:
            raise failure[0]
        return result

    def attach(self, sock: socket.socket | None) -> None:
        if sock is None:
            return
        with self._lock:
            expired = self.expired.is_set() or time.monotonic() >= self.expires_at
            if not expired:
                self._socket = sock
        if expired:
            self._close(sock)

    def _expire(self) -> None:
        self.expired.set()
        with self._lock:
            sock = self._socket
        self._close(sock)

    @staticmethod
    def _close(sock: socket.socket | None) -> None:
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def close(self) -> None:
        self._timer.cancel()
        with self._lock:
            self._socket = None


class _DeadlineHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, *args, deadline: _RequestDeadline, **kwargs):
        super().__init__(host, *args, **kwargs)
        self._deadline = deadline

    def connect(self):
        _connect_tcp(self, self._deadline)
        if self._tunnel_host:
            self._tunnel()


class _DeadlineHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *args, deadline: _RequestDeadline, **kwargs):
        super().__init__(host, *args, **kwargs)
        self._deadline = deadline

    def connect(self):
        _connect_tcp(self, self._deadline)
        if self._tunnel_host:
            self._tunnel()
        server_hostname = self._tunnel_host if self._tunnel_host else self.host
        self.sock.settimeout(self._deadline.remaining())
        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)
        self._deadline.attach(self.sock)


def _connect_tcp(connection: http.client.HTTPConnection, deadline: _RequestDeadline) -> None:
    last_error = None
    for family, socktype, protocol, _canonname, sockaddr in deadline.resolve(connection.host, connection.port):
        sock = socket.socket(family, socktype, protocol)
        try:
            if connection.source_address:
                sock.bind(connection.source_address)
            for option in getattr(connection, "socket_options", ()):
                sock.setsockopt(*option)
            sock.settimeout(deadline.remaining())
            deadline.attach(sock)
            sock.connect(sockaddr)
            connection.sock = sock
            return
        except OSError as exc:
            sock.close()
            if deadline.expired.is_set() or time.monotonic() >= deadline.expires_at:
                deadline.remaining()
            last_error = exc
    if last_error is not None:
        raise last_error
    raise OSError("host did not resolve to a stream address")


class _DeadlineHTTPHandler(HTTPHandler):
    def __init__(self, deadline: _RequestDeadline):
        super().__init__()
        self.deadline = deadline

    def http_open(self, request):
        return self.do_open(_DeadlineHTTPConnection, request, deadline=self.deadline)


class _DeadlineHTTPSHandler(HTTPSHandler):
    def __init__(self, deadline: _RequestDeadline):
        super().__init__()
        self.deadline = deadline

    def https_open(self, request):
        return self.do_open(_DeadlineHTTPSConnection, request, deadline=self.deadline,
                            context=self._context, check_hostname=self._check_hostname)


class HttpProvider(TaskProvider):
    def execute(self, task_id: str, config: Mapping[str, Any], timeout: float) -> tuple[str, dict[str, Any]]:
        url = config["url"]
        method = str(config.get("method", "GET")).upper()
        request_timeout = timeout
        max_bytes = config.get("maxResponseBytes", 1_048_576)
        headers = dict(config.get("headers") or {})
        auth = config.get("auth")
        if auth:
            auth_type = auth["type"]
            if auth_type == "bearer":
                token = os.environ.get(auth["tokenEnv"])
                if not token:
                    raise ConfigError(f"task {task_id}: environment variable {auth['tokenEnv']} is not set")
                headers["Authorization"] = f"Bearer {token}"
            elif auth_type == "basic":
                username = os.environ.get(auth["usernameEnv"])
                password = os.environ.get(auth["passwordEnv"])
                if not username or password is None:
                    missing = auth["usernameEnv"] if not username else auth["passwordEnv"]
                    raise ConfigError(f"task {task_id}: environment variable {missing} is not set")
                encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
                headers["Authorization"] = f"Basic {encoded}"
        data = None
        if method == "POST" and "body" in config:
            data = json.dumps(config["body"], separators=(",", ":")).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        status_code = 0
        body = b""
        deadline = _RequestDeadline(request_timeout)
        opener = urllib.request.build_opener(
            _CredentialSafeRedirectHandler(), _DeadlineHTTPHandler(deadline), _DeadlineHTTPSHandler(deadline)
        )
        deadline.start()
        try:
            response = opener.open(request, timeout=deadline.remaining())
        except urllib.error.HTTPError as exc:
            response = exc
        except (TimeoutError, socket.timeout):
            deadline.close()
            raise ConfigError(f"task {task_id}: HTTP request timed out") from None
        except urllib.error.URLError:
            timed_out = deadline.expired.is_set()
            deadline.close()
            if timed_out:
                raise ConfigError(f"task {task_id}: HTTP request timed out") from None
            raise ConfigError(f"task {task_id}: HTTP connection failed") from None
        try:
            with response:
                status_code = response.status
                if config.get("extract"):
                    chunks = bytearray()
                    while len(chunks) <= max_bytes:
                        deadline.remaining()
                        chunk = response.read1(min(65_536, max_bytes + 1 - len(chunks)))
                        if not chunk:
                            break
                        chunks.extend(chunk)
                    body = bytes(chunks)
        except (TimeoutError, socket.timeout, OSError):
            if deadline.expired.is_set() or time.monotonic() >= deadline.expires_at:
                raise ConfigError(f"task {task_id}: HTTP request timed out") from None
            raise ConfigError(f"task {task_id}: HTTP response read failed") from None
        finally:
            deadline.close()
        if len(body) > max_bytes:
            raise ConfigError(f"task {task_id}: response exceeds maxResponseBytes ({max_bytes})")
        expected = config.get("expectedStatusCodes", list(range(200, 400)))
        values = {"reachable": status_code in expected, "statusCode": status_code}
        if not values["reachable"]:
            return "ERROR", values
        if not config.get("extract"):
            return "OK", values
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ConfigError(f"task {task_id}: response is not valid JSON") from None
        return "OK", payload
