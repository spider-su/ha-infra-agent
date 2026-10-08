import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from home_infra_agent.core import ConfigError, HttpProvider, Job, JobEngine, PingProvider, discover_jobs, validate_task
from home_infra_agent.mqtt import discovery_configs, state_payload


def test_job_discovery_and_task_parsing(tmp_path):
    d = tmp_path / "jobs" / "ok"
    d.mkdir(parents=True)
    (d / "job.yaml").write_text("name: Example\nschedule:\n  interval: 30s\n")
    (d / "hosts.yaml").write_text("type: ping\ntargets:\n  node: localhost\n")
    jobs, errors = discover_jobs(tmp_path / "jobs")
    assert not errors
    assert jobs[0].name == "Example"
    assert jobs[0].task_configs["hosts"]["targets"] == {"node": "localhost"}


def test_ping_result_map_generation(monkeypatch):
    class Completed:
        returncode = 0
    monkeypatch.setattr("home_infra_agent.core.subprocess.run", lambda *a, **k: Completed())
    status, values = PingProvider().execute("nodes", {"targets": {"a": "127.0.0.1", "b": "127.0.0.2"}}, 1)
    assert status == "OK"
    assert values == {"a": "UP", "b": "UP", "online": 2, "total": 2}


def test_http_result_map_generation():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(204); self.end_headers()
        def log_message(self, *args):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        status, values = HttpProvider().execute("health", {"url": f"http://127.0.0.1:{server.server_port}/"}, 2)
    finally:
        server.shutdown(); thread.join()
    assert status == "OK" and values == {"reachable": True, "statusCode": 204}


def test_task_exception_isolation_and_job_aggregation(monkeypatch, tmp_path):
    import home_infra_agent.core as core
    class Good:
        def execute(self, task_id, config, timeout): return "OK", {"count": 3}
    monkeypatch.setitem(core.PROVIDERS, "good", Good())
    job = Job("j", "Job", tmp_path, {"timeout": "1s"}, {
        "broken": {"type": "ping"}, "good": {"type": "good"}})
    result = job.run()
    assert result.status == "ERROR"
    assert result.tasks["broken"].status == "ERROR"
    assert result.tasks["good"].values["count"] == 3
    assert result.values["good.count"] == 3


def test_invalid_job_isolation(tmp_path):
    root = tmp_path / "jobs"
    (root / "bad").mkdir(parents=True); (root / "good").mkdir()
    (root / "bad" / "job.yaml").write_text("[broken")
    (root / "good" / "job.yaml").write_text("name: Good\n")
    jobs, errors = discover_jobs(root)
    assert len(jobs) == 2 and len(errors) == 1
    assert not jobs[0].valid and jobs[1].valid


def test_mqtt_payload_and_discovery():
    job = Job("proxmox", "Proxmox Cluster", Path("."), {"mqtt": {"topic": "home/proxmox"}}, {})
    result = type("Result", (), {"to_dict": lambda self: {"job": "proxmox", "status": "OK", "values": {"online": 3}}})()
    payload = json.loads(state_payload("proxmox", result))
    assert payload["values"]["online"] == 3
    configs = discovery_configs(job, type("R", (), {"values": {"online": 3, "home-lab-0": "UP"}})())
    assert any(topic == "homeassistant/sensor/home_infra_agent_proxmox_online/config" for topic, _ in configs)
    assert all(json.loads(body)["device"]["identifiers"] == ["home_infra_agent_proxmox"] for _, body in configs)


def test_manual_execution():
    job = Job("x", "X", Path("."), {}, {}, False, "test invalid")
    engine = JobEngine([job])
    result = engine.run_job("x")
    assert result.status == "ERROR"
    assert result.values["error"] == "test invalid"


def test_invalid_task_configuration():
    with pytest.raises(ConfigError):
        validate_task("unknown", {"type": "made-up"})
