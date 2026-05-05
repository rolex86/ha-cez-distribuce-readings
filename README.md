# ČEZ Distribuce Readings

Home Assistant custom integration for reading electricity meter data from the ČEZ Distribuce portal.

## Current features

- Login to ČEZ Distribuce portal
- Reauthentication flow when credentials expire or change
- Load supply points
- Load meter reading history
- Adaptive retries, relogin, and refresh backoff on repeated failures
- Options flow (change update interval and detailed history without removing integration)
- Create sensors for:
  - VT meter state
  - NT meter state
  - total meter state
  - last reading period VT consumption
  - last reading period NT consumption
  - last reading period total consumption
  - archive readings and period counts
  - refresh health (`ok` / `warn` / `error`)
- Load HDO / low tariff signal schedule
- Create binary sensors:
  - low tariff currently active
  - additional HDO signal plans when available
- Diagnostics support with anonymized config and data-structure summary

## Notes

This integration currently uses ČEZ Distribuce portal endpoints for:

- monthly / control / billing meter readings
- HDO / signal switching times

It does not yet use PND interval data. PND support can be added later when PND access is available.

## Configuration and options

Initial setup asks for:

- username
- password
- update interval (minutes)
- detailed meter reading history toggle

After setup, options can be changed from the integration UI. Saving options reloads the config entry automatically.

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
