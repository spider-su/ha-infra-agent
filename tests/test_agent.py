import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from home_infra_agent.core import ConfigError, HttpProvider, Job, JobBusyError, JobEngine, PingProvider, discover_jobs, validate_task
from home_infra_agent.mqtt import MqttAdapter, discovery_configs, state_payload


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


def test_invalid_freshness_policy_isolated(tmp_path):
    root = tmp_path / "jobs" / "bad"
    root.mkdir(parents=True)
    (root / "job.yaml").write_text("name: Bad\nfreshness:\n  maxAge: 0s\n")
    jobs, errors = discover_jobs(tmp_path / "jobs")
    assert len(errors) == 1 and not jobs[0].valid


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


def test_aliases_are_unique_and_collision_order_independent(tmp_path, monkeypatch):
    import home_infra_agent.core as core
    class Values:
        def execute(self, task_id, config, timeout): return "OK", config["values"]
    monkeypatch.setitem(core.PROVIDERS, "values", Values())
    tasks = {"z": {"type": "values", "values": {"same": 2, "z_only": 4}},
             "a": {"type": "values", "values": {"same": 1, "a_only": 3}}}
    result = Job("j", "Job", tmp_path, {}, tasks).run()
    assert result.values["a.same"] == 1 and result.values["z.same"] == 2
    assert "same" not in result.values
    assert result.values["a_only"] == 3 and result.values["z_only"] == 4
    reversed_result = Job("j", "Job", tmp_path, {}, dict(reversed(list(tasks.items())))).run()
    assert result.values == reversed_result.values


def test_job_execution_lock_rejects_duplicate_and_allows_other_job(tmp_path, monkeypatch):
    import home_infra_agent.core as core
    entered, both_entered, release = threading.Event(), threading.Event(), threading.Event()
    active = 0
    active_lock = threading.Lock()
    class Slow:
        def execute(self, task_id, config, timeout):
            nonlocal active
            with active_lock:
                active += 1
                if active == 2:
                    both_entered.set()
            entered.set()
            assert release.wait(2)
            with active_lock:
                active -= 1
            return "OK", {"done": True}
    monkeypatch.setitem(core.PROVIDERS, "slow", Slow())
    job = Job("same", "Same", tmp_path, {}, {"task": {"type": "slow"}})
    other = Job("other", "Other", tmp_path, {}, {"task": {"type": "slow"}})
    published = []
    engine = JobEngine([job, other], lambda j, r: published.append(j.id))
    thread = threading.Thread(target=engine.run_job, args=("same",))
    thread.start(); assert entered.wait(1)
    with pytest.raises(JobBusyError):
        engine.run_job("same")
    second = threading.Thread(target=engine.run_job, args=("other",))
    second.start()
    assert both_entered.wait(1), "independent Jobs should run concurrently"
    release.set()
    thread.join(2); second.join(2)
    assert not thread.is_alive() and not second.is_alive()
    assert sorted(published) == ["other", "same"]


def test_busy_lock_released_after_unexpected_job_error(tmp_path, monkeypatch):
    import home_infra_agent.core as core
    class Explodes:
        def execute(self, task_id, config, timeout): raise KeyboardInterrupt("private")
    monkeypatch.setitem(core.PROVIDERS, "explodes", Explodes())
    job = Job("j", "Job", tmp_path, {}, {"x": {"type": "explodes"}})
    engine = JobEngine([job])
    with pytest.raises(KeyboardInterrupt):
        engine.run_job("j")
    assert job.execution_lock.acquire(blocking=False)
    job.execution_lock.release()


def test_runtime_exception_details_are_redacted(tmp_path, monkeypatch):
    import home_infra_agent.core as core
    class Leaks:
        def execute(self, task_id, config, timeout): raise RuntimeError("password=secret-token")
    monkeypatch.setitem(core.PROVIDERS, "leaks", Leaks())
    result = Job("j", "Job", tmp_path, {}, {"x": {"type": "leaks"}}).run()
    assert result.tasks["x"].error == "RuntimeError (details redacted)"
    assert "secret-token" not in json.dumps(result.to_dict())


def test_ping_is_parallel_with_bounded_workers(monkeypatch):
    active = 0
    peak = 0
    guard = threading.Lock()
    class Completed:
        returncode = 0
    def fake_ping(*args, **kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        import time
        time.sleep(.03)
        with guard:
            active -= 1
        return Completed()
    monkeypatch.setattr("home_infra_agent.core.subprocess.run", fake_ping)
    status, values = PingProvider().execute("nodes", {"targets": {f"n{i}": "host" for i in range(20)}}, 2)
    assert status == "OK" and values["online"] == values["total"] == 20
    assert 1 < peak <= 8


def test_state_freshness_and_failure_clear_known_metrics():
    old = "2000-01-01T00:00:00+00:00"
    payload = json.loads(state_payload("j", {"status": "ERROR", "timestamp": old,
        "lastSuccess": old, "values": {"error": "RuntimeError"}}, 60, {"metric", "other"}))
    assert payload["freshness"]["status"] == "STALE"
    assert payload["values"]["metric"] is None and payload["values"]["other"] is None


def test_discovery_adds_freshness_entities_without_changing_existing_ids():
    job = Job("proxmox", "Proxmox Cluster", Path("."),
              {"freshness": {"maxAge": "180s"}, "mqtt": {"topic": "home/proxmox"}}, {})
    result = type("Result", (), {"values": {"online": 3, "home-lab-0": "UP"}})()
    configs = {topic: json.loads(body) for topic, body in discovery_configs(job, result)}
    old_ids = {"status", "last_run", "duration_ms", "online", "home_lab_0"}
    for key in old_ids:
        matches = [cfg for cfg in configs.values() if cfg["unique_id"] == f"home_infra_agent_proxmox_{key}"]
        assert len(matches) == 1
        assert matches[0]["state_topic"] == "home/proxmox/state"
        assert matches[0]["device"]["identifiers"] == ["home_infra_agent_proxmox"]
    assert any(cfg["unique_id"] == "home_infra_agent_proxmox_freshness" for cfg in configs.values())


def test_all_configured_job_discovery_matches_pre_change_golden_contract():
    root = Path(__file__).parents[1]
    baseline = json.loads((root / "tests/fixtures/mqtt_discovery_baseline.json").read_text())
    assert set(baseline) == {"proxmox", "investory", "solarman"}
    for job_id, expected_configs in baseline.items():
        job_config = __import__("yaml").safe_load((root / f"config/jobs/{job_id}/job.yaml").read_text())
        if job_id == "proxmox":
            values = {"nodes.home-lab-0": "UP", "nodes.home-lab-1": "UP", "nodes.home-lab-2": "UP",
                      "nodes.online": 3, "nodes.total": 3, "home-lab-0": "UP", "home-lab-1": "UP",
                      "home-lab-2": "UP", "online": 3, "total": 3}
        elif job_id == "investory":
            raw = {"portfolioId": 1, "snapshotDate": "2026-10-08", "baseCurrency": "PLN",
                   "equity": 1000.0, "totalProfit": 120.0}
            values = {**{f"portfolio.{key}": value for key, value in raw.items()}, **raw}
        else:
            from home_infra_agent.solarman import FIELDS
            raw = {key: ("Normal" if kind == "text" else 1.5) for key, (_source, kind) in FIELDS.items()}
            raw.update(source_status="FRESH", collection_time="2026-10-08T10:00:00+00:00", source_age_seconds=60)
            values = {**{f"current.{key}": value for key, value in raw.items()}, **raw}
        job = Job(job_id, job_config["name"], root / "config/jobs" / job_id, job_config, {})
        current = {topic: json.loads(payload) for topic, payload in
                   discovery_configs(job, type("Result", (), {"values": values})())}
        for record in expected_configs:
            actual = current[record["topic"]]
            expected = record["config"]
            if "expire_after" not in expected:
                actual = {key: value for key, value in actual.items() if key != "expire_after"}
            assert actual == expected, f"discovery config changed: {record['topic']}"


def test_manual_run_origin_and_ui_escape(tmp_path):
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen
    from home_infra_agent.app import AgentServer
    malicious = "<img src=x onerror=alert(1)>"
    job = Job("j", malicious, tmp_path, {}, {}, False, "invalid")
    adapter = type("Adapter", (), {"is_connected": False})()
    engine = JobEngine([job])
    server = AgentServer(("127.0.0.1", 0), engine, adapter)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        html = urlopen(base + "/").read().decode()
        assert "innerHTML" not in html and "textContent" in html
        assert "\\u003cimg" in html
        for headers in ({}, {"Origin": "https://evil.example"}):
            request = Request(base + "/api/jobs/j/run", method="POST", headers=headers)
            with pytest.raises(HTTPError) as err:
                urlopen(request)
            assert err.value.code == 403
        request = Request(base + "/api/jobs/j/run", method="POST", headers={"Origin": base})
        assert json.loads(urlopen(request).read())["status"] == "ERROR"
    finally:
        server.shutdown(); server.server_close(); thread.join(2)


def test_mqtt_publish_deduplicates_discovery_and_clean_shutdown_is_offline():
    class Info:
        def wait_for_publish(self, timeout=None): pass
    class Client:
        def __init__(self): self.messages = []; self.stopped = False; self.disconnected = False
        def publish(self, topic, payload, qos, retain):
            self.messages.append((topic, payload, qos, retain)); return Info()
        def loop_stop(self): self.stopped = True
        def disconnect(self): self.disconnected = True
    adapter = MqttAdapter({"enabled": True})
    client = Client()
    adapter.client = client
    adapter.connected.set()
    job = Job("proxmox", "Proxmox", Path("."), {"freshness": {"maxAge": "60s"},
              "mqtt": {"topic": "home/proxmox"}}, {})
    result = type("Result", (), {"values": {"online": 3}, "to_dict": lambda self: {
        "job": "proxmox", "status": "OK", "timestamp": "2026-10-08T10:00:00+00:00",
        "lastSuccess": "2026-10-08T10:00:00+00:00", "durationMs": 1,
        "values": {"online": 3}, "tasks": {}}})()
    adapter.publish(job, result)
    assert not client.messages
    adapter._publish_one(job, result)
    adapter._publish_one(job, result)
    discovery_topic = "homeassistant/sensor/home_infra_agent_proxmox_online/config"
    assert sum(topic == discovery_topic for topic, *_ in client.messages) == 1
    adapter._on_disconnect(client, None, None, 0, None)
    assert not adapter.is_connected
    adapter._on_connect(client, None, None, 0, None)
    assert adapter._pending["proxmox"] is True
    adapter._publish_one(job, result, force_discovery=True)
    assert sum(topic == discovery_topic for topic, *_ in client.messages) == 2
    adapter.stop()
    assert not adapter.is_connected and client.stopped and client.disconnected
    availability = [message[1] for message in client.messages if message[0] == "home-infra-agent/availability"]
    assert "online" in availability and all(message[3] for message in client.messages)
    assert availability[-1] == "offline"
