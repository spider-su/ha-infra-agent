import json
import subprocess
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
    monkeypatch.setattr("subprocess.run", lambda *a, **k: Completed())
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


def test_mqtt_alias_ownership_is_stable_when_one_configured_task_fails(tmp_path, monkeypatch):
    import home_infra_agent.core as core

    class Values:
        def execute(self, task_id, config, timeout):
            if config.get("fail"):
                raise RuntimeError("unavailable")
            return "OK", {"a": "a", "b": "b"}

    monkeypatch.setitem(core.PROVIDERS, "http", Values())
    tasks = {
        "a": {"type": "http", "url": "https://unused", "extract": {"same": {"path": "$.a", "type": "string"}}},
        "b": {"type": "http", "url": "https://unused", "extract": {"same": {"path": "$.b", "type": "string"}}},
    }
    job = Job("stable", "Stable", tmp_path, {"mqtt": {"topic": "home/stable"}}, tasks)
    healthy = job.run()
    failed_job = Job("stable", "Stable", tmp_path, {"mqtt": {"topic": "home/stable"}},
                     {**tasks, "a": {**tasks["a"], "fail": True}})
    failed = failed_job.run()
    assert "same" not in healthy.values and "same" not in failed.values
    assert failed.values["a.same"] is None and failed.values["b.same"] == "b"

    def entity_ids(result):
        return {json.loads(payload)["unique_id"] for _, payload in discovery_configs(job, result)
                if json.loads(payload)["unique_id"].endswith(("_a_same", "_b_same", "_same"))}

    assert entity_ids(healthy) == entity_ids(failed)
    assert "home_infra_agent_stable_same" not in entity_ids(failed)


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
    monkeypatch.setattr("subprocess.run", fake_ping)
    status, values = PingProvider().execute("nodes", {"targets": {f"n{i}": "host" for i in range(20)}}, 2)
    assert status == "OK" and values["online"] == values["total"] == 20
    assert 1 < peak <= 8


def test_ping_uses_one_overall_deadline_across_target_batches(monkeypatch):
    import time

    calls = []
    def timed_out(args, **kwargs):
        calls.append(kwargs["timeout"])
        time.sleep(kwargs["timeout"])
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr("subprocess.run", timed_out)
    started = time.monotonic()
    status, values = PingProvider().execute(
        "nodes", {"targets": {f"node{i}": "host" for i in range(20)}}, .15)
    duration = time.monotonic() - started
    assert status == "ERROR" and values["online"] == 0
    assert duration < .4
    assert calls and all(0 < timeout <= .15 for timeout in calls)


def test_ping_provider_annotations_resolve():
    from typing import Any, Mapping, get_type_hints
    from home_infra_agent.providers.ping import PingProvider

    assert get_type_hints(PingProvider.execute)["config"] == Mapping[str, Any]


def test_state_freshness_and_failure_clear_known_metrics():
    old = "2000-01-01T00:00:00+00:00"
    payload = json.loads(state_payload("j", {"status": "ERROR", "timestamp": old,
        "lastSuccess": old, "values": {"error": "RuntimeError"}}, 60, {"metric", "other"}))
    assert payload["freshness"]["status"] == "STALE"
    assert payload["values"]["metric"] is None and payload["values"]["other"] is None


def test_fractional_freshness_duration_matches_state_and_home_assistant_expiry():
    from datetime import datetime, timezone
    from home_infra_agent.mqtt import freshness_data

    job = Job("fractional", "Fractional", Path("."), {"freshness": {"maxAge": "0.5m"}}, {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = {"status": "OK", "timestamp": now, "lastSuccess": now, "values": {}}
    freshness = freshness_data(job, result)
    configs = [json.loads(payload) for _, payload in discovery_configs(job, type("Result", (), {"values": {}})())]

    assert freshness == {"status": "FRESH", "ageSeconds": 0, "maxAgeSeconds": 30}
    assert all(config["expire_after"] == 30 for config in configs)


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


def test_failed_ping_run_keeps_configured_binary_sensor_topic():
    job = Job("proxmox", "Proxmox", Path("."), {}, {
        "nodes": {"type": "ping", "targets": {"home-lab-0": "192.0.2.1"}},
    })
    healthy = type("Result", (), {"values": {"nodes.home-lab-0": "UP", "home-lab-0": "UP"}})()
    failed = type("Result", (), {"values": {"nodes.home-lab-0": None, "home-lab-0": None}})()
    healthy_topics = {topic for topic, _ in discovery_configs(job, healthy)}
    failed_topics = {topic for topic, _ in discovery_configs(job, failed)}
    expected = "homeassistant/binary_sensor/home_infra_agent_proxmox_home_lab_0/config"
    assert expected in healthy_topics and expected in failed_topics


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
    server = AgentServer(("127.0.0.1", 0), engine, adapter, allowed_hosts=["api.example"])
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
        ingress = Request(base + "/health", headers={"Host": "ha-infra.home.k3s.com"})
        assert json.loads(urlopen(ingress).read())["status"] == "ok"
        configured_host = Request(base + "/health", headers={"Host": "api.example"})
        assert json.loads(urlopen(configured_host).read())["status"] == "ok"
        bad_host = Request(base + "/health", headers={"Host": "attacker.example"})
        with pytest.raises(HTTPError) as err:
            urlopen(bad_host)
        assert err.value.code == 400
        deep_route = Request(base + "/api/jobs/j/extra")
        with pytest.raises(HTTPError) as err:
            urlopen(deep_route)
        assert err.value.code == 404
        spoofed_origin = Request(base + "/api/jobs/j/run", method="POST",
                                 headers={"Host": "ha-infra.home.k3s.com", "Origin": "https://evil.example"})
        with pytest.raises(HTTPError) as err:
            urlopen(spoofed_origin)
        assert err.value.code == 403
    finally:
        server.shutdown(); server.server_close(); thread.join(2)


def test_mqtt_publish_deduplicates_discovery_and_clean_shutdown_is_offline():
    class Info:
        def wait_for_publish(self, timeout=None): pass
    class Client:
        def __init__(self): self.messages = []; self.stopped = False; self.disconnected = False; self.lifecycle = []
        def publish(self, topic, payload, qos, retain):
            self.messages.append((topic, payload, qos, retain)); return Info()
        def loop_stop(self): self.stopped = True; self.lifecycle.append("loop_stop")
        def disconnect(self): self.disconnected = True; self.lifecycle.append("disconnect")
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
    assert client.lifecycle == ["disconnect", "loop_stop"]
    adapter.publish(job, result)
    assert "proxmox" not in adapter._pending
    availability = [message[1] for message in client.messages if message[0] == "home-infra-agent/availability"]
    assert "online" in availability and all(message[3] for message in client.messages)
    assert availability[-1] == "offline"


def test_explicit_mqtt_entity_metadata_controls_component_and_payloads():
    job = Job("service", "Service", Path("."), {"mqtt": {"entities": {
        "state": {"name": "Service state", "component": "binary_sensor",
                  "payload_on": "READY", "payload_off": "DOWN", "icon": "mdi:heart-pulse"}}}}, {})
    result = type("Result", (), {"values": {"state": "READY"}})()
    configs = discovery_configs(job, result)
    topic, payload = next((topic, body) for topic, body in configs if "home_infra_agent_service_state/config" in topic)
    config = json.loads(payload)
    assert topic.startswith("homeassistant/binary_sensor/")
    assert config["unique_id"] == "home_infra_agent_service_state"
    assert config["name"] == "Service state"
    assert config["payload_on"] == "READY" and config["payload_off"] == "DOWN"
    assert config["icon"] == "mdi:heart-pulse"


def test_health_and_job_api_expose_safe_operational_diagnostics(tmp_path):
    from datetime import datetime, timezone
    from urllib.request import urlopen
    from home_infra_agent.app import AgentServer
    from home_infra_agent.core import JobResult, TaskResult

    secret = "private-config-value"
    good = Job("good", "Healthy Job", tmp_path, {
        "freshness": {"maxAge": "60s"}, "mqtt": {"password": secret},
    }, {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stale_success = "2000-01-01T00:00:00+00:00"
    good.last_result = JobResult("good", "OK", now, 12, {"ready": True}, {}, now)
    good.last_attempt = now
    stale = Job("stale", "Stale Job", tmp_path, {"freshness": {"maxAge": "60s"}}, {})
    stale.last_result = JobResult("stale", "ERROR", now, 8, {}, {}, stale_success)
    stale.last_attempt = now
    failed = Job("failed", "Failed Job", tmp_path, {"freshness": {"maxAge": "60s"}}, {})
    failed.last_result = JobResult("failed", "ERROR", now, 8, {}, {
        "probe": TaskResult("probe", "ERROR", now, 8, {}, "TimeoutError (details redacted)")
    })
    failed.last_attempt = now
    never = Job("never", "Never Run", tmp_path, {"freshness": {"maxAge": "60s"}}, {})
    bad = Job("bad", "Invalid Job", tmp_path, {}, {}, False, "invalid")
    adapter = type("Adapter", (), {"is_connected": False})()
    server = AgentServer(("127.0.0.1", 0), JobEngine([good, stale, failed, never, bad]), adapter)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base + "/health") as response:
            health = json.load(response)
        assert health["status"] == "ok" and health["processAlive"] is True
        assert health["mqttConnected"] is False and health["schedulerRunning"] is False
        assert health["loadedJobs"] == 5 and health["invalidJobs"] == 1
        observed = datetime.fromisoformat(health["observedAt"])
        assert observed.tzinfo is not None and observed.utcoffset().total_seconds() == 0

        with urlopen(base + "/api/jobs") as response:
            jobs = json.load(response)
        good_summary = next(job for job in jobs if job["id"] == "good")
        assert {"id", "name", "valid", "status", "lastRun", "lastSuccess", "freshness"} <= good_summary.keys()
        assert good_summary["valid"] is True
        assert good_summary["status"] == "OK"
        assert good_summary["executionStatus"] == "OK" and good_summary["running"] is False
        assert good_summary["neverExecuted"] is False
        assert good_summary["lastRun"] == now
        assert good_summary["lastAttempt"] == now
        assert good_summary["lastSuccess"] == now
        assert good_summary["failureReason"] is None
        assert good_summary["freshness"]["status"] == "FRESH"
        stale_summary = next(job for job in jobs if job["id"] == "stale")
        assert stale_summary["status"] == "ERROR"
        assert stale_summary["lastSuccess"] == stale_success
        assert stale_summary["freshness"]["status"] == "STALE"
        assert stale_summary["failureReason"] == "one or more tasks reported ERROR"
        failed_summary = next(job for job in jobs if job["id"] == "failed")
        assert failed_summary["status"] == "ERROR"
        assert failed_summary["failureReason"] == "probe: TimeoutError (details redacted)"
        never_summary = next(job for job in jobs if job["id"] == "never")
        assert never_summary["status"] == "UNKNOWN" and never_summary["neverExecuted"] is True
        assert never_summary["lastAttempt"] is None and never_summary["lastSuccess"] is None
        assert never_summary["freshness"]["status"] == "UNKNOWN"
        invalid_summary = next(job for job in jobs if job["id"] == "bad")
        assert invalid_summary["valid"] is False and invalid_summary["failureReason"] == "invalid"

        with urlopen(base + "/api/jobs/good") as response:
            detail = json.load(response)
        assert detail["executionStatus"] == "OK" and detail["lastAttempt"] == now
        assert detail["freshness"]["status"] == "FRESH"
        with urlopen(base + "/api/jobs/stale") as response:
            stale_detail = json.load(response)
        assert stale_detail["freshness"]["status"] == "STALE"
        assert secret not in json.dumps([health, jobs, detail, stale_detail])
        assert "mqtt" not in json.dumps(detail)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_job_api_reports_running_state_without_triggering_execution(tmp_path):
    from urllib.request import urlopen
    from home_infra_agent.app import AgentServer

    job = Job("running", "Running Job", tmp_path, {}, {})
    assert job.execution_lock.acquire(blocking=False)
    server = AgentServer(("127.0.0.1", 0), JobEngine([job]), type("Adapter", (), {"is_connected": False})())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/api/jobs") as response:
            summary = json.load(response)[0]
        assert summary["running"] is True
        assert summary["executionStatus"] == "RUNNING"
        assert summary["neverExecuted"] is True
        assert job.last_result is None
    finally:
        job.execution_lock.release()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_job_freshness_is_recomputed_for_each_api_request(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from urllib.request import urlopen
    import home_infra_agent.mqtt as mqtt
    from home_infra_agent.app import AgentServer
    from home_infra_agent.core import JobResult

    class Clock:
        current = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current

        @staticmethod
        def fromisoformat(value):
            return datetime.fromisoformat(value)

    monkeypatch.setattr(mqtt, "datetime", Clock)
    job = Job("clock", "Clock Job", tmp_path, {"freshness": {"maxAge": "60s"}}, {})
    timestamp = Clock.current.isoformat()
    job.last_success = timestamp
    job.last_attempt = timestamp
    job.last_result = JobResult("clock", "OK", timestamp, 1, {}, {}, timestamp)
    server = AgentServer(("127.0.0.1", 0), JobEngine([job]), type("Adapter", (), {"is_connected": False})())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/jobs"
        with urlopen(url) as response:
            assert json.load(response)[0]["freshness"]["status"] == "FRESH"
        Clock.current += timedelta(seconds=61)
        with urlopen(url) as response:
            updated = json.load(response)[0]
        assert updated["freshness"]["status"] == "STALE"
        assert updated["freshness"]["ageSeconds"] == 61
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_empty_job_configuration_still_reports_live_agent_and_scheduler(tmp_path):
    from urllib.request import urlopen
    from home_infra_agent.app import AgentServer

    engine = JobEngine([])
    engine.start()
    server = AgentServer(("127.0.0.1", 0), engine, type("Adapter", (), {"is_connected": False})())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urlopen(base + "/health") as response:
            health = json.load(response)
        with urlopen(base + "/api/jobs") as response:
            jobs = json.load(response)
        assert health["status"] == "ok" and health["processAlive"] is True
        assert health["schedulerRunning"] is True
        assert health["loadedJobs"] == 0 and health["invalidJobs"] == 0
        assert jobs == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        engine.stop()


def test_repeated_job_failures_preserve_last_success_and_recover(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone
    import home_infra_agent.core as core

    base = datetime(2026, 10, 8, tzinfo=timezone.utc)
    ticks = iter(range(20))
    monkeypatch.setattr(core, "utc_now", lambda: (base + timedelta(seconds=next(ticks))).isoformat())
    outcomes = iter([
        ("OK", {"value": 1}),
        ("ERROR", {"source_status": "ERROR"}),
        ("ERROR", {"source_status": "ERROR"}),
        ("OK", {"value": 2}),
    ])

    class Flaky:
        def execute(self, task_id, config, timeout):
            status, values = next(outcomes)
            return status, values

    class Stable:
        def execute(self, task_id, config, timeout):
            return "OK", {"unrelated": True}

    monkeypatch.setitem(__import__("home_infra_agent.core", fromlist=["PROVIDERS"]).PROVIDERS, "flaky", Flaky())
    monkeypatch.setitem(__import__("home_infra_agent.core", fromlist=["PROVIDERS"]).PROVIDERS, "stable", Stable())
    job = Job("recovery", "Recovery", tmp_path, {}, {
        "source": {"type": "flaky"}, "unrelated": {"type": "stable"},
    })
    engine = JobEngine([job])

    first = engine.run_job("recovery")
    assert first.status == "OK" and first.last_success is not None
    last_success = first.last_success

    failed = engine.run_job("recovery")
    assert failed.status == "ERROR"
    assert failed.tasks["source"].status == "ERROR"
    assert failed.values["unrelated.unrelated"] is True
    assert failed.last_success == last_success

    repeated = engine.run_job("recovery")
    assert repeated.status == "ERROR" and repeated.last_success == last_success

    recovered = engine.run_job("recovery")
    assert recovered.status == "OK"
    assert recovered.last_success != last_success
    assert recovered.values["source.value"] == 2
    assert recovered.values["unrelated.unrelated"] is True



def test_every_task_failure_produces_error_without_a_last_success(monkeypatch, tmp_path):
    import home_infra_agent.core as core

    class Failure:
        def execute(self, task_id, config, timeout):
            raise RuntimeError("private upstream detail")

    monkeypatch.setitem(core.PROVIDERS, "all-fail", Failure())
    job = Job("all-fail", "All tasks fail", tmp_path, {}, {
        "one": {"type": "all-fail"}, "two": {"type": "all-fail"},
    })
    result = JobEngine([job]).run_job("all-fail")
    assert result.status == "ERROR"
    assert all(task.status == "ERROR" for task in result.tasks.values())
    assert result.last_success is None
    assert all(task.error == "RuntimeError (details redacted)" for task in result.tasks.values())
