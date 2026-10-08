import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from home_infra_agent.core import PROVIDERS, JobEngine, discover_jobs
from home_infra_agent.mqtt import discovery_configs, state_payload


def test_existing_job_yaml_loads_after_configuration_schema_extension():
    root = Path(__file__).parents[1]
    jobs, errors = discover_jobs(root / "config/jobs")
    assert not errors
    assert {job.id for job in jobs} == {"investory", "proxmox", "solarman"}
    from home_infra_agent.providers import PROVIDERS
    assert set(PROVIDERS) == {"ping", "http", "kubernetes", "investory_postgres", "solarman"}
    assert all(job.valid and not job.task_errors for job in jobs)


def test_http_service_example_is_a_yaml_only_integration(tmp_path):
    source = Path(__file__).parents[1] / "config/examples/http-service"
    destination = tmp_path / "jobs" / "example-service"
    destination.parent.mkdir()
    shutil.copytree(source, destination)

    response_body = b'{"status":{"online":true},"cluster":{"ready":"2","total":2},"node":{"state":"ready"},"sensor":{"temperature_centi":2075,"humidity":45},"meta":{"updatedAt":"2026-10-08T12:00:00Z"}}'

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        task_file = destination / "health.yaml"
        task = yaml.safe_load(task_file.read_text())
        task["url"] = f"http://127.0.0.1:{server.server_port}/status"
        task.pop("auth")
        task_file.write_text(yaml.safe_dump(task, sort_keys=False))
        providers_before = set(PROVIDERS)
        jobs, errors = discover_jobs(destination.parent)
        assert not errors and len(jobs) == 1
        result = JobEngine(jobs).run_job("example-service")
        assert result.status == "OK"
        assert result.values["temperature"] == 20.8
        assert result.values["nodeState"] == "UP"
        assert result.values["ready"] == result.values["total"] == 2
        state = yaml.safe_load(state_payload(result.job, result, 180))
        assert state["values"]["humidity"] == 45
        assert state["freshness"]["status"] == "FRESH"
        entities = {topic: yaml.safe_load(payload) for topic, payload in discovery_configs(jobs[0], result)}
        ready = entities["homeassistant/sensor/home_infra_agent_example-service_ready/config"]
        assert ready["name"] == "Ready nodes" and ready["unit_of_measurement"] == "nodes"
        node_state = entities["homeassistant/binary_sensor/home_infra_agent_example-service_nodeState/config"]
        assert node_state["payload_on"] == "UP" and node_state["payload_off"] == "DOWN"
        assert set(PROVIDERS) == providers_before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_invalid_task_is_reported_at_load_and_sibling_runs(tmp_path, monkeypatch):
    import home_infra_agent.core as core

    jobs_dir = tmp_path / "jobs"
    job_dir = jobs_dir / "mixed"
    job_dir.mkdir(parents=True)
    (job_dir / "job.yaml").write_text("name: Mixed\nschedule:\n  interval: 60s\n")
    (job_dir / "bad.yaml").write_text("type: made_up\n")
    (job_dir / "good.yaml").write_text("type: ping\ntargets:\n  node: 127.0.0.1\n")

    class Completed:
        returncode = 0
    monkeypatch.setattr("subprocess.run", lambda *a, **k: Completed())
    jobs, errors = discover_jobs(jobs_dir)
    assert len(errors) == 1 and "mixed/bad" in errors[0]
    assert jobs[0].valid and jobs[0].task_errors["bad"] == "task bad: unsupported type 'made_up'"
    result = JobEngine(jobs).run_job("mixed")
    assert result.status == "ERROR"
    assert result.tasks["bad"].status == "ERROR"
    assert result.tasks["good"].status == "OK"
    assert result.values["good.node"] == "UP"


def test_invalid_mqtt_entity_config_isolated_to_its_job(tmp_path):
    jobs_dir = tmp_path / "jobs"
    invalid = jobs_dir / "bad"
    valid = jobs_dir / "good"
    invalid.mkdir(parents=True)
    valid.mkdir()
    (invalid / "job.yaml").write_text("name: Bad\nmqtt:\n  entities:\n    a.b: {}\n    a-b: {}\n")
    (valid / "job.yaml").write_text("name: Good\n")
    jobs, errors = discover_jobs(jobs_dir)
    assert len(jobs) == 2 and len(errors) == 1
    assert not jobs[0].valid and jobs[1].valid


def test_job_duration_validation_rejects_invalid_values_and_accepts_supported_units(tmp_path):
    for field, value in (("timeout", "0s"), ("interval", "-1m"), ("maxAge", "NaN")):
        jobs_dir = tmp_path / field / "jobs"
        job_dir = jobs_dir / "sample"
        job_dir.mkdir(parents=True)
        if field == "interval":
            content = f"name: Sample\nschedule:\n  interval: {value}\n"
        elif field == "maxAge":
            content = f"name: Sample\nfreshness:\n  maxAge: {value}\n"
        else:
            content = f"name: Sample\ntimeout: {value}\n"
        (job_dir / "job.yaml").write_text(content)
        jobs, errors = discover_jobs(jobs_dir)
        assert errors and len(jobs) == 1 and not jobs[0].valid

    jobs_dir = tmp_path / "valid" / "jobs"
    job_dir = jobs_dir / "sample"
    job_dir.mkdir(parents=True)
    (job_dir / "job.yaml").write_text(
        "name: Sample\ntimeout: 1h\nschedule:\n  interval: 500ms\nfreshness:\n  maxAge: 2h\n"
    )
    jobs, errors = discover_jobs(jobs_dir)
    assert not errors and jobs[0].valid
    assert jobs[0].interval == 0.5 and jobs[0].timeout == 3600


def test_mqtt_password_must_be_loaded_from_environment(tmp_path, monkeypatch):
    from home_infra_agent.app import load_app_config

    path = tmp_path / "application.yaml"
    path.write_text("mqtt:\n  password: inline-secret\n")
    try:
        load_app_config(path)
    except ValueError as exc:
        assert "passwordEnv" in str(exc)
        assert "inline-secret" not in str(exc)
    else:
        raise AssertionError("inline MQTT password was accepted")

    path.write_text("mqtt:\n  passwordEnv: HIA_TEST_MQTT_PASSWORD\n")
    monkeypatch.setenv("HIA_TEST_MQTT_PASSWORD", "env-secret")
    assert load_app_config(path)["mqtt"]["password"] == "env-secret"
