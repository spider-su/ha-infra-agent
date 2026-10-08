"""Solarman Cloud Open API source for the existing task and MQTT pipeline."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

from ..errors import ConfigError
from .base import TaskProvider

log = logging.getLogger(__name__)
API_ROOT = "https://globalapi.solarmanpv.com"

# Only these values cross the source boundary into normalized agent state.
FIELDS: dict[str, tuple[str, str]] = {
    "solar_production_total": ("Et_ge0", "number"),
    "solar_production_today": ("Etdy_ge1", "number"),
    "pv_power": ("PVTP", "number"),
    "grid_export_total": ("t_gc1", "number"),
    "grid_export_today": ("t_gc_tdy1", "number"),
    "grid_import_total": ("Et_pu1", "number"),
    "grid_import_today": ("Etdy_pu1", "number"),
    "home_consumption_power": ("E_Puse_t1", "number"),
    "home_consumption_total_raw": ("Et_use1", "number"),
    "home_consumption_today": ("Etdy_use1", "number"),
    "battery_state_of_charge": ("B_left_cap1", "number"),
    "battery_charge_total": ("t_cg_n1", "number"),
    "battery_charge_today": ("Etdy_cg1", "number"),
    "battery_discharge_total": ("t_dcg_n1", "number"),
    "battery_discharge_today": ("Etdy_dcg1", "number"),
    "inverter_status": ("INV_ST1", "text"),
}


class SolarmanError(RuntimeError):
    """Safe-to-log error that never includes credentials or response bodies."""


class SolarmanApi(Protocol):
    def list_stations(self) -> list[dict[str, Any]]: ...
    def list_devices(self, station_id: int) -> list[dict[str, Any]]: ...
    def current_data(self, device_sn: str) -> dict[str, Any]: ...


Transport = Callable[[str, bytes, Mapping[str, str], float], tuple[int, Mapping[str, str], bytes]]


def _urlopen_transport(url: str, body: bytes, headers: Mapping[str, str], timeout: float):
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
            return response.status, dict(response.headers), response.read(1_000_001)
    except urllib.error.HTTPError as exc:
        # Never read or surface the response body: it can contain account data.
        return exc.code, dict(exc.headers or {}), b""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SolarmanError(f"Solarman transport failed ({type(exc).__name__})") from None


class SolarmanOpenApiClient:
    """Small synchronous Open API client with bounded auth/rate-limit recovery."""

    def __init__(self, app_id: str, app_secret: str, email: str, password: str,
                 timeout: float, transport: Transport = _urlopen_transport,
                 sleeper: Callable[[float], None] = time.sleep):
        self.app_id, self.app_secret = app_id, app_secret
        self.email, self.password = email, password
        self.timeout, self.transport, self.sleeper = max(0.1, min(float(timeout), 30.0)), transport, sleeper
        self._token: str | None = None

    def authenticate(self) -> str:
        query = urllib.parse.urlencode({"appId": self.app_id, "language": "en"})
        payload = {
            "appSecret": self.app_secret,
            "email": self.email,
            "password": hashlib.sha256(self.password.encode("utf-8")).hexdigest(),
        }
        try:
            response = self._request("/account/v1.0/token", payload, query=query, token=None)
        except _HttpStatus as exc:
            if exc.status != 429:
                raise SolarmanError(f"Solarman authentication failed (HTTP {exc.status})") from None
            self.sleeper(min(2.0, max(0.0, exc.retry_after)))
            try:
                response = self._request("/account/v1.0/token", payload, query=query, token=None)
            except _HttpStatus as retry_exc:
                raise SolarmanError(f"Solarman authentication failed (HTTP {retry_exc.status})") from None
        token = response.get("access_token")
        if response.get("success") is False or not isinstance(token, str) or not token:
            raise SolarmanError("Solarman authentication rejected")
        self._token = token
        return token

    def list_stations(self) -> list[dict[str, Any]]:
        response = self._authorized("/station/v1.0/list", {"page": 1, "size": 20})
        stations = response.get("stationList")
        if not isinstance(stations, list):
            raise SolarmanError("Solarman station response malformed")
        return [item for item in stations if isinstance(item, dict)]

    def list_devices(self, station_id: int) -> list[dict[str, Any]]:
        response = self._authorized("/station/v1.0/device", {"stationId": station_id})
        devices = response.get("deviceListItems")
        if not isinstance(devices, list):
            raise SolarmanError("Solarman device response malformed")
        return [item for item in devices if isinstance(item, dict)]

    def current_data(self, device_sn: str) -> dict[str, Any]:
        response = self._authorized("/device/v1.0/currentData", {"deviceSn": device_sn})
        if not isinstance(response, dict):
            raise SolarmanError("Solarman currentData response malformed")
        return response

    def _authorized(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if self._token is None:
            self.authenticate()
        reauthed = rate_retried = False
        while True:
            try:
                return self._request(path, body, token=self._token)
            except _HttpStatus as exc:
                if exc.status == 401 and not reauthed:
                    reauthed = True
                    self.authenticate()
                    continue
                if exc.status == 429 and not rate_retried:
                    rate_retried = True
                    delay = min(2.0, max(0.0, exc.retry_after))
                    self.sleeper(delay)
                    continue
                raise SolarmanError(f"Solarman API request failed (HTTP {exc.status})") from None

    def _request(self, path: str, body: dict[str, Any], *, query: str = "", token: str | None) -> dict[str, Any]:
        url = API_ROOT + path + ("?" + query if query else "")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        status, response_headers, raw = self.transport(
            url, json.dumps(body, separators=(",", ":")).encode(), headers, self.timeout
        )
        if status == 401 or status == 429:
            try:
                retry_after_header = next((value for key, value in response_headers.items()
                                           if str(key).lower() == "retry-after"), "0")
                retry_after = min(2.0, max(0.0, float(retry_after_header)))
            except (TypeError, ValueError):
                retry_after = 0.0
            raise _HttpStatus(status, retry_after)
        if not 200 <= status < 300:
            raise SolarmanError(f"Solarman API request failed (HTTP {status})")
        if len(raw) > 1_000_000:
            raise SolarmanError("Solarman API response exceeded size limit")
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SolarmanError("Solarman API returned malformed JSON") from None
        if not isinstance(result, dict) or result.get("success") is False:
            raise SolarmanError("Solarman API reported an unsuccessful response")
        return result


class _HttpStatus(Exception):
    def __init__(self, status: int, retry_after: float):
        self.status, self.retry_after = status, retry_after


def select_device(stations: list[dict[str, Any]], devices_by_station: Mapping[int, list[dict[str, Any]]],
                  requested_serial: str | None = None) -> tuple[int, str]:
    candidates: list[tuple[int, str]] = []
    for station in stations:
        station_id = station.get("id", station.get("stationId"))
        if isinstance(station_id, bool) or not isinstance(station_id, int):
            continue
        for device in devices_by_station.get(station_id, []):
            serial = device.get("deviceSn")
            if isinstance(serial, str) and serial.strip():
                candidates.append((station_id, serial.strip()))
    if requested_serial:
        matches = [candidate for candidate in candidates if candidate[1] == requested_serial.strip()]
        if len(matches) == 1:
            return matches[0]
        raise SolarmanError("Configured Solarman inverter was not uniquely found")
    if len(candidates) != 1:
        raise SolarmanError("Solarman inverter selection is ambiguous; configure deviceSerialEnv")
    return candidates[0]


def parse_collection_time(value: Any) -> datetime:
    if isinstance(value, bool) or value is None:
        raise SolarmanError("Solarman collectionTime is missing or malformed")
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().replace(".", "", 1).isdigit()):
            epoch = float(value)
            if epoch > 1e12:
                epoch /= 1000
            return datetime.fromtimestamp(epoch, timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (OverflowError, OSError, TypeError, ValueError):
        raise SolarmanError("Solarman collectionTime is malformed") from None


def parse_data_list(response: Mapping[str, Any]) -> dict[str, Any]:
    data_list = response.get("dataList")
    if not isinstance(data_list, list):
        raise SolarmanError("Solarman currentData dataList is missing or malformed")
    raw_values: dict[str, Any] = {}
    allowlisted = {source for source, _ in FIELDS.values()}
    for item in data_list:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            continue
        key = item["key"]
        if key in allowlisted:
            if key in raw_values:
                raise SolarmanError("Solarman currentData contains duplicate fields")
            raw_values[key] = item.get("value")
    values: dict[str, Any] = {}
    for output, (source, kind) in FIELDS.items():
        if source not in raw_values or raw_values[source] is None:
            continue
        value = raw_values[source]
        if kind == "text":
            if not isinstance(value, (str, int)) or not str(value).strip():
                raise SolarmanError("Solarman inverter status is malformed")
            values[output] = str(value).strip()
            continue
        if isinstance(value, bool):
            raise SolarmanError(f"Solarman field {output} is malformed")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise SolarmanError(f"Solarman field {output} is malformed") from None
        if not math.isfinite(number) or number < 0 or (output == "battery_state_of_charge" and number > 100):
            raise SolarmanError(f"Solarman field {output} is outside its valid range")
        values[output] = number
    return values


class SolarmanProvider(TaskProvider):
    """Fetch one selected inverter's current data and expose allowlisted fields."""

    def __init__(self, api_factory: Callable[..., SolarmanApi] | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.api_factory = api_factory or SolarmanOpenApiClient
        self.now = now
        self._selection: dict[str, tuple[int, str]] = {}
        self._selection_lock = threading.Lock()

    def execute(self, task_id: str, config: Mapping[str, Any], timeout: float) -> tuple[str, dict[str, Any]]:
        def secret(field: str) -> str:
            env_name = str(config[field])
            value = os.environ.get(env_name)
            if not value:
                raise ConfigError(f"task {task_id}: required environment variable {env_name} is not set")
            return value

        serial_env = config.get("deviceSerialEnv")
        requested_serial = os.environ.get(str(serial_env), "").strip() if serial_env else ""
        cache_key = (f"{task_id}:{config.get('appIdEnv')}:{config.get('emailEnv')}:"
                     f"{serial_env or 'auto'}:{requested_serial}")
        try:
            api = self.api_factory(secret("appIdEnv"), secret("appSecretEnv"),
                                   secret("emailEnv"), secret("passwordEnv"), timeout)
            with self._selection_lock:
                selected = self._selection.get(cache_key)
                if selected is None:
                    stations = api.list_stations()
                    devices = {}
                    for station in stations:
                        station_id = station.get("id", station.get("stationId"))
                        if isinstance(station_id, int) and not isinstance(station_id, bool):
                            devices[station_id] = api.list_devices(station_id)
                    selected = select_device(stations, devices, requested_serial or None)
                    self._selection[cache_key] = selected
            response = api.current_data(selected[1])
            source_time = parse_collection_time(response.get("collectionTime"))
            age = max(0.0, (self.now().astimezone(timezone.utc) - source_time).total_seconds())
            age = round(age, 1)
            max_age = int(config.get("maxDataAgeSeconds", 900))
            common = {"source_status": "FRESH", "collection_time": source_time.isoformat(timespec="seconds"),
                      "source_age_seconds": age}
            if age > max_age:
                common["source_status"] = "STALE"
                log.warning("Solarman task %s source data is stale", task_id)
                return "WARN", common
            values = parse_data_list(response)
            missing = set(FIELDS) - set(values)
            if missing:
                common["source_status"] = "PARTIAL"
                log.warning("Solarman task %s response is missing %d allowlisted fields", task_id, len(missing))
            return ("WARN" if missing else "OK"), {**values, **common}
        except ConfigError:
            raise
        except Exception as exc:
            # Do not forward exception text: transport libraries can include
            # request details, response bodies, or device identifiers.
            log.warning("Solarman task %s failed (%s)", task_id, type(exc).__name__)
            return "ERROR", {"source_status": "ERROR"}
