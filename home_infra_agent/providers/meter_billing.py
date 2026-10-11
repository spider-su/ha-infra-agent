"""Optional tariff and payable-delta calculations for configured meters."""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from ..errors import ConfigError

FIELDS = {
    "real_consumption_m3", "billing_currency", "unit_price_pln_per_m3",
    "variable_charge_pln", "fixed_charge_pln", "total_charge_pln",
    "charge_status",
}


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{field} must be a finite number") from None
    if not math.isfinite(number) or number < 0:
        raise ConfigError(f"{field} must be a finite nonnegative number")
    return number


def _timestamp(value: Any, field: str, timezone_name: str) -> datetime:
    if not isinstance(value, str):
        raise ConfigError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ConfigError(f"{field} must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None:
        raise ConfigError(f"{field} must include a timezone offset")
    return parsed.astimezone(ZoneInfo(timezone_name))


def validate_config(config: Mapping[str, Any], timezone_name: str, current_value: float, current_at: datetime,
                   task_id: str = "meter") -> None:
    history = config.get("reading_history", [])
    if not isinstance(history, list):
        raise ConfigError(f"task {task_id}: reading_history must be a list")
    previous_at = None
    previous_value = None
    for index, entry in enumerate(history):
        field = f"task {task_id} reading_history[{index}]"
        if not isinstance(entry, Mapping) or set(entry) != {"value_m3", "at"}:
            raise ConfigError(f"{field} requires value_m3 and at")
        value = _number(entry["value_m3"], f"{field}.value_m3")
        at = _timestamp(entry["at"], f"{field}.at", timezone_name)
        if previous_at is not None and at <= previous_at:
            raise ConfigError(f"{field}.at must be later than the previous reading")
        if previous_value is not None and value < previous_value:
            raise ConfigError(f"{field}.value_m3 must not decrease in reading_history")
        previous_at, previous_value = at, value
    if history and current_at <= previous_at:
        raise ConfigError(f"task {task_id}: reading.at must be later than the last reading_history.at")
    billing = config.get("billing", {})
    if billing is None:
        billing = {}
    if not isinstance(billing, Mapping):
        raise ConfigError(f"task {task_id}: billing must be a mapping")
    allowed = {"currency", "prices_include_tax", "price_periods"}
    unknown = set(billing) - allowed
    if unknown:
        raise ConfigError(f"task {task_id}: unsupported billing option {sorted(unknown)[0]}")
    currency = billing.get("currency", "PLN")
    if not isinstance(currency, str) or not currency.strip():
        raise ConfigError(f"task {task_id}: billing.currency must be nonempty")
    if not isinstance(billing.get("prices_include_tax", True), bool):
        raise ConfigError(f"task {task_id}: billing.prices_include_tax must be boolean")
    periods = billing.get("price_periods", [])
    if not isinstance(periods, list):
        raise ConfigError(f"task {task_id}: billing.price_periods must be a list")
    previous_from = None
    for index, period in enumerate(periods):
        field = f"task {task_id} billing.price_periods[{index}]"
        if not isinstance(period, Mapping):
            raise ConfigError(f"{field} must be a mapping")
        allowed_period = {"from", "variable_pln_per_m3", "fixed_pln_per_interval"}
        if set(period) - allowed_period or "from" not in period:
            raise ConfigError(f"{field} requires from and optional price fields")
        try:
            effective_from = date.fromisoformat(str(period["from"]))
        except ValueError:
            raise ConfigError(f"{field}.from must be YYYY-MM-DD") from None
        if previous_from is not None and effective_from <= previous_from:
            raise ConfigError(f"{field}.from must be later than the previous period")
        if "variable_pln_per_m3" not in period and "fixed_pln_per_interval" not in period:
            raise ConfigError(f"{field} must define a price")
        if "variable_pln_per_m3" in period:
            _number(period["variable_pln_per_m3"], f"{field}.variable_pln_per_m3")
        if "fixed_pln_per_interval" in period:
            _number(period["fixed_pln_per_interval"], f"{field}.fixed_pln_per_interval")
        previous_from = effective_from


def calculate_billing(config: Mapping[str, Any], current_value: float, current_at: datetime) -> dict[str, Any]:
    history = config.get("reading_history", [])
    billing = config.get("billing") or {}
    result = {
        "real_consumption_m3": None,
        "billing_currency": billing.get("currency", "PLN"),
        "unit_price_pln_per_m3": None,
        "variable_charge_pln": None,
        "fixed_charge_pln": None,
        "total_charge_pln": None,
        "charge_status": "no_previous_real_reading",
    }
    if not history:
        return result
    previous = history[-1]
    previous_value = float(previous["value_m3"])
    consumption = current_value - previous_value
    result["real_consumption_m3"] = round(consumption, 4)
    if consumption < 0:
        result["real_consumption_m3"] = None
        result["charge_status"] = "correction_review"
        return result
    periods = billing.get("price_periods", [])
    effective = None
    current_date = current_at.date()
    for period in periods:
        if date.fromisoformat(str(period["from"])) <= current_date:
            effective = period
    if effective is None:
        result["charge_status"] = "prices_not_configured"
        return result
    variable = effective.get("variable_pln_per_m3")
    fixed = effective.get("fixed_pln_per_interval", 0.0)
    if variable is None:
        result["charge_status"] = "prices_not_configured"
        return result
    result["unit_price_pln_per_m3"] = round(float(variable), 6)
    result["variable_charge_pln"] = round(consumption * float(variable), 2)
    result["fixed_charge_pln"] = round(float(fixed), 2)
    result["total_charge_pln"] = round(result["variable_charge_pln"] + result["fixed_charge_pln"], 2)
    result["charge_status"] = "ready"
    return result
