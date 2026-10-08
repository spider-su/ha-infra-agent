"""Generic bounded HTTP GET/POST provider."""
from __future__ import annotations

import base64
import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any, Mapping

from ..errors import ConfigError
from .base import TaskProvider


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
        try:
            response = urllib.request.urlopen(request, timeout=request_timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        except (TimeoutError, socket.timeout):
            raise ConfigError(f"task {task_id}: HTTP request timed out") from None
        except urllib.error.URLError:
            raise ConfigError(f"task {task_id}: HTTP connection failed") from None
        try:
            with response:
                status_code = response.status
                if config.get("extract"):
                    body = response.read(max_bytes + 1)
        except TimeoutError:
            raise ConfigError(f"task {task_id}: HTTP response read timed out") from None
        except OSError:
            raise ConfigError(f"task {task_id}: HTTP response read failed") from None
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
