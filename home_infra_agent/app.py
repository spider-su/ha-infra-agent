"""HTTP UI and application lifecycle."""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, urlsplit

import yaml
from dotenv import load_dotenv

from .core import JobBusyError, JobEngine, utc_now
from .config import discover_jobs
from .mqtt import MqttAdapter, freshness_data

log = logging.getLogger(__name__)


def load_app_config(path: Path) -> dict:
    if not path.exists():
        return {}
    config = yaml.safe_load(path.read_text()) or {}
    if not isinstance(config, dict):
        raise ValueError("application.yaml must contain a mapping")
    mqtt = config.setdefault("mqtt", {})
    if "password" in mqtt:
        raise ValueError("mqtt.password is not allowed; use passwordEnv and provide the value through the environment")
    if "passwordEnv" in mqtt:
        mqtt["password"] = os.environ.get(mqtt.pop("passwordEnv"), "")
    return config


class AgentServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, engine, adapter, allowed_hosts=()):
        self.engine, self.adapter = engine, adapter
        configured_hosts = allowed_hosts or ()
        if isinstance(configured_hosts, str):
            configured_hosts = configured_hosts.split(",")
        self.allowed_hosts = {"localhost", "127.0.0.1", "::1", "ha-infra.home.k3s.com"}
        self.allowed_hosts.update(str(host).strip().lower().rstrip(".")
                                  for host in configured_hosts if str(host).strip())
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

    @staticmethod
    def _job_summary(job):
        with job.lock:
            result = job.last_result
            last_attempt = job.last_attempt
            last_success = result.last_success if result else job.last_success
        running = job.execution_lock.locked()
        status = result.status if result else "UNKNOWN"
        failure_reason = job.config_error if not job.valid else None
        if result and failure_reason is None:
            failures = [f"{task_id}: {task.error or task.status}"
                        for task_id, task in result.tasks.items()
                        if task.status in {"ERROR", "WARN", "UNKNOWN"}]
            if failures:
                failure_reason = "; ".join(failures)
            elif status in {"ERROR", "WARN"}:
                failure_reason = f"one or more tasks reported {status}"
        return {
            "id": job.id,
            "name": job.name,
            "valid": job.valid,
            "status": status,
            "executionStatus": "RUNNING" if running else status,
            "running": running,
            "neverExecuted": result is None,
            "lastRun": result.timestamp if result else None,
            "lastAttempt": last_attempt,
            "lastSuccess": last_success,
            "failureReason": failure_reason,
            "freshness": freshness_data(job, result),
        }

    def _host_allowed(self):
        raw_host = self.headers.get("Host", "")
        if not raw_host or "@" in raw_host or any(char in raw_host for char in "/\\?#"):
            return False
        try:
            parsed = urlsplit("//" + raw_host)
            if not parsed.hostname:
                return False
            _ = parsed.port
        except ValueError:
            return False
        hostname = parsed.hostname.lower().rstrip(".")
        if hostname in self.server.allowed_hosts:
            return True
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return False
        tailscale = address in ipaddress.ip_network("100.64.0.0/10")
        return (address.is_private or address.is_loopback or address.is_link_local or tailscale) \
            and not (address.is_unspecified or address.is_multicast or address.is_reserved)

    def do_GET(self):
        if not self._host_allowed():
            self._json({"error": "invalid Host header"}, 400)
            return
        path = urlparse(self.path).path
        jobs = self.server.engine.jobs
        if path == "/health":
            self._json({"status": "ok", "mqttConnected": self.server.adapter.is_connected,
                        "loadedJobs": len(jobs),
                        "invalidJobs": sum(not job.valid for job in jobs.values()),
                        "processAlive": True,
                        "schedulerRunning": self.server.engine.scheduler_running,
                        "observedAt": utc_now()})
        elif path == "/api/jobs":
            self._json([self._job_summary(job) for job in jobs.values()])
        elif path.startswith("/api/jobs/") and len(path.split("/")) == 4 and path.split("/")[3]:
            job = jobs.get(unquote(path.split("/")[3]))
            if job:
                self._json({**self._job_summary(job),
                            "nextRun": job.next_run_epoch,
                            "result": job.last_result.to_dict() if job.last_result else None})
            else:
                self._json({"error": "job not found"}, 404)
        elif path == "/" or path == "/index.html":
            self._html()
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._host_allowed():
            self._json({"error": "invalid Host header"}, 400)
            return
        parts = urlparse(self.path).path.split("/")
        if len(parts) == 5 and parts[1:3] == ["api", "jobs"] and parts[4] == "run" and parts[3]:
            if not self._same_origin():
                self._json({"error": "same-origin request required"}, 403)
                return
            job_id = unquote(parts[3])
            if job_id not in self.server.engine.jobs:
                self._json({"error": "job not found"}, 404)
                return
            try:
                result = self.server.engine.run_job(job_id)
                self._json(result.to_dict())
            except JobBusyError:
                self._json({"error": "job is already running"}, 409)
        else:
            self._json({"error": "not found"}, 404)

    def _same_origin(self):
        source = self.headers.get("Origin") or self.headers.get("Referer")
        if not source:
            return False
        try:
            source_parts = urlsplit(source)
            host_parts = urlsplit("//" + self.headers.get("Host", ""))
            if (source_parts.scheme not in {"http", "https"} or not source_parts.hostname
                    or source_parts.username is not None or source_parts.password is not None
                    or not host_parts.hostname):
                return False
            source_port = source_parts.port or (443 if source_parts.scheme == "https" else 80)
            host_port = host_parts.port or (443 if source_parts.scheme == "https" else 80)
            return (source_parts.hostname.lower().rstrip(".") == host_parts.hostname.lower().rstrip(".")
                    and source_port == host_port)
        except ValueError:
            return False

    def _html(self):
        data = [{"id": job.id, "name": job.name} for job in self.server.engine.jobs.values()]
        payload = json.dumps(data).replace("<", "\\u003c")
        html = f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Home Infra Agent</title>
<style>body{{font:16px system-ui;max-width:900px;margin:2rem auto;padding:0 1rem;color:#20252b}}nav{{display:flex;gap:.5rem;flex-wrap:wrap}}button{{padding:.55rem .8rem;cursor:pointer}}.active{{font-weight:bold;background:#dcecff}}pre{{white-space:pre-wrap;background:#f4f6f8;padding:1rem;border-radius:6px}}.status{{font-weight:bold}}</style></head><body><h1>Home Infra Agent <small id="mqtt"></small></h1><nav id="tabs"></nav><main id="detail">Loading…</main>
<script>const jobs={payload};let selected=jobs[0]?.id;const tabsEl=document.querySelector('#tabs'),detail=document.querySelector('#detail');function el(tag,text,cls){{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n}}function tabs(){{tabsEl.replaceChildren(...jobs.map(j=>{{const b=el('button',j.name,j.id===selected?'active':'');b.addEventListener('click',()=>{{selected=j.id;render()}});return b}}))}}async function render(){{tabs();if(!selected)return;try{{let r=await fetch('/api/jobs/'+encodeURIComponent(selected)),j=await r.json();let health=await (await fetch('/health')).json();document.querySelector('#mqtt').textContent='MQTT: '+(health.mqttConnected?'Connected':'Disconnected');let next=j.nextRun?new Date(j.nextRun*1000).toLocaleTimeString([],{{hour:'2-digit',minute:'2-digit'}}):'—';const content=el('div');content.append(el('h2',j.name),el('div',j.result?.status||'UNKNOWN','status'),el('p','Last run: '+(j.result?.timestamp||'Never')+' · Duration: '+(j.result?.durationMs??'—')+' ms · Next run: ~'+next+' · Last success: '+(j.result?.lastSuccess||'Never')),el('h3','Tasks'));const tasks=el('pre',JSON.stringify(j.result?.tasks||{{}},null,2));content.append(tasks,el('h3','Current values'),el('pre',JSON.stringify(j.result?.values||{{}},null,2)));const run=el('button','Run now');run.addEventListener('click',runNow);content.append(run);detail.replaceChildren(content)}}catch(e){{detail.replaceChildren(el('p','Unable to load job details.'))}}}}async function runNow(){{const response=await fetch('/api/jobs/'+encodeURIComponent(selected)+'/run',{{method:'POST',credentials:'same-origin'}});if(!response.ok){{const data=await response.json();detail.prepend(el('p',data.error||'Run failed.'))}}await render()}}render();setInterval(render,15000)</script></body></html>'''
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
    web_config = app_config.get("web", {})
    allowed_hosts = os.getenv("HIA_ALLOWED_HOSTS", web_config.get("allowedHosts", ()))
    server = AgentServer((host, port), engine, adapter, allowed_hosts)
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
        if not engine.stop(timeout=5):
            log.warning("job workers did not stop before the shutdown deadline")
        adapter.stop()


if __name__ == "__main__":
    main()
