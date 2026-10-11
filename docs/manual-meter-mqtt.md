# Gas and water manual readings through MQTT

The `gas-invoice` and `water-invoice` jobs bridge the existing Home Assistant
`input_number.historical_gas_consumption` and `input_number.historical_water_consumption`
helpers to typed cumulative MQTT sensors. They do not read invoices, estimate
hourly consumption, or create physical smart meters. Update the real helpers
when a new meter/invoice reading is available.

Each job polls independently every five minutes. It requires the existing
Home Assistant API and `HIA_HOME_ASSISTANT_TOKEN` supplied through the runtime
secret mechanism, never YAML or committed files. Only authenticated GETs to
the two helper endpoints are made. MQTT configuration, scheduling, bounded
HTTP requests and credential-safe redirects reuse the existing agent pipeline.

Meter readings must be finite, nonnegative and in cubic metres (`m³`/`m3`).
Unavailable, nonnumeric, mismatched entity/unit and HTTP/authentication failures
produce ERROR and null readings, never zero. Discovery uses gas/water device
class, `total_increasing` and `m³`. State expires after 15 minutes without
successful publication; numeric readings require both agent MQTT LWT online
and a successful, fresh per-job result. An ERROR makes them unavailable
immediately rather than leaving the last counter looking current.
On a failed job the invalid reading becomes unknown rather than refreshing a
cached cumulative value. Result freshness diagnoses last successful collection.

Unchanged readings are normal for manual meters. Source age does not mean the
collector is broken. `source_state_changed` is HA's state timestamp, which may
change after HA restarts; it is not a verified invoice date. Job Last run and
Last success describe collection, not meter activity.

A lower reading is rejected relative to the last accepted reading in the same
agent process. This guard is not durable across agent restarts. Before entering
a corrected/decreased meter reading or replacing a meter, review HA statistics
and the baseline intentionally; `total_increasing` treats a decrease as a reset.
Do not infer consumption rates from these periodic manual updates.

## Deployment and migration

1. Build a new image containing the provider; mount both job directories.
2. Supply an authorized HA read token as `HIA_HOME_ASSISTANT_TOKEN` through the
   deployment's secret mechanism. Never transfer credentials without approval.
3. Verify loaded/valid jobs, MQTT connectivity, live cumulative values matching
   the helpers, expiry/failure behavior and statistics before changing Energy.
4. Keep `sensor.gas_invoice_tracker`/`sensor.water_invoice_tracker` and their
   existing statistics until a history-preserving entity migration is arranged.
   Actual new MQTT entity IDs must be read from HA's registry, not guessed.
5. Cut over one source at a time with backups and compare Energy readings.
   Adding a new statistic ID does not automatically merge old history. Do not
   delete the existing templates/helpers or rewrite statistics blindly.

This staged implementation does not automatically change Energy preferences,
the K3s chart, secrets, or live integrations. The chart must both generate the
job ConfigMap files and mount them in Deployment items; merely adding files to
the agent repository does not activate production collection.
