"""Configuration, task providers, scheduling, and normalized result models."""
from __future__ import annotations

import concurrent.futures
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

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


PROVIDERS: dict[str, TaskProvider] = {"ping": PingProvider(), "http": HttpProvider()}


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
