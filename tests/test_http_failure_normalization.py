"""HTTP dispatch tasks publish stable boolean reachability on failure."""
from pathlib import Path

from home_infra_agent.core import Job
from home_infra_agent.errors import ConfigError
from home_infra_agent.providers import PROVIDERS


def _job():
    return Job(
        id="school_sync_run",
        name="School Sync Run",
        directory=Path("."),
        config={"schedule": {"interval": "30m"}, "timeout": "2s"},
        task_configs={
            "trigger": {
                "type": "http",
                "method": "POST",
                "url": "https://example.invalid/dispatch",
                "expectedStatusCodes": [204],
            }
        },
    )


def test_http_transport_error_reports_false(monkeypatch):
    class FailingProvider:
        def execute(self, task_id, config, timeout):
            raise ConfigError("task trigger: HTTP connection failed")

    monkeypatch.setitem(PROVIDERS, "http", FailingProvider())
    result = _job().run()
    assert result.status == "ERROR"
    assert result.tasks["trigger"].status == "ERROR"
    assert result.values["reachable"] is False
    assert result.values["trigger.reachable"] is False
    assert result.values["statusCode"] is None


def test_http_204_reports_true(monkeypatch):
    class AcceptedProvider:
        def execute(self, task_id, config, timeout):
            return "OK", {"reachable": True, "statusCode": 204}

    monkeypatch.setitem(PROVIDERS, "http", AcceptedProvider())
    result = _job().run()
    assert result.status == "OK"
    assert result.values["reachable"] is True
    assert result.values["statusCode"] == 204


def test_http_unexpected_status_reports_false(monkeypatch):
    class RejectedProvider:
        def execute(self, task_id, config, timeout):
            return "ERROR", {"reachable": False, "statusCode": 403}

    monkeypatch.setitem(PROVIDERS, "http", RejectedProvider())
    result = _job().run()
    assert result.status == "ERROR"
    assert result.values["reachable"] is False
    assert result.values["statusCode"] == 403
