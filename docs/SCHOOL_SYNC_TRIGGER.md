# School sync trigger (stage 1)

The existing job scheduler runs the generic HTTP POST task every 30 minutes.
The mock endpoint returns HTTP 204, matching GitHub's workflow_dispatch
acceptance response. No GitHub workflow or school data is involved yet.

The job is `school_sync_run` and publishes MQTT state under
`home/school_sync_run`. The HTTP provider's `reachable` and
`statusCode` values, plus the built-in job status, are visible in HA.

**Important limitation:** a 204 means dispatch accepted, not that a
workflow completed successfully. The generic provider reports errors as
task/job ERROR; on failure the configured `reachable` field may be
null/unknown rather than boolean false. A guaranteed false value for
transport failures would require a small generic HTTP normalization change
and tests, which is intentionally outside this configuration-only stage.

To switch to the real GitHub workflow dispatch:

- Change `trigger.yaml` URL to
  `https://api.github.com/repos/OWNER/REPO/actions/workflows/sync.yml/dispatches`
- Add `auth: {type: bearer, tokenEnv: GITHUB_ACTIONS_TRIGGER_TOKEN}`
- Add headers `Accept: application/vnd.github+json` and
  `X-GitHub-Api-Version: "2022-11-28"`
- Add `body: {ref: main}` and keep `expectedStatusCodes: [204]`
- Inject the fine-grained GitHub token through the deployment secret
  mechanism, never in the repository.

Verify discovery with `discover_jobs(Path("config/jobs"))`, then run
the task manually via the existing UI or API and check MQTT. An HTTP POST
may have been accepted remotely even if the local request times out.
