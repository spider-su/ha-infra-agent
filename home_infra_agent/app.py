"""HTTP UI and application lifecycle."""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import yaml
from dotenv import load_dotenv

from .core import JobEngine, discover_jobs
from .mqtt import MqttAdapter

log = logging.getLogger(__name__)


def load_app_config(path: Path) -> dict:
    if not path.exists():
        return {}
    config = yaml.safe_load(path.read_text()) or {}
    if not isinstance(config, dict):
        raise ValueError("application.yaml must contain a mapping")
    mqtt = config.setdefault("mqtt", {})
    if "passwordEnv" in mqtt:
        mqtt["password"] = os.environ.get(mqtt.pop("passwordEnv"), "")
    return config


class AgentServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, engine, adapter):
        self.engine, self.adapter = engine, adapter
        super().__init__(address, AgentHandler)


class AgentHandler(BaseHTTPRequestHandler):
    server: AgentServer

    def log_message(self, fmt, *args):
        log.info("http %s", fmt % args)

    def _json(self, obj, status=200):
        data = json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        jobs = self.server.engine.jobs
        if path == "/health":
            self._json({"status": "ok", "mqttConnected": self.server.adapter.is_connected})
        elif path == "/api/jobs":
            self._json([{"id": j.id, "name": j.name, "status": j.last_result.status if j.last_result else "UNKNOWN",
                         "lastRun": j.last_result.timestamp if j.last_result else None} for j in jobs.values()])
        elif path.startswith("/api/jobs/"):
            job = jobs.get(unquote(path.split("/")[3]))
            if job:
                self._json({"id": job.id, "name": job.name, "valid": job.valid,
                            "nextRun": job.next_run_epoch,
                            "result": job.last_result.to_dict() if job.last_result else None})
            else:
                self._json({"error": "job not found"}, 404)
        elif path == "/" or path == "/index.html":
            self._html()
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) == 4 and parts[0:2] == ["api", "jobs"] and parts[3] == "run":
            job_id = unquote(parts[2])
            if job_id not in self.server.engine.jobs:
                self._json({"error": "job not found"}, 404)
                return
            result = self.server.engine.run_job(job_id)
            self._json(result.to_dict())
        else:
            self._json({"error": "not found"}, 404)

    def _html(self):
        data = [{"id": job.id, "name": job.name} for job in self.server.engine.jobs.values()]
        payload = json.dumps(data).replace("<", "\\u003c")
        html = f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Home Infra Agent</title>
<style>body{{font:16px system-ui;max-width:900px;margin:2rem auto;padding:0 1rem;color:#20252b}}nav{{display:flex;gap:.5rem;flex-wrap:wrap}}button{{padding:.55rem .8rem;cursor:pointer}}.active{{font-weight:bold;background:#dcecff}}pre{{white-space:pre-wrap;background:#f4f6f8;padding:1rem;border-radius:6px}}.status{{font-weight:bold}}</style></head><body><h1>Home Infra Agent <small id="mqtt"></small></h1><nav id="tabs"></nav><main id="detail">Loading…</main>
<script>const jobs={payload};let selected=jobs[0]?.id;function tabs(){{document.querySelector('#tabs').innerHTML=jobs.map(j=>`<button class="${{j.id===selected?'active':''}}" onclick="selected='${{j.id}}';render()">${{j.name}}</button>`).join('')}}async function render(){{tabs();if(!selected)return;let r=await fetch('/api/jobs/'+encodeURIComponent(selected)),j=await r.json();let health=await (await fetch('/health')).json();document.querySelector('#mqtt').textContent='MQTT: '+(health.mqttConnected?'Connected':'Disconnected');let next=j.nextRun?new Date(j.nextRun*1000).toLocaleTimeString([],{{hour:'2-digit',minute:'2-digit'}}):'—';document.querySelector('#detail').innerHTML=`<h2>${{j.name}}</h2><div class="status">${{j.result?.status||'UNKNOWN'}}</div><p>Last run: ${{j.result?.timestamp||'Never'}} · Duration: ${{j.result?.durationMs??'—'}} ms · Next run: ~${{next}} · Last success: ${{j.result?.lastSuccess||'Never'}}</p><h3>Tasks</h3><pre>${{JSON.stringify(j.result?.tasks||{{}},null,2)}}</pre><h3>Current values</h3><pre>${{JSON.stringify(j.result?.values||{{}},null,2)}}</pre><button onclick="runNow()">Run now</button>`}}async function runNow(){{await fetch('/api/jobs/'+encodeURIComponent(selected)+'/run',{{method:'POST'}});render()}}render();setInterval(render,15000)</script></body></html>'''
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    load_dotenv(override=False)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config_dir = Path(os.getenv("HIA_CONFIG_DIR", "/etc/home-infra-agent"))
    app_config = load_app_config(config_dir / "application.yaml")
    jobs_dir = Path(os.getenv("HIA_JOBS_DIR", str(config_dir / "jobs")))
    jobs, errors = discover_jobs(jobs_dir)
    for error in errors:
        log.error("configuration: %s", error)
    adapter = MqttAdapter(app_config.get("mqtt", {}))
    adapter.start()
    engine = JobEngine(jobs, adapter.publish)
    engine.start()
    host = os.getenv("HIA_HOST", app_config.get("web", {}).get("host", "127.0.0.1"))
    port = int(os.getenv("HIA_PORT", app_config.get("web", {}).get("port", 8080)))
    server = AgentServer((host, port), engine, adapter)
    stopped = threading.Event()
    def stop(*_):
        if not stopped.is_set():
            stopped.set()
            threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("serving %d jobs on %s:%d", len(jobs), host, port)
    try:
        server.serve_forever(poll_interval=1)
    finally:
        server.server_close()
        engine.stop()
        adapter.stop()


if __name__ == "__main__":
    main()
