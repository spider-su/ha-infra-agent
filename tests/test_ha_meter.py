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


def test_independent_jobs_discovery_and_failure_nulls_without_zero(monkeypatch):
    jobs, errors=discover_jobs(Path(__file__).parents[1]/"config"/"jobs")
    assert not errors
    selected=[j for j in jobs if j.id in {"gas-invoice", "water-invoice"}]
    assert len(selected)==2 and all(j.valid for j in selected)
    monkeypatch.setitem(PROVIDERS, "home_assistant_meter", HomeAssistantMeterProvider())
    def fetch(self, task_id, config, timeout):
        entity=config["url"].split("/api/states/")[-1]
        return "OK", payload("27943" if "gas" in entity else "307.2", entity)
    monkeypatch.setattr("home_infra_agent.providers.ha_meter.HttpProvider.execute", fetch)
    engine=JobEngine(selected)
    for job in selected:
        result=engine.run_job(job.id)
        assert result.status=="OK"
        configs=[json.loads(v) for _,v in discovery_configs(job,result)]
        meter=next(c for c in configs if c["unique_id"].endswith("_cumulative_m3") and not c["unique_id"].endswith("reading_cumulative_m3"))
        assert meter["unit_of_measurement"]=="m³" and meter["state_class"]=="total_increasing"
        assert meter["device_class"]==("gas" if job.id=="gas-invoice" else "water")
        assert meter["expire_after"]==900
        assert meter["availability_mode"]=="all" and len(meter["availability"])==2
        assert "availability_topic" not in meter
        assert "value_json.status == 'OK'" in meter["availability"][1]["value_template"]
    def fail(*args):
        raise ConfigError("meter source unavailable")
    monkeypatch.setattr("home_infra_agent.providers.ha_meter.HttpProvider.execute", fail)
    result=engine.run_job("gas-invoice")
    assert result.status=="ERROR" and result.values["cumulative_m3"] is None
    assert json.loads(state_payload("gas-invoice", result, 900))["values"]["cumulative_m3"] is None
