from datetime import date
import sys
from pathlib import Path
from types import ModuleType

from home_infra_agent.core import Job, JobEngine, TaskProvider
from home_infra_agent.providers import PROVIDERS, KubernetesProvider, PresenceProvider, SpeedtestProvider
from home_infra_agent.providers.investory import InvestoryPostgresProvider


def test_static_provider_registry_contains_all_configured_sources():
    assert set(PROVIDERS) == {"ping", "http", "kubernetes", "investory_postgres", "solarman", "speedtest", "presence"}
    assert all(isinstance(provider, TaskProvider) for provider in PROVIDERS.values())


def test_kubernetes_terminal_failed_pods_do_not_degrade_healthy_workloads(tmp_path, monkeypatch):
    provider = KubernetesProvider()
    token = tmp_path / "token"
    ca = tmp_path / "ca.crt"
    token.write_text("test-token")
    ca.write_text("test-ca")
    provider.token_path = token
    provider.ca_path = ca
    monkeypatch.setattr("home_infra_agent.providers.kubernetes.ssl.create_default_context", lambda **_: object())
    responses = {
        "/api/v1/nodes": [{"metadata": {"name": "node-0"}, "status": {"conditions": [
            {"type": "Ready", "status": "True"}]}}],
        "/api/v1/pods": [
            {"metadata": {"name": "old-failed"}, "status": {"phase": "Failed"}},
            {"metadata": {"name": "old-complete"}, "status": {"phase": "Succeeded"}},
        ],
        "/apis/apps/v1/deployments": [],
        "/apis/apps/v1/statefulsets": [],
        "/apis/apps/v1/daemonsets": [],
    }
    monkeypatch.setattr(provider, "_list", lambda path, *_args: responses[path])

    status, values = provider.execute("cluster", {}, 2)
    assert status == "OK"
    assert values["nodesReady"] == values["nodesTotal"] == 1
    assert values["podsFailed"] == 1
    assert values["workloadsStatus"] == "HEALTHY"

    responses["/api/v1/pods"].append({"metadata": {"name": "waiting"}, "status": {"phase": "Pending"}})
    status, values = provider.execute("cluster", {}, 2)
    assert status == "WARN"
    assert values["podsPending"] == 1
    assert values["workloadsStatus"] == "DEGRADED"


def test_investory_query_opens_read_only_and_returns_normalized_snapshot(monkeypatch):
    calls = []
    row = {"snapshot_date": date(2026, 10, 8), "equity": 125.5,
           "total_profit": 5.5, "base_currency": "PLN"}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement):
            calls.append(statement)
            return type("Cursor", (), {"fetchone": lambda self: row})()

    psycopg = ModuleType("psycopg")
    psycopg.connect = lambda *args, **kwargs: (calls.append((args, kwargs)) or Connection())
    rows = ModuleType("psycopg.rows")
    rows.dict_row = object()
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows)
    monkeypatch.setenv("INVESTORY_TEST_URL", "postgresql://test.invalid/read-only")

    status, values = InvestoryPostgresProvider().execute(
        "portfolio", {"databaseUrlEnv": "INVESTORY_TEST_URL", "portfolioId": 1}, 3)

    assert status == "OK"
    assert values == {"portfolioId": 1, "snapshotDate": "2026-10-08", "baseCurrency": "PLN",
                      "equity": 125.5, "totalProfit": 5.5}
    assert calls[1] == "SET TRANSACTION READ ONLY"
    assert "ha_investory_portfolio_latest()" in calls[2]
    assert "FROM investory.ha_investory_portfolio_latest()" in calls[2]


def test_thread_scheduler_runs_and_stops_cleanly(monkeypatch):
    import threading
    import home_infra_agent.core as core

    called = threading.Event()

    class FastProvider(TaskProvider):
        def execute(self, task_id, config, timeout):
            return "OK", {"ready": True}

    monkeypatch.setitem(core.PROVIDERS, "scheduler-test", FastProvider())
    job = Job("scheduled", "Scheduled", Path("."), {"schedule": {"interval": "0.1s"}},
              {"probe": {"type": "scheduler-test"}})
    engine = JobEngine([job], lambda *_: called.set())
    engine.start()
    assert called.wait(2)
    engine.stop()
    assert engine.threads
    assert all(not thread.is_alive() for thread in engine.threads)
    assert job.last_result.status == "OK"


def test_speedtest_normalizes_cli_json(monkeypatch):
    class Completed:
        returncode = 0
        stdout = '{"ping": 12.5, "download": 25000000, "upload": 8000000, "timestamp": "2026-10-09T08:00:00Z", "server": {"id": "123", "name": "Warsaw", "country": "Poland"}}'
        stderr = ""

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/speedtest-cli")
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: Completed())
    status, values = SpeedtestProvider().execute("speed", {}, 120)
    assert status == "OK"
    assert values == {"ping_ms": 12.5, "download_mbps": 25.0, "upload_mbps": 8.0,
                      "server_name": "Warsaw", "server_country": "Poland", "server_id": "123",
                      "tested_at": "2026-10-09T08:00:00Z"}


def test_presence_requires_consecutive_misses_before_away(monkeypatch):
    provider = PresenceProvider()
    observations = iter([
        ("OK", {"alex": "UP", "online": 1, "total": 1}),
        ("ERROR", {"alex": "DOWN", "online": 0, "total": 1}),
        ("ERROR", {"alex": "DOWN", "online": 0, "total": 1}),
    ])
    monkeypatch.setattr(provider._ping, "execute", lambda *_args: next(observations))
    config = {"targets": {"alex": "192.0.2.1"}, "awayAfter": 3}
    assert provider.execute("family", config, 1)[1]["alex"] == "HOME"
    assert provider.execute("family", config, 1)[1]["alex"] == "HOME"
    assert provider.execute("family", config, 1)[1]["alex"] == "HOME"
    observations = iter([("ERROR", {"alex": "DOWN", "online": 0, "total": 1})])
    assert provider.execute("family", config, 1)[1]["alex"] == "AWAY"
