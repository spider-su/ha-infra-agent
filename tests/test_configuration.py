import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from home_infra_agent.core import PROVIDERS, JobEngine, discover_jobs


def test_existing_job_yaml_loads_after_configuration_schema_extension():
    root = Path(__file__).parents[1]
    jobs, errors = discover_jobs(root / "config/jobs")
    assert not errors
    assert {job.id for job in jobs} == {"investory", "proxmox", "solarman"}
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
