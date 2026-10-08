"""Normalized results, Job execution, scheduling, and concurrency controls."""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ConfigError
from .mapping import MappingError, evaluate_health, extract_values
from .providers import (PROVIDERS, HttpProvider, InvestoryPostgresProvider,
                        KubernetesProvider, PingProvider, SolarmanProvider)
from .providers.base import TaskProvider

log = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobBusyError(RuntimeError):
    """Raised when a run is already active for this Job."""


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


def _cron_field(expression: str, minimum: int, maximum: int, field_name: str) -> set[int]:
    allowed: set[int] = set()
    for part in expression.split(","):
        base, slash, step_text = part.partition("/")
        try:
            step = int(step_text) if slash else 1
        except ValueError as exc:
            raise ConfigError(f"invalid cron {field_name}: {expression!r}") from exc
        if step < 1:
            raise ConfigError(f"invalid cron {field_name}: {expression!r}")
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            parts = base.split("-", 1)
            try:
                start, end = int(parts[0]), int(parts[1])
            except ValueError as exc:
                raise ConfigError(f"invalid cron {field_name}: {expression!r}") from exc
        else:
            try:
                start = int(base)
            except ValueError as exc:
                raise ConfigError(f"invalid cron {field_name}: {expression!r}") from exc
            end = maximum if slash else start
        if start < minimum or end > maximum or start > end:
            raise ConfigError(f"invalid cron {field_name}: {expression!r}")
        allowed.update(range(start, end + 1, step))
    if not allowed:
        raise ConfigError(f"invalid cron {field_name}: {expression!r}")
    return allowed


def cron_matches(expression: str, local_time: datetime) -> bool:
    fields = expression.split()
    if len(fields) != 5:
        raise ConfigError("schedule.cron must contain five fields")
    minute, hour, day, month, weekday = fields
    minutes = _cron_field(minute, 0, 59, "minute")
    hours = _cron_field(hour, 0, 23, "hour")
    days = _cron_field(day, 1, 31, "day")
    months = _cron_field(month, 1, 12, "month")
    weekdays = _cron_field(weekday, 0, 7, "weekday")
    cron_weekday = (local_time.weekday() + 1) % 7
    weekday_match = cron_weekday in weekdays or (cron_weekday == 0 and 7 in weekdays)
    day_match = local_time.day in days
    if day != "*" and weekday != "*":
        day_match = day_match or weekday_match
    else:
        day_match = day_match and weekday_match
    return (local_time.minute in minutes and local_time.hour in hours
            and day_match and local_time.month in months)


def next_cron_run(expression: str, timezone_name: str, after: datetime | None = None) -> datetime:
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"invalid schedule timezone: {timezone_name!r}") from exc
    cursor = after or datetime.now(timezone.utc)
    if cursor.tzinfo is None:
        cursor = cursor.replace(tzinfo=zone)
    cursor = cursor.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        local_time = cursor.astimezone(zone)
        if cron_matches(expression, local_time):
            return local_time
        cursor += timedelta(minutes=1)
    raise ConfigError("schedule.cron has no matching time within one year")


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
    execution_lock: threading.Lock = field(default_factory=threading.Lock)
    task_errors: dict[str, str] = field(default_factory=dict)

    @property
    def interval(self) -> float:
        return parse_duration(self.config.get("schedule", {}).get("interval", "60s"))

    @property
    def timeout(self) -> float:
        return parse_duration(self.config.get("timeout", "10s"))

    @property
    def next_run_epoch(self) -> float | None:
        schedule = self.config.get("schedule", {})
        if "cron" in schedule:
            return next_cron_run(
                str(schedule["cron"]), str(schedule.get("timezone", "UTC"))
            ).timestamp()
        return self.last_run_epoch + self.interval if self.last_run_epoch is not None else None

    def _run_unlocked(self) -> JobResult:
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
                    if task_id in self.task_errors:
                        raise ConfigError(self.task_errors[task_id])
                    provider = PROVIDERS[config["type"]]
                    task_timeout = (parse_duration(config.get("timeout", self.timeout))
                                   if config.get("type") == "http" else self.timeout)
                    status, task_values = provider.execute(task_id, config, task_timeout)
                    if config.get("type") == "http" and "extract" in config and status not in {"ERROR", "UNKNOWN"}:
                        task_values = extract_values(task_values, config["extract"])
                    status = evaluate_health(status, task_values, config.get("health"))
                    results[task_id] = TaskResult(task_id, status, timestamp,
                        int((time.monotonic() - task_start) * 1000), task_values)
                except Exception as exc:  # each task is an independent failure domain
                    safe_error = str(exc) if isinstance(exc, (ConfigError, MappingError)) else f"{type(exc).__name__} (details redacted)"
                    log.warning("job %s task %s failed: %s", self.id, task_id, safe_error)
                    results[task_id] = TaskResult(task_id, "ERROR", timestamp,
                        int((time.monotonic() - task_start) * 1000), {}, safe_error)
            statuses = [result.status for result in results.values()]
            status = "UNKNOWN" if not results else "ERROR" if "ERROR" in statuses else "WARN" if "WARN" in statuses else "OK"
            values: dict[str, Any] = {}
            aliases: dict[str, list[Any]] = {}
            for task_id in sorted(results):
                for key, value in sorted(results[task_id].values.items()):
                    values[f"{task_id}.{key}"] = value
                    aliases.setdefault(key, []).append(value)
            # Keep legacy aliases where unique; ambiguous names are omitted.
            values.update({key: items[0] for key, items in aliases.items() if len(items) == 1})
            if status in {"OK", "WARN"}:
                self.last_success = utc_now()
        result = JobResult(self.id, status, utc_now(), int((time.monotonic() - started) * 1000),
                           values, results, self.last_success)
        with self.lock:
            self.last_result = result
            self.last_run_epoch = time.time()
        return result

    def run(self) -> JobResult:
        if not self.execution_lock.acquire(blocking=False):
            raise JobBusyError(f"job {self.id} is already running")
        try:
            return self._run_unlocked()
        finally:
            self.execution_lock.release()


def parse_duration(value: Any) -> float:
    try:
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, (int, float)):
            seconds = float(value)
        else:
            text = str(value).strip().lower()
            if text.endswith("ms"):
                seconds = float(text[:-2]) / 1000
            elif text.endswith("s"):
                seconds = float(text[:-1])
            elif text.endswith("m"):
                seconds = float(text[:-1]) * 60
            elif text.endswith("h"):
                seconds = float(text[:-1]) * 3600
            else:
                seconds = float(text)
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError
        return max(.1, seconds)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ConfigError(f"invalid duration: {value!r}") from exc


class JobEngine:
    def __init__(self, jobs: list[Job], on_result: Callable[[Job, JobResult], None] | None = None):
        self.jobs = {job.id: job for job in jobs}
        self.on_result = on_result
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []

    def run_job(self, job_id: str) -> JobResult:
        job = self.jobs[job_id]
        if not job.execution_lock.acquire(blocking=False):
            raise JobBusyError(f"job {job_id} is already running")
        try:
            result = job._run_unlocked()
            if self.on_result:
                try:
                    self.on_result(job, result)
                except Exception:
                    log.exception("result adapter failed for job %s", job_id)
            return result
        finally:
            job.execution_lock.release()

    def start(self) -> None:
        for job in self.jobs.values():
            thread = threading.Thread(target=self._schedule, args=(job,), daemon=True, name=f"job-{job.id}")
            thread.start()
            self.threads.append(thread)

    def _schedule(self, job: Job) -> None:
        schedule = job.config.get("schedule", {}) if job.valid else {}
        if "cron" in schedule:
            expression = str(schedule["cron"])
            timezone_name = str(schedule.get("timezone", "UTC"))
            while not self.stop_event.is_set():
                try:
                    due = next_cron_run(expression, timezone_name)
                    delay = due.astimezone(timezone.utc).timestamp() - datetime.now(timezone.utc).timestamp()
                    if self.stop_event.wait(max(0.0, delay)):
                        return
                    self.run_job(job.id)
                except JobBusyError:
                    continue
                except Exception:
                    log.exception("job scheduler failed for %s", job.id)
                    if self.stop_event.wait(60):
                        return
            return
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.run_job(job.id)
            except JobBusyError:
                pass
            except Exception:
                log.exception("job scheduler failed for %s", job.id)
            delay = max(.1, job.interval - (time.monotonic() - started))
            self.stop_event.wait(delay)

    def stop(self) -> None:
        self.stop_event.set()
        for thread in self.threads:
            thread.join()

def validate_task(task_id: str, config: Mapping[str, Any]) -> None:
    """Compatibility export; configuration schemas are owned by config.py."""
    from .config import validate_task as validate
    validate(task_id, config)


def discover_jobs(jobs_dir: Path) -> tuple[list[Job], list[str]]:
    """Compatibility export for callers that historically imported from core."""
    from .config import discover_jobs as discover
    return discover(jobs_dir)
