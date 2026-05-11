# ČEZ Distribuce Readings

Home Assistant custom integration for reading electricity meter data from the ČEZ Distribuce portal.

## Current features

- Login to ČEZ Distribuce portal
- Reauthentication flow when credentials expire or change
- Load supply points
- Load meter reading history
- Optional 15-minute PND data branch with a separate refresh interval
- Adaptive retries, relogin, and refresh backoff on repeated failures
- Options flow (change update interval, detailed history, and optional PND settings without removing integration)
- Create sensors for:
  - VT meter state
  - NT meter state
  - total meter state
  - last reading period VT consumption
  - last reading period NT consumption
  - last reading period total consumption
  - archive readings and period counts
  - refresh health (`ok` / `warn` / `error`)
  - optional PND consumption / power summary sensors
- Load HDO / low tariff signal schedule
- Create binary sensors:
  - low tariff currently active
  - additional HDO signal plans when available
- Diagnostics support with anonymized config and data-structure summary

## Notes

This integration currently uses ČEZ Distribuce portal endpoints for:

- monthly / control / billing meter readings
- HDO / signal switching times
- optional PND 15-minute chart data

PND support is fully optional:

- without PND, the integration behaves the same as before
- no PND entities are created
- no PND endpoint is called
- no migration of existing config entries is required

When enabled in options:

- `idDeviceSet` must be filled manually
- PND uses the same authenticated session as the main integration
- a PND warm-up request is done before the first PND fetch after startup/relogin
- on PND auth/session problems the integration performs re-login and one retry
- PND errors do not affect the main readings branch, HDO, or main `refresh_health`
- the PND endpoint returns power in `kW`
- energy is calculated from each valid 15-minute point as `kW × 0.25`
- rows with invalid/unknown status are ignored
- the full PND archive is saved only to JSON
- Home Assistant attributes expose only small aggregates, not the full 15-minute dataset
- PND is refreshed on its own interval, recommended `60` minutes or more

## Configuration and options

Initial setup asks for:

- username
- password
- update interval (minutes)
- detailed meter reading history toggle

After setup, options can be changed from the integration UI. Saving options reloads the config entry automatically.

Optional PND settings in options:

- enable/disable PND
- `PND idDeviceSet`
- `PND idAssembly` (default `-1001`)
- `PND update interval` in minutes (minimum `30`, recommended `60` to `180`)

## Health and error visibility

Entities include refresh diagnostics in attributes:

- `refresh_error_type` (`auth`, `network`, `schema`, `portal`, `unknown`, or `null`)
- `refresh_error_detail`
- `refresh_consecutive_failures`
- `refresh_effective_interval_min`
- `refresh_base_interval_min`

The `refresh_health` sensor is intended for dashboards and automations:

- `ok`: updates are healthy
- `warn`: temporary/update issue detected
- `error`: authentication/schema issue or repeated failures

## Diagnostics

The integration provides Home Assistant diagnostics (`diagnostics.py`) with:

- redacted credentials
- current options
- coordinator status
- anonymized data-shape summary (counts and keys, without sensitive payloads)
- compact PND summary without raw 15-minute measurements

## Installation via HACS

1. HACS → Integrations → Custom repositories.
2. Add this repository URL.
3. Category: Integration.
4. Install `ČEZ Distribuce Readings`.
5. Restart Home Assistant.
6. Settings → Devices & services → Add integration → ČEZ Distribuce Readings.

## Manual installation

Copy this folder:

```text
custom_components/cez_distribuce_readings
to
/config/custom_components/cez_distribuce_readings
Then restart Home Assistant.
```
