"""Provider execution contract."""
from __future__ import annotations

from typing import Any, Mapping


class TaskProvider:
    """Execute a validated task and return status plus normalized source values."""

    def execute(self, task_id: str, config: Mapping[str, Any], timeout: float) -> tuple[str, dict[str, Any]]:
        raise NotImplementedError
