import json
from pathlib import Path

import pytest

from home_infra_agent.core import ConfigError, JobEngine, discover_jobs, validate_task
from home_infra_agent.mqtt import discovery_configs, state_payload
from home_infra_agent.providers import PROVIDERS
from home_infra_agent.providers.ha_meter import HomeAssistantMeterProvider, meter_values

ENTITY = "input_number.historical_gas_consumption"
CONFIG = {"type": "home_assistant_meter", "baseUrl": "http://127.0.0.1:8123", "entityId": ENTITY, "tokenEnv": "TEST_METER_TOKEN"}


def payload(value="27943.0", entity=ENTITY, unit="m³"):
    return {"entity_id": entity, "state": value, "attributes": {"unit_of_measurement": unit},
            "last_changed": "2026-10-10T21:00:00+00:00"}


def test_valid_meter_keeps_actual_total_and_source_timestamp():
    assert meter_values(payload(), ENTITY) == {"cumulative_m3": 27943.0, "source_state_changed": "2026-10-10T21:00:00+00:00"}


@pytest.mark.parametrize("value", ["unknown", "unavailable", "", "bad", "NaN", "Infinity", -1, True, None, {}])
def test_invalid_meter_is_not_converted_to_zero(value):
    with pytest.raises(ConfigError):
        meter_values(payload(value), ENTITY)


@pytest.mark.parametrize("unit", ["kWh", "L", None])
def test_wrong_unit_rejected(unit):
    with pytest.raises(ConfigError):
        meter_values(payload(unit=unit), ENTITY)


def test_mismatched_entity_and_missing_timestamp_rejected():
    with pytest.raises(ConfigError):
        meter_values(payload(entity="sensor.mqtt_gas"), ENTITY)
    value=payload(); value.pop("last_changed")
    with pytest.raises(ConfigError):
        meter_values(value, ENTITY)


def test_source_config_valid_and_cannot_read_own_mqtt_output():
    validate_task("reading", CONFIG)
    for changes in [{"entityId":"sensor.gas_invoice_mqtt"}, {"baseUrl":"http://user:secret@localhost"},
                    {"baseUrl":"http://localhost?token=secret"}, {"tokenEnv":"not-valid"}]:
        with pytest.raises(ConfigError):
            validate_task("reading", {**CONFIG, **changes})


def test_provider_rejects_decrease_but_allows_unchanged_and_increased(monkeypatch):
    current=payload("10")
    monkeypatch.setattr("home_infra_agent.providers.ha_meter.HttpProvider.execute", lambda *a: ("OK", current))
    provider=HomeAssistantMeterProvider()
    assert provider.execute("reading", CONFIG, 1)[1]["cumulative_m3"] == 10
    assert provider.execute("reading", CONFIG, 1)[1]["cumulative_m3"] == 10
    current["state"]="11"
    assert provider.execute("reading", CONFIG, 1)[1]["cumulative_m3"] == 11
    current["state"]="9"
    with pytest.raises(ConfigError, match="decreased"):
        provider.execute("reading", CONFIG, 1)


def test_missing_token_fails_safely(monkeypatch):
    monkeypatch.delenv("TEST_METER_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="not set"):
        HomeAssistantMeterProvider().execute("reading", CONFIG, 1)
