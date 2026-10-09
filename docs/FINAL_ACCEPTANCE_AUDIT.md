# Final Acceptance Audit

**Date:** 2026-10-09  
**Recommendation:** NOT READY for final acceptance. No deployment was initiated by this audit.  
**Boundaries:** Read-only observations on the Google VM, Cloud Run endpoint, Kubernetes, registry, and Argo CD. Local watchdog changes are proposals only.

## Findings

### Investory Cloud Run

The watchdog's configured URL is `https://investory-61359240267.europe-central2.run.app/actuator/health`. At approximately 12:00 Warsaw time (within its configured 09:00-22:00 operating window), requests from both the Google VM and this workstation resolved DNS, completed TLS, and returned HTTP 503 with Spring Actuator JSON `status: DOWN`. The response had no `WWW-Authenticate` challenge, so this endpoint does not require HTTP authentication for the observed request. This is not a VM-specific transport failure; the application health endpoint responds. The VM config intentionally skips 22:00-09:00 Europe/Warsaw.

The active monitor recorded 91 daytime failures by approximately 10:14 UTC and an empty Investory alert marker is present. Its failure threshold remains 3; recovery remains 2. No thresholds, state, or notifications were changed by this audit. The marker does not prove recipient delivery, which was not independently verified.

The active gcloud identity lacks permission to inspect Cloud Run service configuration in project number `61359240267`. Therefore the service's min/max instances, revision, scaling schedule, and internal health component cannot be confirmed. The 503 Actuator response shows the endpoint is reachable and the application reports DOWN; it does not establish why. The least-risk next step is a Cloud Run owner diagnosis, not suppressing the watchdog.

### Investory Agent Job

The live generated Job summary returned `investory` as valid, never executed, and freshness UNKNOWN with no `maxAgeSeconds`. The source Agent YAML already has a 72-hour max age, but the GitOps Helm template omitted it. The chart also scheduled a 22:00 run at the Cloud Run sleep boundary. Prepared GitOps PR [#22](https://github.com/spider-su/ops-autopilot/pull/22) sets `freshness.maxAge: 72h` and moves weekday execution to 09:00-21:00 Warsaw time. The 72-hour limit spans the expected 60-hour Friday-to-Monday gap plus 12 hours. This does not mask the independent Cloud Run failure.

The current Agent API (10:09 UTC) reports `status=ok`, process and scheduler alive, MQTT connected, six loaded Jobs, zero invalid Jobs, and a recent observation. Proxmox, K3s, Network, Solarman, and Speedtest had fresh results. Investory had not yet run after the latest pod started, as expected before its next hourly cron tick. The watchdog proposal requires at least four loaded Jobs and treats Proxmox/K3s as required; Investory, Solarman, Network, and Speedtest are optional warnings. Required Jobs failing, stale, invalid, unexecuted, or lacking valid freshness fail the aggregate target.

### Watchdog State and Permissions

The VM timer is enabled and runs every two minutes. The current `/api/jobs` evaluator checks data but not semantic `/health`; the HTTP health probe currently accepts any successful HTTP response. A local proposal adds semantic health checks for overall status, process, scheduler, MQTT (required in the current deployment), invalid count, minimum Job count, and an `observedAt` timestamp no older than five minutes.

The existing sleep branch deletes the Investory alert marker and resets counters. That contradicts `SKIP never changes state` and can hide recovery or permit repeat alerts. The local proposal changes scheduled downtime to a state-preserving SKIP; thresholds and the existing state machine remain unchanged.

Observed file modes violate the requested restriction: both root-owned config files under `/opt/proxmox-monitor` are 0644; `/var/lib/proxmox-monitor` is 0755; state files and `notifications.log` are 0644. The service runs as root. Applying permissions requires interactive sudo; no permission changes were made.

### K3s and Image Identity

The only failed Pod found was `monitoring/monitoring-grafana-7c6dbfd99d-98dhm`, owned by a ReplicaSet and Evicted on 2026-10-06. A newer Grafana Pod is Running; no active Grafana failure or failed Home Infra Agent Pod was present. No aggregate-rule change is justified.

The requested Agent commit `3e3b24c` has successful test and Docker Publish workflows and a published image. OCI inspection resolves its tag to index digest `sha256:cd36e278dd964ae433957fb89b199bbeb241096dfb7252f42a0e17b089bd8e2d`, linux/amd64 manifest `sha256:fdafe80fa40f059657a34d8e727bfc6288521ee248b3971ac7b1314aced10436`, and image config `sha256:5def45179fec8fbaad2df038b8da81f8c58d5baf4635ee5568910a6ee62dd1ea`.

Since that requested baseline, Agent main advanced to commit `8b87111` and GitOps main advanced to `8aee2bb`; the Agent CI and Docker Publish passed. GitOps main specifies the immutable `sha-8b87111` tag, and the live amd64 Pod reports image ID `sha256:0e20d16668d9182771a8a490cfbaf51d8919a1311a8ebb56726b598e61ccf540`, matching the registry tag's OCI index digest. That index selects amd64 manifest `sha256:9b32d88b86aa61c51618ac2fd2a2de8d7091589cdff9bc3b68249db531202b43`, whose config digest is `sha256:ab7bd254762d73cc7a68723dcb86f04850b1e076893fb81747f515462b705071`. The node architecture is amd64. A CRI image ID need not equal the platform manifest or config digest.

Argo reports the Home Infra Agent dev Application Synced/Healthy at current GitOps main `8aee2bb`. The existing auto-sync reconciled the already-merged image update during this audit: the Agent Pod changed from `sha-fa28e0a` to `sha-8b87111`. No PR merge, rollout command, or image edit from this audit caused that rollout. The image had already advanced beyond `3e3b24c`; no downgrade or duplicate image-promotion PR was prepared. Existing MQTT IDs/topics and chart resource limits are unchanged.

## Prepared Changes and Tests

- The prepared Agent change aligns the source Investory cron with the 21:00 end and adds this audit report.
- GitOps PR #22 adds the missing generated freshness policy and operating-window correction.
- Local-only watchdog proposal: `/Users/alex/evaluate_jobs.py`, `/Users/alex/proxmox-monitor-check-proposed.sh`, tests at `/Users/alex/test_watchdog_acceptance.py`. The fetched baseline shell script matched the VM copy by SHA-256 before edits. These files are not installed on the VM.
- The 22 isolated watchdog tests use temporary state and stub curl/logger/notifier commands. They cover healthy/malformed/stale semantic responses, scheduler/MQTT/config errors, required/optional Job policy, failure threshold, duplicate suppression, recovery, delivery failure logging, scheduled skips, and restrictive created state permissions. No real notification or production failure was simulated.
- `bash -n`, Python compilation, and watchdog tests: passed (22 tests).
- Agent full suite: 113 passed, including Mosquitto integration.
- GitOps Helm lint and full `scripts/validate.ps1`: passed (304 resources; 284 valid, 0 invalid, 0 errors, 20 skipped).

## Required Before Acceptance (At Initial Audit)

1. Human review and merge of Agent and GitOps PRs. GitOps merge can trigger the existing dev Argo auto-sync; no merge was performed.
2. Approval to apply the local watchdog proposal to the VM, add `INFRA_AGENT_MQTT_REQUIRED=true`, `INFRA_AGENT_MIN_JOBS=4`, and `INFRA_AGENT_HEALTH_MAX_AGE_SECONDS=300` to `monitor.conf`, and restrict existing file modes. Proposed permission commands are:
   ```sh
   sudo chown root:root /opt/proxmox-monitor/monitor.conf /opt/proxmox-monitor/monitor.conf.before-infra
   sudo chmod 0600 /opt/proxmox-monitor/monitor.conf /opt/proxmox-monitor/monitor.conf.before-infra
   sudo chown root:root /var/lib/proxmox-monitor
   sudo chmod 0700 /var/lib/proxmox-monitor
   sudo find /var/lib/proxmox-monitor -maxdepth 1 -type f -exec chmod 0600 {} +
   ```
   The proposed check script also applies umask 077 and enforces 0700/0600 state modes. Do not run these until approved.
3. Cloud Run owner access/diagnosis for project `61359240267`, followed by approval for any service configuration change. No Cloud Run configuration was changed.
4. After the approved VM change, verify semantic health, Job policy, file ownership/modes, and the existing failure/recovery counters without forcing a production notification.

The live Investory 503 during its stated operating window, unverified Cloud Run cause, un-applied VM semantic checks, and open permission findings prevent a READY recommendation.

## Post-Approval Update

**Observed:** 2026-10-09, approximately 12:37 Europe/Warsaw. This addendum records state after PR review/merge and explicit approval for the VM changes. The initial findings above remain an audit-time snapshot; the status below supersedes its open-PR and un-applied-VM statements.

### Merged Changes and Argo Reconciliation

- Agent PR [#13](https://github.com/spider-su/ha-infra-agent/pull/13) merged as `9dfe0d3`; GitOps PR [#22](https://github.com/spider-su/ops-autopilot/pull/22) merged as `a487f1a`. Their CI/repository validation checks passed.
- After explicit user authorization, the `home-infra-agent-dev` Argo Application was manually synced to GitOps revision `7de4940`. Argo reported Synced/Healthy. The Deployment rolled due to rendered configuration change and retained image `aserobaba/home-infra-agent:sha-8b871113d5bd412c0fc46f22a722736036fcc4ac`; no image promotion or resource-limit change occurred.
- The live ConfigMap and `/api/jobs` confirmed the Investory schedule `0 9-21 * * 1-5` in `Europe/Warsaw` and `freshness.maxAgeSeconds=259200` (72 hours). At observation, Investory was valid but never executed after the rollout, so its status/freshness remained UNKNOWN pending its next scheduled run. The existing MQTT topics/entity IDs were not changed.
- Live `/health` returned HTTP 200 with status `ok`, process/scheduler/MQTT true, six loaded Jobs, zero invalid Jobs, and a recent observation. The K3s job emitted WARN while the old Grafana Pod remained Evicted/ContainerStatusUnknown; all nodes were Ready and the replacement Grafana Pod was Running. No aggregate-rule change or direct Pod restart was made.

### Approved VM Watchdog Installation

- After explicit approval, read-only preflight confirmed the VM's `check.sh` and evaluator still matched their audited baseline hashes before replacement. Local validation passed: 22 isolated watchdog tests, shell syntax, and Python compilation.
- Installed `/opt/proxmox-monitor/check.sh` has SHA-256 `647a463f2ea1b01b0126611dae65304242a2fc5c3b2bb90e2977a8d02358ac06`; installed `/opt/proxmox-monitor/evaluate_jobs.py` has SHA-256 `2b406e841047a72a54647a645302ce6cc95e10e2b790983a55009b704c63a2b5`. Timestamped pre-change backups were retained for both scripts and `monitor.conf` under `/opt/proxmox-monitor/*pre-acceptance-20261009`.
- Added `INFRA_AGENT_MQTT_REQUIRED=true`, `INFRA_AGENT_MIN_JOBS=4`, and `INFRA_AGENT_HEALTH_MAX_AGE_SECONDS=300`. Both root-owned config files are mode `0600`; `/var/lib/proxmox-monitor` is root-owned `0700`; state files and `notifications.log` are root-owned `0600`. The script applies `umask 077` and enforces restrictive state modes on subsequent runs.
- The existing two-minute timer remained active. Its natural runs completed with service exit status 0; logs showed the semantic `infra-agent` target UP and `/api/jobs` evaluated successfully. Optional Investory/Speedtest no-result conditions were warnings. Investory's existing alert marker remained present while its failure counter continued advancing; observed logs showed no duplicate alert-sent event. No check was manually invoked and no notification was manually sent.

### Remaining Acceptance Gates

**Recommendation remains NOT READY.** The Cloud Run endpoint's daytime HTTP 503/status DOWN was not re-diagnosed or changed. The active Google account still lacks Cloud Run inspection permission. Verify Investory freshness after its next scheduled execution, investigate why K3s reports WARN for the historical Evicted Grafana Pod, and observe the watchdog through critical-Job failure/recovery behavior without forcing a notification. Cloud Run owner diagnosis and approval for any service change remain outstanding. No live Cloud Run configuration or alert marker was changed.
