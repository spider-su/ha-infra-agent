"""Configuration loading and startup validation for Jobs and Tasks."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import yaml

from .core import Job, _configured_fields, next_cron_run, parse_duration
from .errors import ConfigError
from .mapping import MappingError, validate_extractions, validate_health, validate_output_field
from .providers import PROVIDERS

log = logging.getLogger(__name__)

def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text())
    except OSError as exc:
        raise ConfigError(f"{path.name}: unable to read configuration") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name}: invalid YAML") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{path.name}: document must be a mapping")
    return value


def validate_task(task_id: str, config: Mapping[str, Any]) -> None:
    kind = config.get("type")
    if kind not in PROVIDERS:
        raise ConfigError(f"task {task_id}: unsupported type {kind!r}")
    if kind == "ping":
        targets = config.get("targets")
        if not isinstance(targets, dict) or not targets:
            raise ConfigError(f"task {task_id}: targets must be a non-empty mapping")
        if any(not validate_output_field(name) for name in targets):
            raise ConfigError(f"task {task_id}: target names must start with a letter or underscore and contain only letters, digits, underscores, or hyphens")
        if {"online", "total"} & set(targets):
            raise ConfigError(f"task {task_id}: target names cannot use the built-in online or total fields")
    if kind == "speedtest":
        unknown = set(config) - {"type", "serverId"}
        if unknown:
            raise ConfigError(f"task {task_id}: unsupported Speedtest option {sorted(unknown)[0]}")
        if "serverId" in config:
            try:
                server_id = int(config["serverId"])
                if server_id <= 0:
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"task {task_id}: serverId must be a positive integer") from exc
    if kind == "http":
        allowed_http = {"type", "url", "method", "headers", "auth", "timeout", "body",
                        "expectedStatusCodes", "maxResponseBytes", "extract", "health"}
        unknown = set(config) - allowed_http
        if unknown:
            raise ConfigError(f"task {task_id}: unsupported HTTP option {sorted(unknown)[0]}")
        parts = urlsplit(config.get("url", "")) if isinstance(config.get("url"), str) else None
        if not parts or parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ConfigError(f"task {task_id}: url must be an absolute HTTP(S) URL")
        try:
            _ = parts.port
        except ValueError as exc:
            raise ConfigError(f"task {task_id}: url has an invalid port") from exc
        if parts.username is not None or parts.password is not None:
            raise ConfigError(f"task {task_id}: credentials must use auth environment references, not URL userinfo")
        if str(config.get("method", "GET")).upper() not in {"GET", "POST"}:
            raise ConfigError(f"task {task_id}: method must be GET or POST")
        if "body" in config and str(config.get("method", "GET")).upper() != "POST":
            raise ConfigError(f"task {task_id}: body is only supported with POST")
        if "body" in config:
            try:
                json.dumps(config["body"])
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"task {task_id}: body must contain JSON values") from exc
        headers = config.get("headers", {})
        if not isinstance(headers, Mapping) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
            raise ConfigError(f"task {task_id}: headers must map strings to strings")
        try:
            parse_duration(config.get("timeout", "10s"))
        except ConfigError as exc:
            raise ConfigError(f"task {task_id}: timeout must be a positive duration") from exc
        max_bytes = config.get("maxResponseBytes", 1_048_576)
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= 5_242_880:
            raise ConfigError(f"task {task_id}: maxResponseBytes must be an integer from 1 to 5242880")
        expected = config.get("expectedStatusCodes", list(range(200, 400)))
        if not isinstance(expected, list) or not expected or any(isinstance(code, bool) or not isinstance(code, int) or not 100 <= code <= 599 for code in expected):
            raise ConfigError(f"task {task_id}: expectedStatusCodes must be a non-empty list of HTTP status codes")
        auth = config.get("auth")
        if auth is not None:
            if not isinstance(auth, Mapping):
                raise ConfigError(f"task {task_id}: auth must be a mapping")
            auth_type = auth.get("type")
            required_auth = {"type", "tokenEnv"} if auth_type == "bearer" else {"type", "usernameEnv", "passwordEnv"} if auth_type == "basic" else set()
            if not required_auth or set(auth) != required_auth:
                raise ConfigError(f"task {task_id}: auth must define bearer tokenEnv or basic usernameEnv/passwordEnv")
            if any(not isinstance(auth[field], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", auth[field]) for field in required_auth - {"type"}):
                raise ConfigError(f"task {task_id}: auth environment references must be valid variable names")
        if "extract" in config:
            try:
                validate_extractions(config["extract"], task_id)
            except MappingError as exc:
                raise ConfigError(str(exc)) from None
    if "health" in config:
        try:
            validate_health(config["health"], task_id)
        except MappingError as exc:
            raise ConfigError(str(exc)) from None
    if "extract" in config and kind != "http":
        raise ConfigError(f"task {task_id}: extract is currently supported for HTTP Tasks")
    if kind == "solarman":
        for field_name in ("appIdEnv", "appSecretEnv", "emailEnv", "passwordEnv"):
            if not isinstance(config.get(field_name), str) or not config[field_name].strip():
                raise ConfigError(f"task {task_id}: {field_name} must name an environment variable")
        serial_env = config.get("deviceSerialEnv")
        if serial_env is not None and (not isinstance(serial_env, str) or not serial_env.strip()):
            raise ConfigError(f"task {task_id}: deviceSerialEnv must name an environment variable")
        try:
            max_age = int(config.get("maxDataAgeSeconds", 900))
            if max_age < 1:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"task {task_id}: maxDataAgeSeconds must be a positive integer") from exc
    if kind == "kubernetes" and config.get("scope", "cluster") != "cluster":
        raise ConfigError(f"task {task_id}: only cluster scope is supported")
    if kind == "investory_postgres":
        if not isinstance(config.get("databaseUrlEnv"), str) or not config["databaseUrlEnv"].strip():
            raise ConfigError(f"task {task_id}: databaseUrlEnv is required")
        try:
            if int(config.get("portfolioId", 1)) != 1:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"task {task_id}: this Investory source is scoped to portfolioId 1") from exc



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
            try:
                parse_duration(config.get("timeout", "10s"))
            except ConfigError as exc:
                raise ConfigError("job.yaml: timeout must be a positive duration") from exc
            if "cron" in schedule:
                if "interval" in schedule:
                    raise ConfigError("job.yaml: schedule cannot combine cron and interval")
                timezone_name = schedule.get("timezone", "UTC")
                if not isinstance(timezone_name, str):
                    raise ConfigError("job.yaml: schedule.timezone must be a string")
                next_cron_run(str(schedule["cron"]), timezone_name)
            else:
                try:
                    parse_duration(schedule.get("interval", "60s"))
                except ConfigError as exc:
                    raise ConfigError("job.yaml: schedule.interval must be a positive duration") from exc
            freshness = config.get("freshness", {})
            if not isinstance(freshness, dict):
                raise ConfigError("job.yaml: freshness must be a mapping")
            if "maxAge" in freshness:
                try:
                    parse_duration(freshness["maxAge"])
                except ConfigError as exc:
                    raise ConfigError("job.yaml: freshness.maxAge must be a positive duration") from exc
            validate_entity_metadata(config.get("mqtt", {}), job_id)
            tasks: dict[str, dict[str, Any]] = {}
            task_errors: dict[str, str] = {}
            for task_file in sorted(directory.glob("*.yaml")):
                if task_file.name == "job.yaml":
                    continue
                task_id = task_file.stem
                try:
                    task_config = _load_yaml(task_file)
                    validate_task(task_id, task_config)
                    tasks[task_id] = task_config
                except Exception as exc:
                    message = str(exc) if isinstance(exc, (ConfigError, MappingError)) else "invalid task configuration"
                    task_errors[task_id] = message
                    tasks[task_id] = {}
                    errors.append(f"{job_id}/{task_id}: {message}")
                    log.error("invalid task configuration %s/%s: %s", job_id, task_id, message)
            validate_discovery_identifiers(tasks, config.get("mqtt", {}), job_id)
            jobs.append(Job(job_id, name, directory, config, tasks, task_errors=task_errors))
        except Exception as exc:
            message = f"{job_id}: {exc}"
            log.error("invalid job configuration %s", message)
            errors.append(message)
            jobs.append(Job(job_id, job_id, directory, {}, {}, False, message))
    return jobs, errors


def validate_entity_metadata(mqtt: Any, job_id: str) -> None:
    if not isinstance(mqtt, Mapping):
        raise MappingError(f"job {job_id} mqtt must be a mapping")
    entities = mqtt.get("entities", {})
    if not isinstance(entities, Mapping):
        raise MappingError(f"job {job_id} mqtt.entities must be a mapping")
    allowed = {"name", "component", "payload_on", "payload_off", "unit_of_measurement",
               "unit_of_measurement_field", "device_class", "state_class", "icon", "expire_after"}
    reserved = {"status", "last_run", "duration_ms", "last_success", "freshness"}
    seen: dict[str, str] = {}
    for field, metadata in entities.items():
        prefix = f"job {job_id} mqtt.entities.{field}"
        if not validate_output_field(field) or not isinstance(metadata, Mapping):
            raise MappingError(f"{prefix} field name must start with a letter or underscore and contain only letters, digits, underscores, or hyphens")
        unknown = set(metadata) - allowed
        if unknown:
            raise MappingError(f"{prefix}: unsupported option {sorted(unknown)[0]}")
        slug = re.sub(r"[^A-Za-z0-9_]", "_", field)
        if slug in reserved:
            raise MappingError(f"{prefix} conflicts with a built-in entity identifier")
        if slug in seen:
            raise MappingError(f"{prefix} duplicates entity identifier for {seen[slug]}")
        seen[slug] = field
        for key in ("name", "unit_of_measurement", "unit_of_measurement_field", "device_class", "state_class", "icon"):
            if key in metadata and (not isinstance(metadata[key], str) or not metadata[key]):
                raise MappingError(f"{prefix}.{key} must be a non-empty string")
        if metadata.get("component", "sensor") not in {"sensor", "binary_sensor"}:
            raise MappingError(f"{prefix}.component must be sensor or binary_sensor")
        for key in ("payload_on", "payload_off"):
            if key in metadata and not isinstance(metadata[key], str):
                raise MappingError(f"{prefix}.{key} must be a string")
        if ("payload_on" in metadata or "payload_off" in metadata) and metadata.get("component") != "binary_sensor":
            raise MappingError(f"{prefix} payload_on/off require component: binary_sensor")
        if "unit_of_measurement" in metadata and "unit_of_measurement_field" in metadata:
            raise MappingError(f"{prefix} cannot set both unit_of_measurement and unit_of_measurement_field")
        if "expire_after" in metadata and (isinstance(metadata["expire_after"], bool)
                                             or not isinstance(metadata["expire_after"], int)
                                             or metadata["expire_after"] < 1):
            raise MappingError(f"{prefix}.expire_after must be a positive integer")


def validate_discovery_identifiers(task_configs: Mapping[str, Mapping[str, Any]], mqtt: Mapping[str, Any], job_id: str) -> None:
    """Reject collisions among known extracted and explicitly named HA entities."""
    fields_by_task: dict[str, list[str]] = {}
    field_counts: dict[str, int] = {}
    for task_id, config in task_configs.items():
        fields = sorted(_configured_fields(config) or ()) if isinstance(config, Mapping) else []
        fields_by_task[task_id] = fields
        for field in fields:
            field_counts[field] = field_counts.get(field, 0) + 1
    names = [f"{task_id}.{field}" for task_id, fields in fields_by_task.items() for field in fields]
    names.extend(field for field, count in field_counts.items() if count == 1)
    names.extend(mqtt.get("entities", {}).keys())
    seen: dict[str, str] = {}
    for name in names:
        if not isinstance(name, str):
            continue
        if "." not in name and not validate_output_field(name):
            raise MappingError(f"job {job_id}: output field {name!r} has an invalid MQTT entity name")
        slug = re.sub(r"[^A-Za-z0-9_]", "_", name)
        if slug in _RESERVED_ENTITY_SLUGS:
            raise MappingError(f"job {job_id}: output {name!r} conflicts with a built-in MQTT entity")
        previous = seen.get(slug)
        if previous is not None and previous != name:
            raise MappingError(f"job {job_id}: MQTT entity identifiers collide for {previous!r} and {name!r}")
        seen[slug] = name


_RESERVED_ENTITY_SLUGS = {"status", "last_run", "duration_ms", "last_success", "freshness"}
