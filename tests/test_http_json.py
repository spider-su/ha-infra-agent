import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from home_infra_agent.core import ConfigError, HttpProvider, Job, JobEngine, validate_task


@contextmanager
def serve(payload, status=200, content_type="application/json", delay=0):
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def _respond(self):
            if delay:
                time.sleep(delay)
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            received.update(method=self.command, path=self.path, headers=dict(self.headers), body=body)
            content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = _respond
        do_POST = _respond

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/status", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def run_http(task, timeout="2s"):
    job = Job("http-test", "HTTP test", Path("."), {"timeout": timeout}, {"status": task})
    return JobEngine([job]).run_job("http-test")


def test_legacy_http_reachability_and_headers_behavior_remains():
    with serve(b"not json", content_type="text/plain") as (url, _):
        status, values = HttpProvider().execute("health", {"url": url, "headers": {"X-Test": "yes"}}, 2)
    assert status == "OK" and values == {"reachable": True, "statusCode": 200}


def test_json_fetch_extraction_transform_and_health_rules():
    payload = {"status": {"online": True}, "cluster": {"ready": "3", "total": 3},
               "sensor": {"temperature_centi": 2179}, "node": {"state": "ready"}}
    with serve(payload) as (url, _):
        result = run_http({
            "type": "http", "url": url, "timeout": "2s", "extract": {
                "online": {"path": "$.status.online", "type": "boolean", "required": True},
                "ready": {"path": "$.cluster.ready", "type": "integer"},
                "total": {"path": "$.cluster.total", "type": "integer"},
                "temperature": {"path": "$.sensor.temperature_centi", "type": "number", "multiply": .01, "round": 1},
            }, "health": {"rules": [{"field": "online", "equals": True},
                                       {"field": "ready", "equalsField": "total"}], "onFailure": "WARN"}
        })
    assert result.status == "OK"
    assert result.tasks["status"].values == {"online": True, "ready": 3, "total": 3, "temperature": 21.8}


def test_post_body_bearer_auth_expected_status_and_json_response(monkeypatch):
    monkeypatch.setenv("EXAMPLE_TOKEN", "never-return-this-secret")
    response = {"request": {"method": "POST", "path": "/status", "authenticated": True}}
    with serve(response, status=201) as (url, received):
        result = run_http({"type": "http", "url": url, "method": "POST", "body": {"probe": "yes"},
                           "auth": {"type": "bearer", "tokenEnv": "EXAMPLE_TOKEN"},
                           "expectedStatusCodes": [201], "extract": {
                               "created": {"path": "$.request.method", "type": "string"}}})
    assert result.tasks["status"].values == {"created": "POST"}
    assert received["headers"]["Authorization"] == "Bearer never-return-this-secret"
    assert json.loads(received["body"]) == {"probe": "yes"}
    assert "never-return-this-secret" not in json.dumps(result.to_dict())


def test_basic_auth_reads_environment_and_never_echoes_password(monkeypatch):
    monkeypatch.setenv("HTTP_USER", "test-user")
    monkeypatch.setenv("HTTP_PASSWORD", "secret-password")
    with serve({"ok": True}) as (url, received):
        result = run_http({"type": "http", "url": url, "auth": {"type": "basic", "usernameEnv": "HTTP_USER",
                           "passwordEnv": "HTTP_PASSWORD"}, "extract": {
                               "ok": {"path": "$.ok", "type": "boolean"}}})
    assert received["headers"]["Authorization"].startswith("Basic ")
    assert "secret-password" not in json.dumps(result.to_dict())


def test_missing_auth_environment_variable_is_useful_and_safe(monkeypatch):
    monkeypatch.delenv("MISSING_API_TOKEN", raising=False)
    with serve({"ok": True}) as (url, _):
        result = run_http({"type": "http", "url": url, "auth": {"type": "bearer", "tokenEnv": "MISSING_API_TOKEN"}})
    assert result.status == "ERROR"
    assert result.tasks["status"].error == "task status: environment variable MISSING_API_TOKEN is not set"


def test_unexpected_status_is_error_and_extracts_no_body():
    with serve({"secret": "not exposed"}, status=503) as (url, _):
        result = run_http({"type": "http", "url": url, "expectedStatusCodes": [200], "extract": {
            "secret": {"path": "$.secret", "type": "string"}}})
    assert result.status == "ERROR"
    assert result.tasks["status"].values == {"reachable": False, "statusCode": 503}
    assert "not exposed" not in json.dumps(result.to_dict())


def test_invalid_json_and_non_json_extraction_fail_safely():
    with serve(b"<html>private response</html>", content_type="text/html") as (url, _):
        result = run_http({"type": "http", "url": url, "extract": {"x": {"path": "$.x", "type": "string"}}})
    assert result.status == "ERROR"
    assert result.tasks["status"].error == "task status: response is not valid JSON"
    assert "private response" not in json.dumps(result.to_dict())


def test_oversized_response_is_rejected():
    with serve({"long": "x" * 100}) as (url, _):
        result = run_http({"type": "http", "url": url, "maxResponseBytes": 32,
                           "extract": {"x": {"path": "$.long", "type": "string"}}})
    assert result.status == "ERROR"
    assert "response exceeds maxResponseBytes" in result.tasks["status"].error


def test_http_timeout_is_bounded():
    with serve({"ok": True}, delay=.3) as (url, _):
        started = time.monotonic()
        result = run_http({"type": "http", "url": url, "timeout": "100ms"})
        duration = time.monotonic() - started
    assert result.status == "ERROR"
    assert duration < .28


@pytest.mark.parametrize("config", [
    {"type": "http", "url": "ftp://example.invalid"},
    {"type": "http", "url": "http://example.invalid", "method": "DELETE"},
    {"type": "http", "url": "http://example.invalid", "headers": ["bad"]},
    {"type": "http", "url": "http://example.invalid", "maxResponseBytes": 9_000_000},
    {"type": "http", "url": "http://example.invalid", "expectedStatusCodes": []},
    {"type": "http", "url": "http://example.invalid", "auth": {"type": "bearer", "tokenEnv": "bad-name"}},
    {"type": "http", "url": "http://example.invalid", "body": {"x": 1}},
])
def test_http_configuration_validation(config):
    with pytest.raises(ConfigError):
        validate_task("status", config)


def test_connection_refused_and_invalid_credentials_are_safe():
    from http.server import HTTPServer

    server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_port
    server.server_close()
    refused = run_http({"type": "http", "url": f"http://127.0.0.1:{port}/"})
    assert refused.status == "ERROR"
    assert refused.tasks["status"].error == "task status: HTTP connection failed"

    with serve({"error": "credential rejected: private-value"}, status=401) as (url, _):
        denied = run_http({"type": "http", "url": url})
    assert denied.status == "ERROR"
    assert denied.tasks["status"].values == {"reachable": False, "statusCode": 401}
    assert "private-value" not in json.dumps(denied.to_dict())
