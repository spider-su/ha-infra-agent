# Gas and water calculated meter values through MQTT

The gas-invoice and water-invoice jobs are configuration-backed meter mappers.
They read a real starting reading, its timestamp, seasonal daily steps, and the
summer/winter mode from each job's reading.yaml. The provider integrates the
configured step over the current date/time and publishes the result through the
existing MQTT pipeline.

There is no Home Assistant API dependency and no HIA_HOME_ASSISTANT_TOKEN
requirement. The only user-maintained inputs are the real meter facts and
configuration in infra-config, normally changed a few times per year.

Each job publishes two different classes of value:

- cumulative_m3 is the configured real meter reading. It is the only entity
  marked total_increasing and is suitable as a measured MQTT source.
- estimated_cumulative_m3 and estimated_since_reading_m3 are calculations.
  They are explicitly named estimates and use state_class measurement; they
  must not be presented as measured Energy input.
- last_reading_m3, last_reading_at, active_mode, step_m3_per_day, and
  calculated_at expose provenance and calculation state for the dashboard.

The configured real reading is not silently advanced. A new real meter reading
must be entered in the appropriate reading.yaml together with its timestamp.
The estimator then calculates from that fact until the next update. The
seasonal calendar must cover every day exactly once; auto mode selects the
configured summer or winter interval, while summer or winter mode can pin one
mode for a controlled test or exceptional period.

Readings must be finite and nonnegative cubic metres. Invalid configuration
fails validation rather than producing zero or a fabricated measurement. A
decreasing real reading is a meter correction/replacement decision and must be
reviewed against HA statistics before changing the total_increasing source.

MQTT publication retains the existing job IDs and topics for continuity:
home/meters/gas and home/meters/water. Discovery identifies the real cumulative
entity separately from the calculated entities. State expires after 15 minutes
without a successful publication; failed jobs do not refresh a cached value.

## Deployment and migration

1. Update the real reading and timestamp in the infra-config source when a meter
   is read. Keep the seasonal rates and mode in the same file.
2. Build and publish an image containing the calculated_meter provider.
3. Roll out the chart with jobs.meters enabled. The chart mounts both job
   directories and does not inject an HA token.
4. Verify loaded jobs, MQTT discovery, live cumulative values, estimates,
   expiry/failure behavior, and HA registry entity IDs.
5. Keep sensor.gas_invoice_tracker and sensor.water_invoice_tracker and their
   existing statistics until a history-preserving Energy migration is arranged.
   Do not delete the dormant HA helpers or aliases during this cutover.
6. If the dashboard needs the projection, use the explicitly estimated entities.
   Keep measured Energy statistics sourced only from cumulative_m3 or a verified
   historical alias.

This implementation changes the agent and chart source. It does not itself
restart Home Assistant, change live Energy preferences, or deploy the
workload.
