"""Calculate configured manual-meter projections and expose MQTT-safe fields."""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..errors import ConfigError
from .base import TaskProvider
from .meter_billing import FIELDS as BILLING_FIELDS, calculate_billing, validate_config as validate_billing_config

FIELDS = {
    "cumulative_m3", "estimated_cumulative_m3", "estimated_since_reading_m3",
    "last_reading_m3", "last_reading_at", "active_mode", "step_m3_per_day",
    "calculated_at",
    *BILLING_FIELDS,
}
MODES = {"auto", "summer", "winter"}


def _number(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{field} must be a finite number") from None
    if not math.isfinite(number) or number < 0 or (positive and number <= 0):
        prefix = "positive " if positive else ""
        raise ConfigError(f"{field} must be a finite {prefix}number")
    return number


def _month_day(value: Any, field: str) -> tuple[int, int]:
    if not isinstance(value, str):
        raise ConfigError(f"{field} must use MM-DD")
    try:
        parsed = date.fromisoformat(f"2000-{value}")
    except ValueError:
        raise ConfigError(f"{field} must use a valid MM-DD") from None
    return parsed.month, parsed.day


def _parse_reading_at(value: Any, timezone_name: str) -> datetime:
    if not isinstance(value, str):
        raise ConfigError("reading.at must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ConfigError("reading.at must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None:
        raise ConfigError("reading.at must include a timezone offset")
    return parsed.astimezone(ZoneInfo(timezone_name))


def _season_for_day(day: date, seasons: Mapping[str, Mapping[str, Any]]) -> str:
    month_day = (day.month, day.day)
    matches = []
    for mode, season in seasons.items():
        start = _month_day(season["start"], f"seasons.{mode}.start")
        end = _month_day(season["end"], f"seasons.{mode}.end")
        if start <= end:
            in_range = start <= month_day <= end
        else:
            in_range = month_day >= start or month_day <= end
        if in_range:
            matches.append(mode)
    if len(matches) != 1:
        raise ConfigError(f"season configuration does not select exactly one mode for {day.isoformat()}")
    return matches[0]


def validate_config(config: Mapping[str, Any], task_id: str = "meter") -> None:
    allowed = {"type", "timezone", "mode", "reading", "seasons", "reading_history", "billing"}
    unknown = set(config) - allowed
    if unknown:
        raise ConfigError(f"task {task_id}: unsupported calculated meter option {sorted(unknown)[0]}")
    if config.get("type") != "calculated_meter":
        raise ConfigError(f"task {task_id}: type must be calculated_meter")
    timezone_name = config.get("timezone")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ConfigError(f"task {task_id}: timezone is required")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        raise ConfigError(f"task {task_id}: timezone is invalid") from None
    mode = config.get("mode", "auto")
    if mode not in MODES:
        raise ConfigError(f"task {task_id}: mode must be auto, summer or winter")
    reading = config.get("reading")
    if not isinstance(reading, Mapping) or set(reading) != {"value_m3", "at"}:
        raise ConfigError(f"task {task_id}: reading requires value_m3 and at")
    _number(reading["value_m3"], f"task {task_id} reading.value_m3")
    _parse_reading_at(reading["at"], timezone_name)
    seasons = config.get("seasons")
    if not isinstance(seasons, Mapping) or set(seasons) != {"summer", "winter"}:
        raise ConfigError(f"task {task_id}: seasons must define summer and winter")
    for name in ("summer", "winter"):
        season = seasons[name]
        if not isinstance(season, Mapping) or set(season) != {"start", "end", "step_m3_per_day"}:
            raise ConfigError(f"task {task_id}: seasons.{name} requires start, end and step_m3_per_day")
        _month_day(season["start"], f"task {task_id} seasons.{name}.start")
        _month_day(season["end"], f"task {task_id} seasons.{name}.end")
        _number(season["step_m3_per_day"], f"task {task_id} seasons.{name}.step_m3_per_day", positive=True)
    probe = date(2000, 1, 1)
    for offset in range(366):
        _season_for_day(probe + timedelta(days=offset), seasons)
    validate_billing_config(config, timezone_name, float(reading["value_m3"]), _parse_reading_at(reading["at"], timezone_name), task_id)


def _next_boundary(local_now: datetime, mode: str, seasons: Mapping[str, Mapping[str, Any]]) -> datetime:
    end_month, end_day = _month_day(seasons[mode]["end"], f"seasons.{mode}.end")
    boundary = date(local_now.year, end_month, end_day) + timedelta(days=1)
    if boundary <= local_now.date():
        boundary = date(local_now.year + 1, end_month, end_day) + timedelta(days=1)
    return datetime.combine(boundary, time.min, tzinfo=local_now.tzinfo)


def calculate(config: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    validate_config(config)
    zone = ZoneInfo(str(config["timezone"]))
    reading_m3 = _number(config["reading"]["value_m3"], "reading.value_m3")
    reading_at = _parse_reading_at(config["reading"]["at"], str(config["timezone"]))
    current = now.astimezone(zone)
    if current < reading_at:
        raise ConfigError("current time is before reading.at")
    seasons = config["seasons"]
    cursor = reading_at.astimezone(timezone.utc)
    end = current.astimezone(timezone.utc)
    estimated = reading_m3
    while cursor < end:
        local_cursor = cursor.astimezone(zone)
        mode = str(config.get("mode", "auto"))
        if mode == "auto":
            mode = _season_for_day(local_cursor.date(), seasons)
            next_boundary = _next_boundary(local_cursor, mode, seasons).astimezone(timezone.utc)
        else:
            next_boundary = end
        segment_end = min(end, next_boundary)
        seconds = (segment_end - cursor).total_seconds()
        estimated += float(seasons[mode]["step_m3_per_day"]) * seconds / 86400
        cursor = segment_end
    active_mode = str(config.get("mode", "auto"))
    if active_mode == "auto":
        active_mode = _season_for_day(current.date(), seasons)
    step = float(seasons[active_mode]["step_m3_per_day"])
    values = {
        "cumulative_m3": round(reading_m3, 4),
        "estimated_cumulative_m3": round(estimated, 4),
        "estimated_since_reading_m3": round(estimated - reading_m3, 4),
        "last_reading_m3": round(reading_m3, 4),
        "last_reading_at": reading_at.isoformat(timespec="seconds"),
        "active_mode": active_mode,
        "step_m3_per_day": step,
        "calculated_at": current.isoformat(timespec="seconds"),
    }
    values.update(calculate_billing(config, reading_m3, reading_at))
    return values


class CalculatedMeterProvider(TaskProvider):
    def __init__(self, now=lambda: datetime.now(timezone.utc)):
        self.now = now

    def execute(self, task_id: str, config: Mapping[str, Any], timeout: float) -> tuple[str, dict[str, Any]]:
        return "OK", calculate(config, self.now())
