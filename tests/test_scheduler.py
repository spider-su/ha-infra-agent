from datetime import datetime, timezone
import threading
import time
from pathlib import Path

from home_infra_agent.core import Job, JobEngine, TaskProvider, next_cron_run


def test_cron_skips_nonexistent_spring_dst_minute_and_handles_repeated_fall_minute():
    spring = next_cron_run("30 2 * * *", "Europe/Warsaw", datetime(2026, 3, 29, 0, 0, tzinfo=timezone.utc))
    assert spring.isoformat() == "2026-03-30T02:30:00+02:00"

    fall = next_cron_run("30 2 * * *", "Europe/Warsaw", datetime(2026, 10, 25, 0, 45, tzinfo=timezone.utc))
    assert fall.astimezone(timezone.utc).isoformat() == "2026-10-25T01:30:00+00:00"
    assert fall.isoformat() == "2026-10-25T02:30:00+01:00"
    assert fall.fold == 1


def test_cron_fields_are_parsed_once_and_next_run_is_strictly_after_cursor(monkeypatch):
    import home_infra_agent.core as core

    calls = 0
    original = core._cron_field
    def counted(*args):
        nonlocal calls
        calls += 1
        return original(*args)

    monkeypatch.setattr(core, "_cron_field", counted)
    after = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    result = next_cron_run("0 0 1 1 *", "UTC", after)
    assert result.isoformat() == "2027-01-01T00:00:00+00:00"
    assert calls == 5


def test_shutdown_waits_for_running_job_and_manual_run_is_rejected(monkeypatch):
    import home_infra_agent.core as core

    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    stop_results = []

    class Slow(TaskProvider):
        def execute(self, task_id, config, timeout):
            started.set()
            assert release.wait(5)
            return "OK", {"finished": True}

    monkeypatch.setitem(core.PROVIDERS, "shutdown-test", Slow())
    job = Job("slow", "Slow", Path("."), {"schedule": {"interval": "60s"}},
              {"probe": {"type": "shutdown-test"}})
    published = []
    engine = JobEngine([job], lambda j, r: published.append((j.id, r.status)))
    engine.start()
    assert started.wait(2)

    try:
        try:
            engine.run_job("slow")
        except core.JobBusyError:
            pass
        else:
            raise AssertionError("manual execution should be rejected while scheduled execution is active")

        stopper = threading.Thread(target=lambda: (stop_results.append(engine.stop(timeout=.1)), stopped.set()), daemon=True)
        stopper.start()
        assert stopped.wait(.5), "shutdown did not respect its configured deadline"
        assert stop_results == [False], "shutdown must report an active worker"
    finally:
        release.set()

    assert engine.stop(timeout=2), "shutdown did not finish after the active provider returned"
    stopper.join(1)
    assert all(not thread.is_alive() for thread in engine.threads)
    assert published == [("slow", "OK")], "rejected manual run must not publish a duplicate result"
