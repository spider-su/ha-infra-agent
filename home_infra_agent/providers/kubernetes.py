"""Read-only in-cluster Kubernetes health provider."""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode

from .base import TaskProvider


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
