# Home Assistant utility consumption estimates

Install `packages/utility_consumption.yaml` as `/config/packages/utility_consumption.yaml` on the Home Assistant host. Ensure `/config/configuration.yaml` enables the package directory:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

The package estimates the current month's gas and water consumption from editable daily baselines and summer/winter factors. Automatic season mode uses winter from November through March and summer from April through October; Helpers allow a manual season override and adjustments to every baseline/factor. It sends one persistent notification daily at 20:00 Europe/Warsaw, updating the same notification each day.

To calibrate with actual meter values (about quarterly), enter both cumulative readings in the Utility helpers and press **Record quarterly gas and water readings**. The first entry establishes the reference; the next valid entry recalculates daily baselines from consumption since that reference. Estimates remain provisional until that second entry.
