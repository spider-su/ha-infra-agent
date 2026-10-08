"""Configuration, task providers, scheduling, and normalized result models."""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

import yaml

log = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class TaskResult:
    task: str
    status: str
    timestamp: str
    duration_ms: int
    values: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {"task": self.task, "status": self.status, "timestamp": self.timestamp,
                  "durationMs": self.duration_ms, "values": dict(self.values)}
        if self.error:
            result["error"] = self.error
        return result


@dataclass(frozen=True)
class JobResult:
    job: str
    status: str
    timestamp: str
    duration_ms: int
    values: Mapping[str, Any]
    tasks: Mapping[str, TaskResult]
    last_success: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"job": self.job, "status": self.status, "timestamp": self.timestamp,
                "durationMs": self.duration_ms, "lastSuccess": self.last_success,
                "values": dict(self.values), "tasks": {k: v.to_dict() for k, v in self.tasks.items()}}


class TaskProvider:
    """Provider extension point: validate a task config, then return normalized values."""
    def execute(self, task_id: str, config: Mapping[str, Any], timeout: float) -> tuple[str, dict[str, Any]]:
        raise NotImplementedError


class PingProvider(TaskProvider):
    def execute(self, task_id: str, config: Mapping[str, Any], timeout: float) -> tuple[str, dict[str, Any]]:
        targets = config.get("targets")
        if not isinstance(targets, dict) or not targets:
            raise ConfigError(f"task {task_id}: targets must be a non-empty mapping")
        values: dict[str, Any] = {}
        for name, host in targets.items():
            if not isinstance(host, str) or not host.strip():
                values[str(name)] = "DOWN"
                continue
            try:
                completed = subprocess.run(["ping", "-c", "1", "-W", str(max(1, int(timeout))), host],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           timeout=timeout + 1, check=False)
                values[str(name)] = "UP" if completed.returncode == 0 else "DOWN"
            except (OSError, subprocess.TimeoutExpired):
                values[str(name)] = "DOWN"
        online = sum(value == "UP" for value in values.values())
        values.update(online=online, total=len(targets))
        return ("OK" if online == len(targets) else "WARN" if online else "ERROR"), values


class HttpProvider(TaskProvider):
    def execute(self, task_id: str, config: Mapping[str, Any], timeout: float) -> tuple[str, dict[str, Any]]:
        url = config.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ConfigError(f"task {task_id}: url must be an HTTP(S) URL")
        method = str(config.get("method", "GET")).upper()
        request = urllib.request.Request(url, method=method, headers=config.get("headers") or {})
        status_code = 0
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status_code = response.status
                response.read(1)
        except urllib.error.HTTPError as exc:
            status_code = exc.code
        values = {"reachable": 200 <= status_code < 400, "statusCode": status_code}
        return ("OK" if values["reachable"] else "ERROR"), values


class KubernetesProvider(TaskProvider):
    """Read aggregate cluster health through the pod's narrowly scoped service account."""

    token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    page_limit = 500
    max_pages = 100

    def _list(self, api_path: str, timeout: float, token: str, context: ssl.SSLContext) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        continuation = ""
        seen_tokens: set[str] = set()
        pages = 0
        while True:
            query = {"limit": self.page_limit}
            if continuation:
                query["continue"] = continuation
            request = urllib.request.Request(
                f"https://kubernetes.default.svc{api_path}?{urlencode(query)}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                    payload = json.load(response)
            except urllib.error.HTTPError as exc:
                raise RuntimeError(f"Kubernetes API returned HTTP {exc.code} for {api_path}") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Kubernetes API request failed for {api_path}: {exc}") from exc
            page_items = payload.get("items")
            if not isinstance(page_items, list):
                raise RuntimeError(f"Kubernetes API response for {api_path} has no items list")
            items.extend(item for item in page_items if isinstance(item, dict))
            continuation = str(payload.get("metadata", {}).get("continue", ""))
            if not continuation:
                return items
            if continuation in seen_tokens or pages >= self.max_pages:
                raise RuntimeError(f"Kubernetes API pagination did not finish for {api_path}")
            seen_tokens.add(continuation)
            pages += 1

    def execute(self, task_id: str, config: Mapping[str, Any], timeout: float) -> tuple[str, dict[str, Any]]:
        try:
            token = self.token_path.read_text().strip()
            context = ssl.create_default_context(cafile=str(self.ca_path))
        except OSError as exc:
            raise RuntimeError("in-cluster service account credentials are unavailable") from exc
        if not token:
            raise RuntimeError("in-cluster service account token is empty")

        nodes = self._list("/api/v1/nodes", timeout, token, context)
        pods = self._list("/api/v1/pods", timeout, token, context)
        deployments = self._list("/apis/apps/v1/deployments", timeout, token, context)
        statefulsets = self._list("/apis/apps/v1/statefulsets", timeout, token, context)
        daemonsets = self._list("/apis/apps/v1/daemonsets", timeout, token, context)

        def desired(item: Mapping[str, Any]) -> int:
            return int(item.get("spec", {}).get("replicas", 1) or 0)

        nodes_ready = 0
        values: dict[str, Any] = {}
        for node in nodes:
            name = str(node.get("metadata", {}).get("name", "unknown"))
            ready = any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in (node.get("status", {}).get("conditions") or [])
            )
            nodes_ready += int(ready)
            safe_name = "_".join(part for part in "".join(
                ch if ch.isascii() and ch.isalnum() else "_" for ch in name
            ).split("_") if part)
            values[f"node_{safe_name or 'unknown'}"] = "UP" if ready else "DOWN"

        pod_phases: dict[str, int] = {"Running": 0, "Pending": 0, "Failed": 0, "Unknown": 0, "Succeeded": 0}
        pods_not_ready = 0
        for pod in pods:
            pod_status = pod.get("status", {})
            phase = str(pod_status.get("phase", "Unknown"))
            pod_phases[phase if phase in pod_phases else "Unknown"] += 1
            if phase == "Running" and not pod.get("metadata", {}).get("deletionTimestamp"):
                ready = any(
                    condition.get("type") == "Ready" and condition.get("status") == "True"
                    for condition in (pod_status.get("conditions") or [])
                )
                pods_not_ready += int(not ready)

        deployments_ready = sum(
            int(item.get("status", {}).get("availableReplicas", 0) or 0) >= desired(item)
            for item in deployments
        )
        statefulsets_ready = sum(
            int(item.get("status", {}).get("readyReplicas", 0) or 0) >= desired(item)
            for item in statefulsets
        )
        daemonsets_ready = sum(
            int(item.get("status", {}).get("numberReady", 0) or 0)
            >= int(item.get("status", {}).get("desiredNumberScheduled", 0) or 0)
            for item in daemonsets
        )
        workloads_healthy = (
            bool(nodes) and nodes_ready == len(nodes)
            and deployments_ready == len(deployments)
            and statefulsets_ready == len(statefulsets)
            and daemonsets_ready == len(daemonsets)
            and pod_phases["Pending"] == 0 and pod_phases["Unknown"] == 0 and pods_not_ready == 0
        )
        values.update({
            "nodesReady": nodes_ready,
            "nodesTotal": len(nodes),
            "podsRunning": pod_phases["Running"],
            "podsNotReady": pods_not_ready,
            "podsPending": pod_phases["Pending"],
            "podsFailed": pod_phases["Failed"],
            "podsUnknown": pod_phases["Unknown"],
            "deploymentsAvailable": deployments_ready,
            "deploymentsTotal": len(deployments),
            "statefulsetsReady": statefulsets_ready,
            "statefulsetsTotal": len(statefulsets),
            "daemonsetsReady": daemonsets_ready,
            "daemonsetsTotal": len(daemonsets),
            "workloadsStatus": "HEALTHY" if workloads_healthy else "DEGRADED",
        })
        status = "OK" if workloads_healthy else "ERROR" if not nodes or nodes_ready == 0 else "WARN"
        return status, values


PROVIDERS: dict[str, TaskProvider] = {
    "ping": PingProvider(), "http": HttpProvider(), "kubernetes": KubernetesProvider(),
}


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{path.name}: document must be a mapping")
    return value


def validate_task(task_id: str, config: Mapping[str, Any]) -> None:
    kind = config.get("type")
    if kind not in PROVIDERS:
        raise ConfigError(f"task {task_id}: unsupported type {kind!r}")
    if kind == "ping" and (not isinstance(config.get("targets"), dict) or not config["targets"]):
        raise ConfigError(f"task {task_id}: targets must be a non-empty mapping")
    if kind == "http" and not str(config.get("url", "")).startswith(("http://", "https://")):
        raise ConfigError(f"task {task_id}: url must be an HTTP(S) URL")
    if kind == "kubernetes" and config.get("scope", "cluster") != "cluster":
        raise ConfigError(f"task {task_id}: only cluster scope is supported")


@dataclass
class Job:
    id: str
    name: str
    directory: Path
    config: dict[str, Any]
    task_configs: dict[str, dict[str, Any]]
    valid: bool = True
    config_error: str | None = None
    last_result: JobResult | None = None
    last_success: str | None = None
    last_run_epoch: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def interval(self) -> float:
        return parse_duration(self.config.get("schedule", {}).get("interval", "60s"))

    @property
    def timeout(self) -> float:
        return parse_duration(self.config.get("timeout", "10s"))

    def run(self) -> JobResult:
        started = time.monotonic()
        results: dict[str, TaskResult] = {}
        if not self.valid:
            status = "ERROR"
            values: dict[str, Any] = {"error": self.config_error or "invalid job configuration"}
        else:
            for task_id, config in self.task_configs.items():
                task_start = time.monotonic()
                timestamp = utc_now()
                try:
                    validate_task(task_id, config)
                    provider = PROVIDERS[config["type"]]
                    status, task_values = provider.execute(task_id, config, self.timeout)
                    results[task_id] = TaskResult(task_id, status, timestamp,
                        int((time.monotonic() - task_start) * 1000), task_values)
                except Exception as exc:  # each task is an independent failure domain
                    log.warning("job %s task %s failed: %s", self.id, task_id, exc)
                    results[task_id] = TaskResult(task_id, "ERROR", timestamp,
                        int((time.monotonic() - task_start) * 1000), {}, str(exc))
            statuses = [result.status for result in results.values()]
            status = "UNKNOWN" if not results else "ERROR" if "ERROR" in statuses else "WARN" if "WARN" in statuses else "OK"
            values = {f"{task_id}.{key}": value for task_id, result in results.items() for key, value in result.values.items()}
            # Also expose unique value names directly for convenient HA templates/UI.
            for result in results.values():
                for key, value in result.values.items():
                    if key not in values:
                        values[key] = value
            if status == "OK":
                self.last_success = utc_now()
        result = JobResult(self.id, status, utc_now(), int((time.monotonic() - started) * 1000),
                           values, results, self.last_success)
        with self.lock:
            self.last_result = result
            self.last_run_epoch = time.time()
        return result


def parse_duration(value: Any) -> float:
    if isinstance(value, (int, float)):
        return max(0.1, float(value))
    text = str(value).strip().lower()
    try:
        if text.endswith("ms"):
            return max(.1, float(text[:-2]) / 1000)
        if text.endswith("s"):
            return max(.1, float(text[:-1]))
        if text.endswith("m"):
            return max(.1, float(text[:-1]) * 60)
        return max(.1, float(text))
    except ValueError as exc:
        raise ConfigError(f"invalid duration: {value!r}") from exc


def discover_jobs(jobs_dir: Path) -> tuple[list[Job], list[str]]:
    jobs: list[Job] = []
    errors: list[str] = []
    for job_file in sorted(jobs_dir.glob("*/job.yaml")):
        directory = job_file.parent
        job_id = directory.name
        try:
            config = _load_yaml(job_file)
            name = config.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ConfigError("job.yaml: name is required")
            schedule = config.get("schedule", {})
            if not isinstance(schedule, dict):
                raise ConfigError("job.yaml: schedule must be a mapping")
            parse_duration(schedule.get("interval", "60s"))
            tasks: dict[str, dict[str, Any]] = {}
            for task_file in sorted(directory.glob("*.yaml")):
                if task_file.name == "job.yaml":
                    continue
                tasks[task_file.stem] = _load_yaml(task_file)
            jobs.append(Job(job_id, name, directory, config, tasks))
        except Exception as exc:
            message = f"{job_id}: {exc}"
            log.error("invalid job configuration %s", message)
            errors.append(message)
            jobs.append(Job(job_id, job_id, directory, {}, {}, False, message))
    return jobs, errors


class JobEngine:
    def __init__(self, jobs: list[Job], on_result: Callable[[Job, JobResult], None] | None = None):
        self.jobs = {job.id: job for job in jobs}
        self.on_result = on_result
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []

    def run_job(self, job_id: str) -> JobResult:
        job = self.jobs[job_id]
        result = job.run()
        if self.on_result:
            try:
                self.on_result(job, result)
            except Exception:
                log.exception("result adapter failed for job %s", job_id)
        return result

    def start(self) -> None:
        for job in self.jobs.values():
            thread = threading.Thread(target=self._schedule, args=(job,), daemon=True, name=f"job-{job.id}")
            thread.start()
            self.threads.append(thread)

    def _schedule(self, job: Job) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.run_job(job.id)
            except Exception:
                log.exception("job scheduler failed for %s", job.id)
            delay = max(.1, job.interval - (time.monotonic() - started))
            self.stop_event.wait(delay)

    def stop(self) -> None:
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=2)
