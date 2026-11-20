# School Calendar Sync (GitHub Actions)

Workflow: .github/workflows/school-sync.yml. Triggered by the existing
school_sync_run job via GitHub workflow_dispatch or manually in Actions.

Synchronizes exams, homework, substitutions, and cancelled lessons.
Normal timetable lessons are intentionally excluded.

## Required repository Actions secrets

Set in Settings > Secrets and variables > Actions:

- VULCAN_CREDENTIAL_JSON: Entire registered-device JSON produced by the
  standalone vulcan-client registration; not the initial /api/ap token.
- GOOGLE_OAUTH_JSON: Authorized-user JSON containing a Google refresh_token,
  client_id, client_secret and token_uri. The downloaded Desktop OAuth
  client_secret file alone is NOT sufficient.
- GOOGLE_CALENDAR_ID: Destination calendar identifier.
- VULCAN_PUPIL_INDEX (optional repository variable): pupil index from
  vulcan accounts, default 0.

Do not commit or print private tokens, pupil data or secret JSON. Google OAuth
consent in external Testing mode may produce refresh tokens expiring in 7 days.
Ensure the connected Google user has permission to edit the target calendar.

## Validation and rollout

1. Configure all three Actions secrets and the optional pupil index.
2. Actions > School Calendar Sync > Run workflow > dry_run=true.
   This fetches and prepares events but performs no Google Calendar calls.
3. Review the prepared count in the Actions job summary.
4. Run with dry_run=false, confirm created events in Google Calendar.
5. Run again; previously created events should be unchanged.
6. Verify the agent's 30-minute dispatch and MQTT reachable status.

The script uses Python 3.12, Europe/Warsaw, and fetches 7 days back and 60
days ahead. Exam notifications are scheduled 24 hours before the all-day event.
Events use deterministic IDs derived from pupil + source type + source ID.
No deletions are performed, even if eduVULCAN responds with no items. Changes
to event data are updated in place. Only counts are written to the job summary.

## Known limitations

- Events that disappear from eduVULCAN are not removed automatically.
- Only lessons with a Substitution object are included.
- Cancellation interpretation should be checked against actual school data.
- HTTP 204 from GitHub confirms dispatch acceptance, not successful completion
  of the workflow.
- Missing IDs or invalid dates are skipped. Do a first dry-run and inspect the
  expected count before enabling apply.

Tests: python -m pytest -q tests/test_school_calendar_sync.py
