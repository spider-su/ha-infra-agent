import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from home_infra_agent.mqtt import discovery_configs, state_payload
from home_infra_agent.solarman import (
    FIELDS,
    SolarmanOpenApiClient,
    SolarmanProvider,
    parse_collection_time,
    parse_data_list,
    select_device,
)
from home_infra_agent.core import Job, TaskProvider, validate_task


def test_auth_request_matches_ha_integration_semantics_without_live_credentials():
    calls = []

    def transport(url, body, headers, timeout):
        calls.append((url, json.loads(body), headers, timeout))
        return 200, {}, b'{"success":true,"access_token":"fake-token"}'

    client = SolarmanOpenApiClient("app-id", "app-secret", "user@example.invalid", "test-password", 4, transport)
    assert client.authenticate() == "fake-token"
    url, payload, headers, timeout = calls[0]
    assert url.endswith("/account/v1.0/token?appId=app-id&language=en")
    assert payload == {
        "appSecret": "app-secret",
        "email": "user@example.invalid",
        "password": hashlib.sha256(b"test-password").hexdigest(),
    }
    assert "Authorization" not in headers and timeout == 4


def test_auth_429_gets_only_one_bounded_retry():
    responses = [(429, {"Retry-After": "90"}, b""),
                 (200, {}, b'{"success":true,"access_token":"fake-token"}'),
                 (429, {}, b""), (429, {}, b"")]
    calls, sleeps = [], []
    def transport(url, body, headers, timeout):
        calls.append(url)
        return responses.pop(0)
    client = SolarmanOpenApiClient("id", "secret", "email", "password", 3, transport, sleeps.append)
    assert client.authenticate() == "fake-token"
    with pytest.raises(Exception, match="HTTP 429"):
        client.authenticate()
    assert len(calls) == 4 and sleeps == [2.0, 0.0]


def test_select_device_requires_unique_selection_unless_serial_configured():
    stations = [{"id": 1}, {"id": 2}]
    devices = {1: [{"deviceSn": "serial-a"}], 2: [{"deviceSn": "serial-b"}]}
    with pytest.raises(Exception, match="ambiguous"):
        select_device(stations, devices)
    assert select_device(stations, devices, "serial-b") == (2, "serial-b")
    with pytest.raises(Exception, match="not uniquely found"):
        select_device(stations, devices, "missing")


def test_parse_only_allowlisted_fields_and_reject_malformed_values():
    response = {"dataList": [{"key": "Et_ge0", "value": "12.5"},
                             {"key": "secretTelemetry", "value": "discard"},
                             {"key": "INV_ST1", "value": "Normal"}]}
    assert parse_data_list(response) == {"solar_production_total": 12.5, "inverter_status": "Normal"}
    assert "solar_production_today" not in parse_data_list({"dataList": [{"key": "Et_ge0", "value": 1}]})
    with pytest.raises(Exception, match="missing or malformed"):
        parse_data_list({"dataList": {}})
    with pytest.raises(Exception, match="malformed|valid range"):
        parse_data_list({"dataList": [{"key": "Et_ge0", "value": "NaN"}]})
    with pytest.raises(Exception, match="duplicate"):
        parse_data_list({"dataList": [{"key": "Et_ge0", "value": 1}, {"key": "Et_ge0", "value": 2}]})


def test_collection_time_supports_epoch_milliseconds_and_iso():
    expected = datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc)
    assert parse_collection_time(1791453600) == expected
    assert parse_collection_time(1791453600000) == expected
    assert parse_collection_time("2026-10-08T10:00:00Z") == expected
    with pytest.raises(Exception, match="collectionTime"):
        parse_collection_time(None)


def test_401_reauth_and_429_retry_are_each_bounded():
    statuses = [200, 401, 200, 200]
    bodies = [b'{"success":true,"access_token":"one"}', b"", b'{"success":true,"access_token":"two"}',
              b'{"success":true,"collectionTime":1791453600,"dataList":[]}']
    calls, sleeps = [], []

    def transport(url, body, headers, timeout):
        calls.append((url, headers.get("Authorization")))
        status = statuses.pop(0)
        return status, {}, bodies.pop(0)

    client = SolarmanOpenApiClient("id", "secret", "email", "password", 2, transport, sleeps.append)
    client.current_data("serial")
    assert len(calls) == 4
    assert calls[1][1] == "Bearer one" and calls[3][1] == "Bearer two"
    assert sleeps == []

    responses = [(200, {}, b'{"success":true,"access_token":"token"}'),
                 (429, {"Retry-After": "99"}, b""),
                 (200, {}, b'{"success":true,"stationList":[]}'),
                 (429, {"Retry-After": "99"}, b""),
                 (429, {"Retry-After": "99"}, b"")]
    def limited_transport(*_):
        return responses.pop(0)
    sleeps = []
    client = SolarmanOpenApiClient("id", "secret", "email", "password", 2, limited_transport, sleeps.append)
    assert client.list_stations() == []
    with pytest.raises(Exception, match="HTTP 429"):
        client.list_stations()
    assert sleeps == [2.0, 2.0]

    responses = [(200, {}, b'{"success":true,"access_token":"one"}'), (401, {}, b""),
                 (200, {}, b'{"success":true,"access_token":"two"}'), (401, {}, b"")]
    calls = []
    def unauthorized_transport(url, body, headers, timeout):
        calls.append(url)
        return responses.pop(0)
    client = SolarmanOpenApiClient("id", "secret", "email", "password", 2, unauthorized_transport)
    with pytest.raises(Exception, match="HTTP 401"):
        client.current_data("serial")
    assert len(calls) == 4


def _current_data(now: datetime):
    return {"collectionTime": int(now.timestamp()), "dataList": [
        {"key": source, "value": "Normal" if kind == "text" else "1.5"}
        for source, kind in FIELDS.values()
    ]}


def test_provider_caches_selection_polls_once_and_marks_stale_without_metrics(monkeypatch):
    now = datetime(2026, 10, 8, 10, tzinfo=timezone.utc)
    class FakeApi:
        def __init__(self):
            self.discovery_calls = 0
            self.data_calls = 0
            self.collection_time = now
        def list_stations(self):
            self.discovery_calls += 1
            return [{"id": 3}]
        def list_devices(self, station_id):
            self.discovery_calls += 1
            return [{"deviceSn": "fake-serial"}]
        def current_data(self, device_sn):
            self.data_calls += 1
            return _current_data(self.collection_time)

    api = FakeApi()
    monkeypatch.setenv("SOLARMAN_APP_ID", "fake")
    monkeypatch.setenv("SOLARMAN_APP_SECRET", "fake")
    monkeypatch.setenv("SOLARMAN_EMAIL", "fake")
    monkeypatch.setenv("SOLARMAN_PASSWORD", "fake")
    provider = SolarmanProvider(api_factory=lambda *args: api, now=lambda: now)
    config = {"appIdEnv": "SOLARMAN_APP_ID", "appSecretEnv": "SOLARMAN_APP_SECRET",
              "emailEnv": "SOLARMAN_EMAIL", "passwordEnv": "SOLARMAN_PASSWORD", "maxDataAgeSeconds": 900}
    status, values = provider.execute("current", config, 4)
    assert status == "OK" and values["source_status"] == "FRESH"
    assert values["solar_production_total"] == 1.5 and values["collection_time"].endswith("+00:00")
    provider.execute("current", config, 4)
    assert api.discovery_calls == 2 and api.data_calls == 2
    api.collection_time = now.replace(hour=9)
    status, values = provider.execute("current", config, 4)
    assert status == "WARN" and values["source_status"] == "STALE"
    assert "solar_production_total" not in values


def test_provider_failure_is_visible_and_does_not_publish_readings(monkeypatch):
    class FailedApi:
        def list_stations(self): return [{"id": 1}]
        def list_devices(self, station_id): return [{"deviceSn": "private-serial"}]
        def current_data(self, device_sn): raise RuntimeError("do not expose this error")
    for name in ("SOLARMAN_APP_ID", "SOLARMAN_APP_SECRET", "SOLARMAN_EMAIL", "SOLARMAN_PASSWORD"):
        monkeypatch.setenv(name, "test")
    provider = SolarmanProvider(api_factory=lambda *args: FailedApi())
    config = {"appIdEnv": "SOLARMAN_APP_ID", "appSecretEnv": "SOLARMAN_APP_SECRET",
              "emailEnv": "SOLARMAN_EMAIL", "passwordEnv": "SOLARMAN_PASSWORD"}
    status, values = provider.execute("current", config, 4)
    assert status == "ERROR" and values == {"source_status": "ERROR"}


def test_mqtt_metadata_state_and_stable_ids_for_solarman_config():
    root = Path(__file__).parents[1]
    job_data = yaml.safe_load((root / "config/jobs/solarman/job.yaml").read_text())
    task_data = yaml.safe_load((root / "config/jobs/solarman/current.yaml").read_text())
    validate_task("current", task_data)
    from home_infra_agent.core import PROVIDERS
    assert isinstance(PROVIDERS["solarman"], TaskProvider)
    assert job_data["schedule"]["interval"] == "3600s"
    job = Job("solarman", job_data["name"], Path("."), job_data, {})
    values = {"solar_production_total": 1.25, "source_status": "FRESH"}
    result = type("Result", (), {"values": values, "to_dict": lambda self: {"values": values}})()
    configs = discovery_configs(job, result)
    config = next(json.loads(payload) for topic, payload in configs if topic.endswith("home_infra_agent_solarman_solar_production_total/config"))
    assert config["unique_id"] == "home_infra_agent_solarman_solar_production_total"
    assert config["name"] == "Solar production total"
    assert (config["device_class"], config["state_class"], config["unit_of_measurement"]) == ("energy", "total_increasing", "kWh")
    assert config["expire_after"] == 7200
    assert json.loads(state_payload("solarman", result))["values"] == values


def test_existing_investory_and_proxmox_discovery_contracts_remain():
    def config_for(job_id, job_config, values, suffix):
        job = Job(job_id, job_id, Path("."), {"mqtt": job_config}, {})
        result = type("Result", (), {"values": values})()
        return next(json.loads(body) for topic, body in discovery_configs(job, result)
                    if topic.endswith(suffix))

    investment = config_for("investory", {"topic": "home/investory"},
                            {"equity": 1000, "baseCurrency": "PLN"},
                            "home_infra_agent_investory_equity/config")
    assert investment["device_class"] == "monetary" and investment["unit_of_measurement"] == "PLN"
    proxmox = config_for("proxmox", {"topic": "home/proxmox"}, {"home-lab-0": "UP"},
                         "home_infra_agent_proxmox_home_lab_0/config")
    assert proxmox["payload_on"] == "UP" and proxmox["payload_off"] == "DOWN"
def test_all_allowlisted_keys_are_represented_once():
    assert len(FIELDS) == 16
    assert len({source for source, _ in FIELDS.values()}) == 16
