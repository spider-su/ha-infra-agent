"""Bounded ICMP reachability provider."""
from __future__ import annotations

import concurrent.futures
import subprocess
from typing import Any

from .base import TaskProvider


class PingProvider(TaskProvider):
    def execute(self, task_id: str, config: Mapping[str, Any], timeout: float) -> tuple[str, dict[str, Any]]:
        targets = config["targets"]
        values: dict[str, Any] = {}
        def ping(name_host: tuple[Any, Any]) -> tuple[str, str]:
            name, host = name_host
            if not isinstance(host, str) or not host.strip():
                return str(name), "DOWN"
            try:
                completed = subprocess.run(["ping", "-c", "1", "-W", str(max(1, int(timeout))), host],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           timeout=timeout + 1, check=False)
                return str(name), "UP" if completed.returncode == 0 else "DOWN"
            except (OSError, subprocess.TimeoutExpired):
                return str(name), "DOWN"
        items = list(targets.items())
        for offset in range(0, len(items), 8):
            batch = items[offset:offset + 8]
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(batch), thread_name_prefix="ping") as pool:
                futures = [pool.submit(ping, item) for item in batch]
                try:
                    for future in concurrent.futures.as_completed(futures, timeout=timeout + 1.2):
                        name, status = future.result()
                        values[name] = status
                except concurrent.futures.TimeoutError:
                    for future in futures:
                        future.cancel()
        for name, _host in items:
            values.setdefault(str(name), "DOWN")
        online = sum(value == "UP" for value in values.values())
        values.update(online=online, total=len(targets))
        return ("OK" if online == len(targets) else "WARN" if online else "ERROR"), values
