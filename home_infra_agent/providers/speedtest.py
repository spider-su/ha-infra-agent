"""Occasional Internet speed measurements via the speedtest-cli command."""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, Mapping

from .base import TaskProvider


class SpeedtestProvider(TaskProvider):
    """Run one bounded Speedtest.net measurement and normalize its result."""

    def execute(self, task_id: str, config: Mapping[str, Any], timeout: float) -> tuple[str, dict[str, Any]]:
        command = shutil.which("speedtest-cli") or shutil.which("speedtest")
        if not command:
            raise RuntimeError("speedtest-cli is not installed")
        args = [command, "--secure", "--json"]
        if config.get("serverId") is not None:
            args.extend(["--server", str(config["serverId"])])
        completed = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=timeout, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("speedtest-cli failed")
        try:
            result = json.loads(completed.stdout)
            server = result["server"]
            values = {
                "ping_ms": float(result["ping"]),
                "download_mbps": round(float(result["download"]) / 1_000_000, 2),
                "upload_mbps": round(float(result["upload"]) / 1_000_000, 2),
                "server_name": str(server["name"]),
                "server_country": str(server["country"]),
                "server_id": str(server["id"]),
                "tested_at": str(result.get("timestamp", "")),
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("speedtest-cli returned invalid JSON") from exc
        return "OK", values
