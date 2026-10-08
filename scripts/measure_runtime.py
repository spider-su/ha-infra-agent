#!/usr/bin/env python3
"""Measure a local container with one periodic HTTP Job; requires Docker."""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "home-infra-agent:stage4"


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        time.sleep(0.15)  # model a small but non-zero remote request
        body = b'{"state":"UP"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def docker(*args: str, timeout: int = 30) -> str:
    return subprocess.check_output(["docker", *args], text=True, timeout=timeout).strip()


def read_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=2) as response:
        return json.load(response)


def main() -> None:
    if not shutil.which("docker"):
        raise SystemExit("Docker is required")
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
        raise SystemExit(f"Build {IMAGE} first with: docker build -t {IMAGE} .")

    temp_path = Path(tempfile.mkdtemp(prefix=".hia-measure-", dir=ROOT))
    container = f"hia-measure-{uuid.uuid4().hex[:8]}"
    server = ThreadingHTTPServer(("0.0.0.0", free_port()), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    jobs = temp_path / "jobs" / "demo"
    jobs.mkdir(parents=True)
    (temp_path / "application.yaml").write_text("mqtt:\n  enabled: false\n")
    (jobs / "job.yaml").write_text(
        "name: Measurement demo\nschedule:\n  interval: 2s\ntimeout: 3s\n"
        "freshness:\n  maxAge: 10s\nmqtt:\n  topic: home/measurement-demo\n"
    )
    (jobs / "health.yaml").write_text(
        "type: http\nurl: http://host.docker.internal:PORT/health\ntimeout: 2s\n"
        "extract:\n  serviceState:\n    path: $.state\n    type: string\n".replace(
            "PORT", str(server.server_port)
        )
    )

    try:
        agent_port = free_port()
        started = time.monotonic()
        docker(
            "run", "--detach", "--name", container,
            "--add-host=host.docker.internal:host-gateway",
            "--publish", f"127.0.0.1:{agent_port}:8080",
            "--volume", f"{temp_path}:/etc/home-infra-agent:ro",
            "--env", "HIA_HOST=0.0.0.0", IMAGE,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                health = read_json(f"http://127.0.0.1:{agent_port}/health")
                if health["loadedJobs"] == 1:
                    break
            except Exception:
                time.sleep(0.05)
        else:
            raise RuntimeError(f"agent did not start: {docker('logs', container)}")
        startup_seconds = time.monotonic() - started
        time.sleep(0.5)

        def stats() -> str:
            return docker("stats", "--no-stream", "--format", "{{.CPUPerc}} {{.MemUsage}} {{.PIDs}}", container)

        thread_script = (
            "import glob; p=next(p for p in glob.glob('/proc/[0-9]*/cmdline') "
            "if b'home-infra-agent' in open(p,'rb').read()); "
            "print(open('/proc/'+p.split('/')[2]+'/status').read().split('Threads:\\t')[1].splitlines()[0])"
        )
        thread_count = docker("exec", container, "python", "-c", thread_script)
        idle = stats()
        workload_samples = []
        for _ in range(10):
            time.sleep(1)
            workload_samples.append(stats())
        detail = read_json(f"http://127.0.0.1:{agent_port}/api/jobs/demo")
        result = detail.get("result") or {}
        if result.get("status") != "OK":
            raise RuntimeError(f"fixture Job failed: {result}")
        print(json.dumps({
            "image": IMAGE,
            "startupSeconds": round(startup_seconds, 3),
            "loadedJobs": health["loadedJobs"],
            "idleDockerStats": idle,
            "processThreads": int(thread_count),
            "workloadDockerStatsSamples": workload_samples,
            "jobDurationMs": result.get("durationMs"),
            "jobStatus": result.get("status"),
            "jobFreshness": detail.get("freshness"),
        }, indent=2))
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True, timeout=30)
        server.shutdown()
        server.server_close()
        shutil.rmtree(temp_path, ignore_errors=True)


if __name__ == "__main__":
    main()
