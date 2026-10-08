"""Restricted read-only Investory portfolio query provider."""
from __future__ import annotations

import os
from typing import Any, Mapping

from ..errors import ConfigError
from .base import TaskProvider


class InvestoryPostgresProvider(TaskProvider):
    """Read the latest Investory performance snapshot in a read-only transaction."""

    def execute(self, task_id: str, config: Mapping[str, Any], timeout: float) -> tuple[str, dict[str, Any]]:
        env_name = config["databaseUrlEnv"]
        conninfo = os.environ.get(env_name)
        if not conninfo:
            raise ConfigError(f"database connection environment variable {env_name} is unavailable")
        portfolio_id = 1  # config validation restricts this source to the single supported portfolio

        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL support is not installed") from exc

        statement_timeout_ms = max(100, int(timeout * 1000))
        with psycopg.connect(
            conninfo,
            connect_timeout=max(1, int(timeout)),
            options=f"-c statement_timeout={statement_timeout_ms}",
            row_factory=dict_row,
        ) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            row = connection.execute(
                """SELECT snapshot_date, equity, total_profit, base_currency
                     FROM investory.ha_investory_portfolio_latest()"""
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Investory performance snapshot for portfolio {portfolio_id} is unavailable")

        values: dict[str, Any] = {
            "portfolioId": portfolio_id,
            "snapshotDate": row["snapshot_date"].isoformat() if row["snapshot_date"] else None,
            "baseCurrency": row["base_currency"],
            "equity": float(row["equity"]) if row["equity"] is not None else None,
            "totalProfit": float(row["total_profit"]) if row["total_profit"] is not None else None,
        }
        return "OK", values
