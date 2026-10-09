# Integration Onboarding and Operations

This guide covers the operational workflow. The README's Configuration section remains the detailed reference for supported fields and transformations.

## Architecture

The Job Engine discovers and validates `jobs/<id>/job.yaml` and sibling task YAML files, schedules Jobs, and isolates task failures. The generic HTTP provider handles ordinary JSON GET/POST APIs. Specialized providers own protocol-specific behavior: ping, Kubernetes API, Investory's read-only PostgreSQL summary, and Solarman authentication/device selection.

Providers return primitive values and status. Results are normalized before health rules and MQTT publication. Freshness is based on the last successful `OK` or `WARN` result and `freshness.maxAge`, independently from the latest result status. Results are held in memory, not history.

The MQTT adapter publishes state and retained Home Assistant Discovery config. Home Assistant owns entity registry, dashboards, and automations. Infra Agent monitors configured Jobs; it is not an independent notification service. The separate Google VM watchdog must independently monitor critical reachability and deliver alerts even when the home network or agent is unavailable. It should consume the agent summary rather than duplicate detailed collection. This PR does not change the watchdog.

## Choose an Integration

| Integration | Preferred implementation |
|---|---|
| Standard JSON API or simple health endpoint | YAML-only HTTP provider |
| Existing Proxmox/K3s checks | Reuse `ping` or `kubernetes` provider |
| Investory portfolio summary | Existing read-only `investory_postgres` provider |
| Solarman Cloud | Existing `solarman` provider |
| Protocol authentication, pagination, stateful selection, or parsing not expressible in YAML | Specialized provider only for the concrete limitation |
| Critical independent availability and notifications | External Google VM watchdog |

Prefer YAML before Python. Do not add Home Assistant behavior to providers.

## YAML-Only HTTP Onboarding

The complete, schema-validated example is `config/examples/http-service/`. Copy it to `config/jobs/<stable-job-id>/`; the directory name is the job ID and each sibling YAML file (except `job.yaml`) is a task. Below is the supported shape from that example:

```yaml
# job.yaml
name: Example HTTP Service
schedule:
  interval: 60s
timeout: 5s
freshness:
  maxAge: 180s
mqtt:
  enabled: true
  topic: home/example-service
  device:
    name: Example HTTP Service
    manufacturer: Custom
    model: JSON status API
  entities:
    online:
      name: API online
      icon: mdi:web
    nodeState:
      name: Node state
      component: binary_sensor
      payload_on: UP
      payload_off: DOWN
    ready:
      name: Ready nodes
      unit_of_measurement: nodes
      state_class: measurement
    temperature:
      name: Temperature
      unit_of_measurement: "°C"
      device_class: temperature
      state_class: measurement
    humidity:
      name: Humidity
      unit_of_measurement: "%"
      device_class: humidity
      state_class: measurement
    updatedAt:
      name: API updated
      device_class: timestamp
```

```yaml
# health.yaml
type: http
url: https://service.internal/api/status
method: GET
timeout: 5s
headers:
  Accept: application/json
auth:
  type: bearer
  tokenEnv: EXAMPLE_SERVICE_API_TOKEN
expectedStatusCodes: [200]
maxResponseBytes: 1048576
extract:
  online:
    path: $.status.online
    type: boolean
    required: true
  ready:
    path: $.cluster.ready
    type: integer
    required: true
  total:
    path: $.cluster.total
    type: integer
    required: true
  nodeState:
    path: $.node.state
    type: string
    map:
      ready: UP
      offline: DOWN
  temperature:
    path: $.sensor.temperature_centi
    type: number
    multiply: 0.01
    round: 1
  humidity:
    path: $.sensor.humidity
    type: number
    default: null
  updatedAt:
    path: $.meta.updatedAt
    type: timestamp
    required: true
health:
  rules:
    - field: online
      equals: true
    - field: ready
      equalsField: total
    - field: nodeState
      equals: UP
  onFailure: WARN
```

The URL and token variable are placeholders. This task expects a JSON response shaped like:

```json
{
  "status": {"online": true},
  "cluster": {"ready": 3, "total": 3},
  "node": {"state": "ready"},
  "sensor": {"temperature_centi": 2175, "humidity": 42.5},
  "meta": {"updatedAt": "2026-10-09T10:00:00Z"}
}
```

Normalized outputs are `online`, `ready`, `total`, `nodeState`, `temperature` (21.8), `humidity`, and `updatedAt`. All health rules must pass; failure is `WARN` in this example. Discovery also publishes built-in Job status, last-run, duration, and freshness entities. IDs/topics derive from stable job and field names, not display names. A task-qualified value such as `health.temperature` is preserved; a short alias is available only when one configured task owns that field.

### Procedure

1. Identify the endpoint, network route from the actual Pod/host, auth needs, response size, and success/failure response shapes.
2. Copy `config/examples/http-service` to `config/jobs/<stable-id>` and edit only the copy.
3. Set interval, Job/task timeout, and freshness threshold. HTTP timeout bounds DNS, connection, redirects, headers, and body reads as one deadline.
4. Map fields the API really returns; choose required/default behavior and health rules deliberately.
5. Add metadata only for useful stable MQTT entities. Avoid renaming fields or changing existing entity components.
6. Validate and test locally:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
cp -R config/examples/http-service config/jobs/example-service
# Edit the copied URL and mappings. Keep credentials out of YAML.
export EXAMPLE_SERVICE_API_TOKEN='<value-from-local-secret-store>'
python -c 'from pathlib import Path; from home_infra_agent.config import discover_jobs; jobs, errors = discover_jobs(Path("config/jobs")); print(f"{len(jobs)} jobs, {len(errors)} errors"); raise SystemExit(bool(errors))'
python -m pytest -q
HIA_CONFIG_DIR="$PWD/config" HIA_HOST=127.0.0.1 home-infra-agent
```

Never commit or print secret values; prefer a local secret manager over shell history. Existing jobs may need private routes or credentials, so an unreachable source can yield a runtime error even when schema validation succeeds.

7. Open a PR against this repository's `main` with config, validation/test output, a redacted response sample, and entity compatibility assessment. Do not regenerate the discovery golden fixture to conceal an unexplained change.
8. After human review/merge, deploy through the existing adjacent `ops-autopilot/applications/home-infra-agent/` GitOps workflow. This repository builds/scans an image but does not deploy it. Follow that checkout's README for promotion; record the image digest and config revision.
9. Verify `/health`, `/api/jobs`, `/api/jobs/<id>`, and Home Assistant's new and existing entities. Verify the correct broker and trusted ingress after a Pod restart.
10. Exercise failure and recovery against a safe endpoint/environment. Confirm status/freshness changes, stale values are cleared, discovery identity stays constant, and recovery repopulates state.
11. On failure, revert to the recorded image digest/config revision through GitOps and verify the previous release. Do not clear retained topics as rollback.

## API and Deployment Checks

For local use, checked-in `config/application.yaml` disables MQTT and binds to loopback:

```sh
HIA_CONFIG_DIR="$PWD/config" HIA_HOST=127.0.0.1 .venv/bin/home-infra-agent
curl --fail --show-error http://127.0.0.1:8080/health
curl --fail --show-error http://127.0.0.1:8080/api/jobs
curl --fail --show-error http://127.0.0.1:8080/api/jobs/example-service
```

`GET /health` means the process answered, not that all Jobs are healthy. `/api/jobs` gives compact summaries; `/api/jobs/{id}` includes normalized output. `POST /api/jobs/{id}/run` is the only mutating API action and requires a same-origin Origin or Referer. The API has no user authentication and must remain on a trusted private network. Kubernetes binds the Pod interface behind an internal Service; ingress should be reachable only through the private/home/Tailscale network.

After deployment, check loaded/invalid Job counts, scheduler/MQTT state, each Job's last success/freshness, pod events/logs, broker connectivity, and HA entities. The existing Traefik hostname `ha-infra.home.k3s.com` is allowed by Host validation. Extra hostnames require `web.allowedHosts` or `HIA_ALLOWED_HOSTS`; allowlisting a Host is not access control and does not replace private DNS, routing, ACLs, or authentication at another layer.

## MQTT Compatibility

- Keep job IDs, field names, state/discovery topics, unique IDs, device identifiers, and existing component types stable. HA entity IDs can be used by dashboards and automations; changing identity can break those references.
- Qualified output names remain stable. A short alias exists only for a unique configured owner. Configured values from failed tasks remain as null; known retained discovery config is not removed on a task failure.
- Reserved built-in entity fields: `status`, `last_run`, `duration_ms`, `last_success`, `freshness`.
- Retained discovery/state is replayed on reconnect. Broker restart does not erase retained records.
- Removed/renamed config can leave a stale retained discovery record. This is confirmed but deferred: safe automatic cleanup needs prior-topic state or broker-wide enumeration, neither currently exists. Identify the exact old discovery topic from broker/HA diagnostics, remove the HA entity, then clear only that verified retained config topic:

```sh
mosquitto_pub -h <private-broker> -t 'homeassistant/sensor/<verified-unique-id>/config' -r -n
```

Never wildcard-delete discovery/state topics. Preserve `tests/fixtures/mqtt_discovery_baseline.json`; review any proposed change as an explicit migration. Existing entity identity remains unchanged by this PR.

## Security

- Supply HTTP/MQTT credentials via deployment secrets and environment references. Use read-only, least-privilege accounts. Never put secret values in YAML, source, tickets, or diagnostic output.
- Private endpoints must be reachable only on trusted routes. Restrict API, broker, and ingress at network/ACL layers; Host validation is not authentication.
- Host validation permits loopback/private/link-local/Tailscale IP literals and `ha-infra.home.k3s.com` by default. Configure other names explicitly. POST Run checks Host and same-origin, but the API has no user identity/authorization.
- Job results can expose infrastructure names, addresses, or service data; protect API, MQTT, and logs accordingly. Runtime exception details are redacted; validation errors can describe invalid field names/paths.
- A timed-out POST may already have been processed remotely. Treat its outcome as unknown, not proof of non-execution.

## Troubleshooting

| Symptom | Diagnostic steps |
|---|---|
| Job not discovered | Confirm `config/jobs/<id>/job.yaml`; inspect `/api/jobs` validity and startup logs. |
| Invalid YAML/config | Run `discover_jobs` validation above; inspect the file/task-specific safe validation error. |
| HTTP authentication failure | Verify variable names and secret injection without printing values; check read-only account permissions. |
| Job timeout | Check endpoint DNS/routing from the Pod, latency, body size, and task/Job budget. |
| Job `ERROR` | Inspect `/api/jobs/<id>` sanitized error and task status; compare JSON paths/status codes with the response. |
| Job never executed | Check `valid`, `schedulerRunning`, schedule/timezone, due interval, and active execution. |
| Freshness `UNKNOWN` | No successful timestamp exists or no `maxAge` is configured. |
| Freshness `STALE` | Last success exceeded `maxAge`; inspect route, latest errors, and schedule. |
| MQTT disconnected | Check broker route/DNS, port, ACL, username, and secret reference; do not dump environment/config. |
| Entity missing | Check MQTT enabled/topic, discovery/state topics, broker ACL, HA MQTT integration, and metadata validation. |
| Duplicate/stale entity | Compare unique IDs and exact retained discovery topic; use the specific cleanup procedure above. |
| Traefik failure | From a trusted client test ingress hostname, `/health`, and `/api/jobs`; inspect route/Host config without making it public. |
| Watchdog reports DOWN | Independently test from the VM; inspect private DNS, Tailscale ACL/routes, ingress, and Pod health. Its alert path must work during home outage. |

Kubernetes diagnostics (do not inspect/dump Secret values):

```sh
kubectl -n home-infra-agent get pods
kubectl -n home-infra-agent logs deploy/home-infra-agent --since=15m
kubectl -n home-infra-agent rollout status deploy/home-infra-agent
curl --fail --show-error https://ha-infra.home.k3s.com/health
curl --fail --show-error https://ha-infra.home.k3s.com/api/jobs
```

The external Traefik configuration is not owned by this repository; see the adjacent ops-autopilot chart README for current route details.

## Maintenance Policy and Limits

Stable/maintenance-oriented: bug and security fixes are welcome; MQTT discovery, entity IDs, topics, and API contracts must remain compatible. Standard HTTP integrations use YAML. A specialized provider requires a concrete protocol limitation. No database, queue, workflow engine, monitoring framework, speculative abstraction, or unnecessary provider/locking/exception refactor.

Known limits: in-memory results, startup-only config (no hot reload), unauthenticated API, explicit cleanup for discovery entities removed from config, and no safe forced termination of arbitrary Python worker threads. Repository tests do not verify production credentials, deployment, ingress, broker state, dashboards, or watchdog reachability. Perform those checks after human review in the target environment; this PR does not deploy.

