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


def test_real_reading_stays_measured_and_projection_is_separate():
    result = calculate(CONFIG, datetime(2026, 11, 1, 12, tzinfo=ZONE))
    assert result["cumulative_m3"] == 100.0
    assert result["last_reading_m3"] == 100.0
    assert result["active_mode"] == "winter"
    assert result["estimated_since_reading_m3"] == pytest.approx(3.0, abs=0.01)
    assert result["estimated_cumulative_m3"] == pytest.approx(103.0, abs=0.01)


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
    entities = {topic: json.loads(payload) for topic, payload in discovery_configs(gas, result)}
    cumulative = next(payload for topic, payload in entities.items() if "_gas-invoice_cumulative_m3/config" in topic)
    estimate = next(payload for topic, payload in entities.items() if "_gas-invoice_estimated_cumulative_m3/config" in topic)
    assert cumulative["state_class"] == "total_increasing"
    assert cumulative["device_class"] == "gas"
    assert estimate["state_class"] == "measurement"
    assert "device_class" not in estimate
