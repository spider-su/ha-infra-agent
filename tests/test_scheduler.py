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


def test_shutdown_waits_for_running_job_and_manual_run_is_rejected(monkeypatch):
    import home_infra_agent.core as core

    started, release, stopped = threading.Event(), threading.Event(), threading.Event()

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

        stopper = threading.Thread(target=lambda: (engine.stop(), stopped.set()), daemon=True)
        stopper.start()
        time.sleep(2.1)
        assert not stopped.is_set(), "shutdown returned while a provider was still running"
    finally:
        release.set()

    assert stopped.wait(2), "shutdown did not finish after the active provider returned"
    assert all(not thread.is_alive() for thread in engine.threads)
    assert published == [("slow", "OK")], "rejected manual run must not publish a duplicate result"
