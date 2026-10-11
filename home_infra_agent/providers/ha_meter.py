"""Read real manual HA meter helpers; never synthesize consumption or zeroes."""
from __future__ import annotations

import math
import threading
from datetime import datetime
from typing import Any, Mapping

from ..errors import ConfigError
from .base import TaskProvider
from .http import HttpProvider

FIELDS = {"cumulative_m3", "source_state_changed"}


def http_config(config: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "http", "url": config["baseUrl"].rstrip("/") + "/api/states/" + config["entityId"],
            "method": "GET", "auth": {"type": "bearer", "tokenEnv": config["tokenEnv"]},
            "expectedStatusCodes": [200], "maxResponseBytes": 65536,
            "extract": {"reading": {"path": "$.state", "type": "number", "required": True}}}


def meter_values(payload: Any, entity_id: str) -> dict[str, Any]:
    """Validate without logging source bodies, credentials, or meter readings."""
    if not isinstance(payload, dict) or payload.get("entity_id") != entity_id:
        raise ConfigError("meter response has an unexpected entity")
    attributes = payload.get("attributes")
    if not isinstance(attributes, dict) or attributes.get("unit_of_measurement") not in {"m³", "m3"}:
        raise ConfigError("meter source must report cubic metres")
    value = payload.get("state")
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ConfigError("meter reading must be numeric")
    try:
        reading = float(value)
    except (TypeError, ValueError):
        raise ConfigError("meter reading is unavailable or nonnumeric") from None
    if not math.isfinite(reading) or reading < 0:
        raise ConfigError("meter reading must be finite and nonnegative")
    changed = payload.get("last_changed")
    try:
        timestamp = datetime.fromisoformat(changed.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise ConfigError("meter source state timestamp is invalid") from None
    return {"cumulative_m3": reading, "source_state_changed": timestamp.isoformat()}


class HomeAssistantMeterProvider(TaskProvider):
    def __init__(self):
        self._last: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def execute(self, task_id: str, config: Mapping[str, Any], timeout: float) -> tuple[str, dict[str, Any]]:
        status, payload = HttpProvider().execute(task_id, http_config(config), timeout)
        if status != "OK":
            raise ConfigError("meter source HTTP request failed")
        values = meter_values(payload, config["entityId"])
        key = (config["baseUrl"], config["entityId"])
        with self._lock:
            previous = self._last.get(key)
            if previous is not None and values["cumulative_m3"] < previous:
                raise ConfigError("cumulative meter decreased; review the source reading")
            self._last[key] = values["cumulative_m3"]
        return "OK", values
