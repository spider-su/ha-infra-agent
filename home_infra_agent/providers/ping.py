"""Bounded ICMP reachability provider."""
from __future__ import annotations

import concurrent.futures
import math
import subprocess
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
