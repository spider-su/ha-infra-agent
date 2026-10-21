# Gas and water calculated meter values through MQTT

The gas-invoice and water-invoice jobs are configuration-backed meter mappers.
They read a real starting reading, its timestamp, seasonal daily steps, and the
summer/winter mode from each job's reading.yaml. The provider integrates the
configured step over the current date/time and publishes the result through the
existing MQTT pipeline.

There is no Home Assistant API dependency and no HIA_HOME_ASSISTANT_TOKEN
requirement. The only user-maintained inputs are the real meter facts and
configuration in infra-config, normally changed a few times per year.

Each job publishes a continuous Energy-facing projection plus diagnostic values:

- cumulative_m3 starts at the latest configured real meter reading and advances
  continuously using the configured seasonal rate. It is marked total_increasing
  so Home Assistant Energy can use the projected cumulative series.
- estimated_cumulative_m3 mirrors that same projection for diagnostics.
  estimated_since_reading_m3 exposes only the projected delta since the latest
  real reading. Both remain explicitly named estimates with state_class
  measurement.
- last_reading_m3, last_reading_at, active_mode, step_m3_per_day, and
  calculated_at expose provenance and calculation state for the dashboard.
- real_consumption_m3, unit_price_pln_per_m3, variable_charge_pln,
  fixed_charge_pln, total_charge_pln, billing_currency, and charge_status are
  the optional payment layer. They remain unavailable or report
  prices_not_configured until a previous real reading and exact price periods
  are supplied.

When a new real meter reading is entered in the appropriate reading.yaml
together with its timestamp, cumulative_m3 rebases to that value immediately on
the next job run. It then continues from the new baseline using the configured
seasonal rate. Append the prior real reading to reading_history before replacing
the current reading. Only deltas between real readings can become payable
consumption. The seasonal calendar must cover every day exactly once; auto mode
selects the configured summer or winter interval, while summer or winter mode
can pin one mode for a controlled test or exceptional period.

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
6. Use cumulative_m3 as the continuous projected Energy source. Keep the
   explicitly estimated entities available for diagnostics and dashboards that
   need to show the projection delta or provenance.

This implementation changes the agent and chart source. It does not itself
restart Home Assistant, change live Energy preferences, or deploy the
workload.
