"""Synthetic tests for calendar synchronization; no school data or credentials."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

spec = spec_from_file_location("school_calendar_sync",
    Path(__file__).parents[1] / "scripts" / "school_calendar_sync.py")
sync = module_from_spec(spec)
spec.loader.exec_module(sync)


def test_exam_homework_and_only_changed_lessons():
    exams = [{"Id": 3, "DeadlineAt": "2026-12-01T00:00:00",
              "Subject": {"Name": "Physics"}, "Content": "Unit 5"}]
    homework = [{"Id": 5, "DateAt": "2026-12-02", "Subject": {"Name": "Math"}}]
    lessons = [
        {"Id": 20, "DateAt": "2026-12-03", "TimeSlot": {"Start": "08:00", "End": "08:45"},
         "Subject": {"Name": "English"}},
        {"Id": 21, "DateAt": "2026-12-03", "TimeSlot": {"Start": "09:00", "End": "09:45"},
         "Subject": {"Name": "History"}, "Substitution": {
            "ChangeDateAt": "2026-12-03", "Subject": {"Name": "Geography"},
            "TeacherPrimary": {"Name": "Teacher"}}},
        {"Id": 22, "DateAt": "2026-12-03", "TimeSlot": {"Start": "10:00", "End": "10:45"},
         "Subject": {"Name": "Chemistry"}, "Substitution": {
            "ClassAbsence": True}},
    ]
    events = sync.build_events("123", exams, homework, lessons)
    assert len(events) == 4
    summaries = {e["summary"] for e in events}
    assert "Sprawdzian: Physics" in summaries
    assert "Zadanie domowe: Math" in summaries
    assert "Zastępstwo: Geography" in summaries
    assert "Odwołane: Chemistry" in summaries
    exam = next(e for e in events if e["summary"].startswith("Sprawdzian"))
    assert exam["start"] == {"date": "2026-12-01"}
    assert exam["end"] == {"date": "2026-12-02"}
    assert exam["reminders"]["overrides"][0]["minutes"] == 1440
    assert all(e["id"].startswith("v") for e in events)


def test_stable_ids_and_updates_are_deterministic():
    raw = [{"Id": 42, "DeadlineAt": "2026-12-01", "Subject": {"Name": "Physics"}}]
    original = sync.build_events("123", raw, [], [])[0]
    edited = sync.build_events("123", [{**raw[0], "Content": "Updated"}], [], [])[0]
    assert original["id"] == edited["id"]
    assert original["description"] != edited["description"]
    assert original["id"] != sync.build_events("456", raw, [], [])[0]["id"]


def test_missing_ids_or_dates_cannot_create_bad_events():
    assert sync.build_events("123", [{"DeadlineAt": "2026-12-01"},
                                     {"Id": 1, "DeadlineAt": None}], [], []) == []


def test_dry_run_never_calls_google():
    events = sync.build_events("123", [{"Id": 1, "DeadlineAt": "2026-12-01"}], [], [])
    counts = sync.synchronize(None, "calendar", events, True)
    assert counts == {"prepared": 1, "created": 0, "updated": 0, "unchanged": 0}
