"""Persistent state for the external monitor heartbeat."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import Job, JobResult

HEARTBEAT_MAX_AGE = "5h"


def heartbeat_job() -> Job:
    return Job("external_monitor_heartbeat", "External Monitor", Path("."), {
        "freshness": {"maxAge": HEARTBEAT_MAX_AGE},
        "mqtt": {
            "topic": "home/external_monitor_heartbeat",
            "device": {"name": "External Monitor", "manufacturer": "Home Infra Agent",
                       "model": "External watchdog"},
            "entities": {
                "count": {"name": "Heartbeat count", "unit_of_measurement": "checks",
                          "state_class": "total_increasing", "icon": "mdi:heart-pulse",
                          "expire_after": 18000},
                "last_seen": {"name": "Last heartbeat", "device_class": "timestamp",
                              "expire_after": 18000},
            },
        },
    }, {})


def heartbeat_result(state: dict[str, Any]) -> JobResult:
    timestamp = state["lastSeen"]
    return JobResult("external_monitor_heartbeat", "OK", timestamp, 0,
                     {"count": state["count"], "last_seen": timestamp}, {}, timestamp)


class ExternalMonitorHeartbeat:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def record(self) -> dict[str, Any]:
        with self._lock:
            try:
                current = json.loads(self.path.read_text())
                count = current.get("count", 0) if isinstance(current, dict) else 0
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    count = 0
            except FileNotFoundError:
                count = 0
            except (OSError, json.JSONDecodeError):
                count = 0

            state = {
                "count": count + 1,
                "lastSeen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(json.dumps(state, separators=(",", ":")) + "\n")
            os.replace(temporary, self.path)
            return state
