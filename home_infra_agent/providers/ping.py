"""Bounded ICMP reachability provider."""
from __future__ import annotations

import concurrent.futures
import math
import subprocess
import threading
import time
from typing import Any, Mapping

from .base import TaskProvider


class PingProvider(TaskProvider):
    def execute(self, task_id: str, config: Mapping[str, Any], timeout: float) -> tuple[str, dict[str, Any]]:
        targets = config["targets"]
        values: dict[str, Any] = {}
        deadline = time.monotonic() + timeout
        def ping(name_host: tuple[Any, Any], remaining: float) -> tuple[str, str]:
            name, host = name_host
            if not isinstance(host, str) or not host.strip():
                return str(name), "DOWN"
            try:
                completed = subprocess.run(["ping", "-c", "1", "-W", str(max(1, math.ceil(remaining))), host],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           timeout=remaining, check=False)
                return str(name), "UP" if completed.returncode == 0 else "DOWN"
            except (OSError, subprocess.TimeoutExpired):
                return str(name), "DOWN"
        items = list(targets.items())
        for offset in range(0, len(items), 8):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            batch = items[offset:offset + 8]
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(batch), thread_name_prefix="ping") as pool:
                futures = [pool.submit(ping, item, remaining) for item in batch]
                try:
                    for future in concurrent.futures.as_completed(futures, timeout=remaining):
                        name, status = future.result()
                        values[name] = status
                except concurrent.futures.TimeoutError:
                    # Running subprocesses enforce the same remaining deadline themselves.
                    pass
        for name, _host in items:
            values.setdefault(str(name), "DOWN")
        online = sum(value == "UP" for value in values.values())
        values.update(online=online, total=len(targets))
        return ("OK" if online == len(targets) else "WARN" if online else "ERROR"), values


class PresenceProvider(TaskProvider):
    """Turn repeated ICMP observations into stable person presence states."""

    def __init__(self) -> None:
        self._ping = PingProvider()
        self._lock = threading.Lock()
        self._state: dict[tuple[str, str], tuple[str, int]] = {}

    def execute(self, task_id: str, config: Mapping[str, Any], timeout: float) -> tuple[str, dict[str, Any]]:
        probe_status, probe_values = self._ping.execute(task_id, config, timeout)
        away_after = int(config.get("awayAfter", 3))
        values: dict[str, Any] = {}
        home_count = 0
        with self._lock:
            for name in config["targets"]:
                key = (task_id, str(name))
                observed = probe_values.get(str(name), "DOWN")
                previous, misses = self._state.get(key, ("AWAY", 0))
                if observed == "UP":
                    state, misses = "HOME", 0
                else:
                    misses += 1
                    state = "AWAY" if misses >= away_after else previous
                self._state[key] = (state, misses)
                values[str(name)] = state
                if state == "HOME":
                    home_count += 1
            values.update(
                family_home="HOME" if home_count else "AWAY",
                home_count=home_count,
                total=len(config["targets"]),
                probe_online=probe_values.get("online", 0),
            )
        # A completed probe cycle is valid even when everyone is away;
        # network health is reported separately by the network job.
        return "OK" if probe_status in {"OK", "WARN", "ERROR"} else probe_status, values
