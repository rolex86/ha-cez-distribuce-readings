# ČEZ Distribuce Readings

Home Assistant custom integration for reading electricity meter data from the ČEZ Distribuce portal.

## Current features

- Login to ČEZ Distribuce portal
- Load supply points
- Load meter reading history
- Create sensors for:
  - VT meter state
  - NT meter state
  - total meter state
  - last reading period VT consumption
  - last reading period NT consumption
  - last reading period total consumption
- Load HDO / low tariff signal schedule
- Create binary sensor:
  - low tariff currently active

## Notes

This integration currently uses ČEZ Distribuce portal endpoints for:

- monthly / control / billing meter readings
- HDO / signal switching times

It does not yet use PND interval data. PND support can be added later when PND access is available.

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
