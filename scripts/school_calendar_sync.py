"""Stateless eduVULCAN -> Google Calendar synchronization.

Only exams, homework, substitutions and cancelled lessons are synchronized.
Normal timetable entries are deliberately excluded. No event is deleted.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Warsaw")


def name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("Name") or value.get("Code") or "").strip()
    return str(value or "").strip()


def day_of(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value.date() if isinstance(value, datetime) else value
    if isinstance(value, dict):
        value = value.get("Date") or value.get("Value")
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def clock(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("Time") or value.get("Value")
    if not value:
        return None
    value = str(value)
    try:
        return datetime.strptime(value[:5], "%H:%M").strftime("%H:%M")
    except ValueError:
        return None


def stable_id(pupil: str, kind: str, source_id: Any) -> str:
    # Google Calendar event IDs allow lowercase base32hex characters; hex is safe.
    key = f"{pupil}:{kind}:{source_id}".encode("utf-8")
    return "v" + hashlib.sha256(key).hexdigest()[:40]


def all_day(day: date) -> dict[str, Any]:
    return {
        "start": {"date": day.isoformat()},
        "end": {"date": (day + timedelta(days=1)).isoformat()},
    }


def build_events(pupil: str, exams: list[dict], homework: list[dict],
                 schedule: list[dict]) -> list[dict]:
    events: dict[str, dict] = {}
    for kind, records in (("exam", exams), ("homework", homework)):
        for item in records:
            if not isinstance(item, dict) or item.get("Id") is None:
                continue
            day = day_of(item.get("DeadlineAt") or (item.get("DateAt") if kind == "homework" else None))
            if day is None:
                continue
            subject = name(item.get("Subject")) or "?"
            prefix = "Sprawdzian" if kind == "exam" else "Zadanie domowe"
            title = f"{prefix}: {subject}"
            details = [str(item.get("Type") or "") if kind == "exam" else "",
                       str(item.get("Content") or "")]
            event = {
                "id": stable_id(pupil, kind, item["Id"]),
                "summary": title,
                "description": "\n".join(v for v in details if v).strip(),
                **all_day(day),
                "extendedProperties": {"private": {"source": "eduvulcan",
                                                    "sourceType": kind,
                                                    "sourceId": str(item["Id"])}},
            }
            if kind == "exam":
                event["reminders"] = {"useDefault": False, "overrides": [
                    {"method": "popup", "minutes": 24 * 60}]}
            events[event["id"]] = event

    for item in schedule:
        if not isinstance(item, dict) or item.get("Id") is None:
            continue
        sub = item.get("Substitution")
        if not isinstance(sub, dict) or not sub:
            continue
        day = day_of(sub.get("ChangeDateAt")) or day_of(item.get("DateAt"))
        slot = sub.get("TimeSlot") or item.get("TimeSlot") or {}
        start, end = clock(slot.get("Start")), clock(slot.get("End"))
        if day is None or not start or not end:
            continue
        cancelled = bool(sub.get("ClassAbsence")) or (
            not sub.get("Subject") and not sub.get("TeacherPrimary"))
        subject = name(sub.get("Subject")) or name(item.get("Subject")) or "Zajęcia"
        label = "Odwołane" if cancelled else "Zastępstwo"
        event = {
            "id": stable_id(pupil, "lesson", f"{item['Id']}:{day.isoformat()}"),
            "summary": f"{label}: {subject}",
            "description": "\n".join(filter(None, [
                name(sub.get("Room")) or name(item.get("Room")),
                str(sub.get("PupilNote") or ""),
                str(sub.get("Reason") or ""),
                str(sub.get("Description") or ""),
            ])),
            "start": {"dateTime": f"{day.isoformat()}T{start}:00",
                      "timeZone": "Europe/Warsaw"},
            "end": {"dateTime": f"{day.isoformat()}T{end}:00",
                    "timeZone": "Europe/Warsaw"},
            "extendedProperties": {"private": {"source": "eduvulcan",
                                                "sourceType": "lesson",
                                                "sourceId": str(item["Id"])}},
        }
        events[event["id"]] = event
    return list(events.values())


async def fetch_school(credential_json: str, pupil_index: int,
                       start: date, end: date) -> tuple[str, list, list, list]:
    from vulcan_client.api import VulcanApi
    from vulcan_client.credential import Credential

    credential = Credential.from_dict(json.loads(credential_json))
    async with VulcanApi(credential) as api:
        accounts = await api.accounts()
        if not 0 <= pupil_index < len(accounts):
            raise ValueError("VULCAN_PUPIL_INDEX is out of range")
        account = accounts[pupil_index]
        pupil = account["Pupil"]
        rest = account["Unit"]["RestURL"]
        pupil_id = pupil["Id"]
        # Do not print school records; these contain a child's private data.
        exams = await api.exams(rest, pupil_id, start, end)
        homework = await api.homework(rest, pupil_id, start, end)
        schedule = await api.schedule(rest, pupil_id, start, end)
        if not all(isinstance(items, list) for items in (exams, homework, schedule)):
            raise ValueError("Unexpected eduVULCAN response type")
        return str(pupil_id), exams, homework, schedule


def calendar_service(oauth_json: str):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    info = json.loads(oauth_json)
    scopes = ["https://www.googleapis.com/auth/calendar.events"]
    credentials = Credentials.from_authorized_user_info(info, scopes=scopes)
    if not credentials.refresh_token:
        raise ValueError("GOOGLE_OAUTH_JSON must contain a refresh_token")
    credentials.refresh(Request())
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def synchronize(service, calendar_id: str, events: list[dict], dry_run: bool) -> dict[str, int]:
    from googleapiclient.errors import HttpError

    counts = {"prepared": len(events), "created": 0, "updated": 0, "unchanged": 0}
    if dry_run:
        return counts
    for event in events:
        event_id = event["id"]
        try:
            previous = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        except HttpError as exc:
            if exc.resp.status != 404:
                raise
            service.events().insert(calendarId=calendar_id, body=event).execute()
            counts["created"] += 1
            continue
        fields = ("summary", "description", "start", "end", "reminders", "extendedProperties")
        # Google may add response-only fields or timezone offsets. Compare the
        # relevant values, not the entire server object.
        desired = {key: event[key] for key in fields if key in event}
        old = {key: previous.get(key) for key in desired}
        # Google returns timed event values with UTC offsets, whereas input
        # values use local wall time + timeZone. Normalize to instants to avoid
        # patching unchanged events on every scheduled run.
        for boundary in ("start", "end"):
            if boundary in desired and "dateTime" in desired[boundary]:
                def instant(part):
                    raw = part["dateTime"]
                    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=ZoneInfo(part.get("timeZone") or "Europe/Warsaw"))
                    return parsed.timestamp()
                if old.get(boundary) and "dateTime" in old[boundary]:
                    if instant(old[boundary]) == instant(desired[boundary]):
                        old[boundary] = desired[boundary]
        if old == desired:
            counts["unchanged"] += 1
        else:
            service.events().patch(calendarId=calendar_id, eventId=event_id,
                                   body=desired).execute()
            counts["updated"] += 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and normalize without any Google Calendar calls")
    args = parser.parse_args()
    for key in ("VULCAN_CREDENTIAL_JSON", "GOOGLE_CALENDAR_ID"):
        if not os.getenv(key):
            raise SystemExit(f"Missing required configuration: {key}")
    if not args.dry_run and not os.getenv("GOOGLE_OAUTH_JSON"):
        raise SystemExit("Missing required configuration: GOOGLE_OAUTH_JSON")
    now = datetime.now(TZ).date()
    back = int(os.getenv("SYNC_DAYS_BACK", "7"))
    ahead = int(os.getenv("SYNC_DAYS_AHEAD", "60"))
    if not 0 <= back <= 365 or not 1 <= ahead <= 365:
        raise SystemExit("SYNC_DAYS_BACK/AHEAD must be in the range 0..365 / 1..365")
    pupil, exams, homework, schedule = asyncio.run(fetch_school(
        os.environ["VULCAN_CREDENTIAL_JSON"],
        int(os.getenv("VULCAN_PUPIL_INDEX", "0")),
        now - timedelta(days=back), now + timedelta(days=ahead)))
    events = build_events(pupil, exams, homework, schedule)
    if args.dry_run:
        counts = synchronize(None, os.environ["GOOGLE_CALENDAR_ID"], events, True)
    else:
        service = calendar_service(os.environ["GOOGLE_OAUTH_JSON"])
        counts = synchronize(service, os.environ["GOOGLE_CALENDAR_ID"], events, False)
    print(json.dumps(counts, sort_keys=True))
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as out:
            out.write("## eduVULCAN Calendar Sync\n\n")
            out.write(f"- Mode: {'dry-run' if args.dry_run else 'apply'}\n")
            for key, value in counts.items():
                out.write(f"- {key}: {value}\n")
            out.write("- Deletions: disabled\n")


if __name__ == "__main__":
    main()
