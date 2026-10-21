from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from home_infra_agent.errors import ConfigError
from home_infra_agent.providers.calculated_meter import calculate, validate_config

ZONE = ZoneInfo("Europe/Warsaw")
CONFIG = {
    "type": "calculated_meter",
    "timezone": "Europe/Warsaw",
    "mode": "auto",
    "reading": {"value_m3": 100.0, "at": "2026-10-31T12:00:00+01:00"},
    "seasons": {
        "summer": {"start": "04-01", "end": "10-31", "step_m3_per_day": 2.0},
        "winter": {"start": "11-01", "end": "03-31", "step_m3_per_day": 4.0},
    },
}


def test_cumulative_meter_advances_from_real_reading():
    result = calculate(CONFIG, datetime(2026, 11, 1, 12, tzinfo=ZONE))
    assert result["cumulative_m3"] == pytest.approx(103.0, abs=0.01)
    assert result["last_reading_m3"] == 100.0
    assert result["active_mode"] == "winter"
    assert result["estimated_since_reading_m3"] == pytest.approx(3.0, abs=0.01)
    assert result["estimated_cumulative_m3"] == pytest.approx(103.0, abs=0.01)


def test_new_real_reading_rebases_projection_immediately():
    config = {**CONFIG, "reading": {
        "value_m3": 140.0,
        "at": "2026-11-30T12:00:00+01:00",
    }}

    at_reading = calculate(config, datetime(2026, 11, 30, 12, tzinfo=ZONE))
    after_reading = calculate(config, datetime(2026, 12, 1, 0, tzinfo=ZONE))

    assert at_reading["cumulative_m3"] == 140.0
    assert after_reading["cumulative_m3"] == pytest.approx(142.0, abs=0.01)
    assert after_reading["last_reading_m3"] == 140.0


def test_fixed_mode_uses_configured_mode_for_entire_projection():
    config = {**CONFIG, "mode": "summer"}
    result = calculate(config, datetime(2026, 11, 1, 12, tzinfo=ZONE))
    assert result["active_mode"] == "summer"
    assert result["step_m3_per_day"] == 2.0
    assert result["estimated_since_reading_m3"] == pytest.approx(2.0, abs=0.01)


@pytest.mark.parametrize("bad", [
    {**CONFIG, "mode": "invalid"},
    {**CONFIG, "reading": {"value_m3": 100.0, "at": "2026-10-31T12:00:00"}},
])
def test_invalid_configuration_is_rejected(bad):
    with pytest.raises(ConfigError):
        validate_config(bad)


def test_season_gap_is_rejected():
    bad = {**CONFIG, "seasons": {
        "summer": {"start": "04-01", "end": "10-31", "step_m3_per_day": 2.0},
        "winter": {"start": "11-02", "end": "03-31", "step_m3_per_day": 4.0},
    }}
    with pytest.raises(ConfigError):
        validate_config(bad)


def test_meter_discovery_separates_real_reading_from_estimates():
    from home_infra_agent.core import JobEngine, discover_jobs
    from home_infra_agent.mqtt import discovery_configs
    root = Path(__file__).parents[1]
    jobs, errors = discover_jobs(root / "config/jobs")
    assert not errors
    gas = next(job for job in jobs if job.id == "gas-invoice")
    result = JobEngine([gas]).run_job(gas.id)
    entities = {topic: json.loads(payload) for topic, payload in discovery_configs(gas, result) if payload}
    cumulative = next(payload for topic, payload in entities.items() if "_gas-invoice_cumulative_m3/config" in topic)
    estimate = next(payload for topic, payload in entities.items() if "_gas-invoice_estimated_cumulative_m3/config" in topic)
    assert cumulative["state_class"] == "total_increasing"
    assert cumulative["device_class"] == "gas"
    assert estimate["state_class"] == "measurement"
    assert "device_class" not in estimate
    assert not any(any(marker in topic for marker in ("_reading_cumulative_m3/", "_reading_estimated_", "_reading_active_mode/", "_reading_calculated_at/", "_reading_last_", "_reading_step_")) for topic in entities)


def test_payable_delta_requires_real_history_and_uses_configured_price():
    from home_infra_agent.providers.calculated_meter import calculate
    config = {**CONFIG,
        "reading": {"value_m3": 105.0, "at": "2026-11-01T12:00:00+01:00"},
        "reading_history": [{"value_m3": 100.0, "at": "2026-10-31T12:00:00+01:00"}],
        "billing": {"currency": "PLN", "prices_include_tax": True,
                    "price_periods": [{"from": "2026-01-01",
                                       "variable_pln_per_m3": 2.0,
                                       "fixed_pln_per_interval": 3.0}]}}
    result = calculate(config, datetime(2026, 11, 2, 12, tzinfo=ZONE))
    assert result["real_consumption_m3"] == 5.0
    assert result["unit_price_pln_per_m3"] == 2.0
    assert result["variable_charge_pln"] == 10.0
    assert result["fixed_charge_pln"] == 3.0
    assert result["total_charge_pln"] == 13.0
    assert result["charge_status"] == "ready"


def test_money_is_unavailable_until_prices_are_provided():
    result = calculate({**CONFIG, "reading_history": [
        {"value_m3": 99.0, "at": "2026-10-30T12:00:00+01:00"}]},
        datetime(2026, 10, 31, 12, tzinfo=ZONE))
    assert result["real_consumption_m3"] == pytest.approx(1.0, abs=0.01)
    assert result["total_charge_pln"] is None
    assert result["charge_status"] == "prices_not_configured"


def test_downward_correction_never_becomes_payable_consumption():
    config = {**CONFIG, "reading": {"value_m3": 99.0, "at": "2026-11-01T12:00:00+01:00"},
              "reading_history": [{"value_m3": 100.0, "at": "2026-10-31T12:00:00+01:00"}]}
    result = calculate(config, datetime(2026, 11, 2, 12, tzinfo=ZONE))
    assert result["real_consumption_m3"] is None
    assert result["total_charge_pln"] is None
    assert result["charge_status"] == "correction_review"
