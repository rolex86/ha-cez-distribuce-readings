"""Sensors for ČEZ Distribuce Readings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import CezDistribuceCoordinator, extract_ean


@dataclass(frozen=True, kw_only=True)
class CezSensorDescription(SensorEntityDescription):
    """ČEZ sensor description."""

    kind: str


BASE_SENSORS: tuple[CezSensorDescription, ...] = (
    CezSensorDescription(
        key="state_vt",
        translation_key="state_vt",
        kind="state_vt",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    CezSensorDescription(
        key="state_nt",
        translation_key="state_nt",
        kind="state_nt",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    CezSensorDescription(
        key="state_total",
        translation_key="state_total",
        kind="state_total",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    CezSensorDescription(
        key="last_period_vt",
        translation_key="last_period_vt",
        kind="last_period_vt",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    CezSensorDescription(
        key="last_period_nt",
        translation_key="last_period_nt",
        kind="last_period_nt",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    CezSensorDescription(
        key="last_period_total",
        translation_key="last_period_total",
        kind="last_period_total",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    CezSensorDescription(
        key="last_period_days",
        translation_key="last_period_days",
        kind="last_period_days",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="d",
    ),
    CezSensorDescription(
        key="last_period_avg_daily",
        translation_key="last_period_avg_daily",
        kind="last_period_avg_daily",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="kWh/d",
    ),
    CezSensorDescription(
        key="last_period_nt_share",
        translation_key="last_period_nt_share",
        kind="last_period_nt_share",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
    ),
    CezSensorDescription(
        key="last_period_vt_share",
        translation_key="last_period_vt_share",
        kind="last_period_vt_share",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
    ),
    CezSensorDescription(
        key="archive_readings_count",
        translation_key="archive_readings_count",
        kind="archive_readings_count",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    CezSensorDescription(
        key="archive_periods_count",
        translation_key="archive_periods_count",
        kind="archive_periods_count",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    CezSensorDescription(
        key="refresh_health",
        translation_key="refresh_health",
        kind="refresh_health",
    ),
)

PND_SENSORS: tuple[CezSensorDescription, ...] = (
    CezSensorDescription(
        key="pnd_spotreba_obdobi",
        translation_key="pnd_spotreba_obdobi",
        kind="pnd_spotreba_obdobi",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    CezSensorDescription(
        key="pnd_prumer_den",
        translation_key="pnd_prumer_den",
        kind="pnd_prumer_den",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="kWh/d",
    ),
    CezSensorDescription(
        key="pnd_max_kw",
        translation_key="pnd_max_kw",
        kind="pnd_max_kw",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
    ),
    CezSensorDescription(
        key="pnd_posledni_kw",
        translation_key="pnd_posledni_kw",
        kind="pnd_posledni_kw",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
    ),
    CezSensorDescription(
        key="pnd_posledni_mereni",
        translation_key="pnd_posledni_mereni",
        kind="pnd_posledni_mereni",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    CezSensorDescription(
        key="pnd_health",
        translation_key="pnd_health",
        kind="pnd_health",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors."""
    coordinator: CezDistribuceCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[CezReadingSensor] = []

    for point in coordinator.data.get("points", []):
        uid = point.get("uid")
        if not uid:
            continue

        detail = coordinator.data.get("details_by_uid", {}).get(uid, {})
        ean = extract_ean(detail, point)

        for description in BASE_SENSORS:
            entities.append(
                CezReadingSensor(
                    coordinator=coordinator,
                    uid=uid,
                    ean=ean,
                    description=description,
                )
            )

        if coordinator.pnd_configured and coordinator.is_pnd_target(uid, ean):
            for description in PND_SENSORS:
                entities.append(
                    CezReadingSensor(
                        coordinator=coordinator,
                        uid=uid,
                        ean=ean,
                        description=description,
                    )
                )

    async_add_entities(entities)


def _parse_decimal(value: Any) -> Decimal | None:
    """Parse ČEZ decimal string."""
    if value is None:
        return None

    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _reading_datetime(item: dict[str, Any]) -> datetime:
    """Build datetime from ČEZ datumOdectu + casOdectu."""
    date_part = str(item.get("datumOdectu", "")).split("T")[0] or "1970-01-01"
    time_part = str(item.get("casOdectu") or "0000").zfill(4)

    return datetime.fromisoformat(
        f"{date_part}T{time_part[:2]}:{time_part[2:]}:00"
    )


def _valid_readings(readings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return valid readings sorted newest first."""
    valid = [
        item
        for item in readings
        if item.get("statusText") in (None, "Validní")
    ]

    return sorted(valid, key=_reading_datetime, reverse=True)


def _latest_reading(readings: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return newest valid reading."""
    valid = _valid_readings(readings)
    return valid[0] if valid else None


def _previous_same_meter(
    readings: list[dict[str, Any]],
    latest: dict[str, Any],
) -> dict[str, Any] | None:
    """Return previous valid reading with the same meter serial."""
    latest_dt = _reading_datetime(latest)
    latest_serial = latest.get("sernr")

    candidates = [
        item
        for item in _valid_readings(readings)
        if item.get("sernr") == latest_serial
        and _reading_datetime(item) < latest_dt
    ]

    return candidates[0] if candidates else None


def _vt(item: dict[str, Any]) -> Decimal:
    return _parse_decimal(item.get("stavVt")) or Decimal("0")


def _nt(item: dict[str, Any]) -> Decimal:
    return _parse_decimal(item.get("stavNt")) or Decimal("0")


def _round_decimal(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(round(value, 3))


def _parse_sensor_datetime(value: Any) -> datetime | None:
    """Parse archive datetime into a HA-friendly timezone-aware datetime."""
    if value is None:
        return None

    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)

    return parsed


class CezReadingSensor(CoordinatorEntity[CezDistribuceCoordinator], SensorEntity):
    """ČEZ reading sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CezDistribuceCoordinator,
        uid: str,
        ean: str | None,
        description: CezSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._uid = uid
        self._ean = ean

        base_id = ean or uid
        self._attr_unique_id = f"{DOMAIN}_{base_id}_{description.key}"

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info."""
        name_suffix = self._ean or self._uid[-8:]

        return {
            "identifiers": {(DOMAIN, self._uid)},
            "name": f"ČEZ odběrné místo {name_suffix}",
            "manufacturer": "ČEZ Distribuce",
            "model": "Elektroměr",
        }

    def _archive(self) -> dict[str, Any]:
        """Return normalized archive for this supply point."""
        return self.coordinator.data.get("archives_by_uid", {}).get(self._uid, {})

    def _latest_period(self) -> dict[str, Any] | None:
        """Return latest calculated period."""
        latest_period = self._archive().get("latest_period")
        return latest_period if isinstance(latest_period, dict) else None

    def _pnd_archive(self) -> dict[str, Any]:
        """Return compact PND archive for this supply point."""
        return self.coordinator.data.get("pnd_archives_by_uid", {}).get(self._uid, {})

    def _pnd_status(self) -> dict[str, Any]:
        """Return per-UID PND status."""
        return self.coordinator.data.get("pnd_status_by_uid", {}).get(self._uid, {})

    def _last_valid_pnd(self) -> dict[str, Any] | None:
        """Return latest valid PND measurement."""
        last_valid = self._pnd_archive().get("last_valid")
        return last_valid if isinstance(last_valid, dict) else None

    @property
    def native_value(self) -> float | int | str | datetime | None:
        """Return sensor state."""
        readings = self.coordinator.data.get("readings_by_uid", {}).get(self._uid, [])
        latest = _latest_reading(readings)
        refresh_status = self.coordinator.refresh_status_attributes()
        error_type = refresh_status.get("refresh_error_type")
        consecutive_failures = int(refresh_status.get("refresh_consecutive_failures") or 0)

        kind = self.entity_description.kind

        if kind == "refresh_health":
            if error_type in ("auth", "schema"):
                return "error"
            if error_type and consecutive_failures >= 3:
                return "error"
            if error_type:
                return "warn"
            return "ok" if self.coordinator.last_update_success else "warn"

        if kind in (
            "last_period_vt",
            "last_period_nt",
            "last_period_total",
            "last_period_days",
            "last_period_avg_daily",
            "last_period_nt_share",
            "last_period_vt_share",
            "archive_readings_count",
            "archive_periods_count",
        ):
            archive = self._archive()
            latest_period = self._latest_period()

            if kind == "archive_readings_count":
                return archive.get("readings_count")

            if kind == "archive_periods_count":
                return archive.get("periods_count")

            if latest_period is None:
                return None

            if kind == "last_period_vt":
                return latest_period.get("vt_kwh")

            if kind == "last_period_nt":
                return latest_period.get("nt_kwh")

            if kind == "last_period_total":
                return latest_period.get("total_kwh")

            if kind == "last_period_days":
                return latest_period.get("days")

            if kind == "last_period_avg_daily":
                return latest_period.get("avg_kwh_day")

            if kind == "last_period_nt_share":
                return latest_period.get("nt_share_percent")

            if kind == "last_period_vt_share":
                return latest_period.get("vt_share_percent")

        if kind == "pnd_spotreba_obdobi":
            return self._pnd_archive().get("total_kwh")

        if kind == "pnd_prumer_den":
            return self._pnd_archive().get("avg_kwh_day")

        if kind == "pnd_max_kw":
            return self._pnd_archive().get("max_kw")

        if kind == "pnd_posledni_kw":
            last_valid = self._last_valid_pnd()
            return last_valid.get("kw") if last_valid else None

        if kind == "pnd_posledni_mereni":
            last_valid = self._last_valid_pnd()
            return _parse_sensor_datetime(last_valid.get("end_time")) if last_valid else None

        if kind == "pnd_health":
            status = self._pnd_status()
            if not status.get("configured"):
                return "disabled"
            if status.get("ok") is False and status.get("using_cached_data"):
                return "cached"
            if status.get("ok") is False:
                return "error"
            if status.get("using_cached_data"):
                return "cached"
            if status.get("ok") is True:
                return "ok"
            return "disabled"

        if latest is None:
            return None

        if kind == "state_vt":
            return _round_decimal(_vt(latest))

        if kind == "state_nt":
            return _round_decimal(_nt(latest))

        if kind == "state_total":
            return _round_decimal(_vt(latest) + _nt(latest))

        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return attributes."""
        kind = self.entity_description.kind

        if kind.startswith("pnd_"):
            archive = self._pnd_archive()
            status = self._pnd_status()
            attrs: dict[str, Any] = {
                "uid": self._uid,
                "ean": self._ean,
                "pnd_enabled": status.get("enabled"),
                "pnd_configured": status.get("configured"),
                "pnd_ok": status.get("ok"),
                "pnd_error_type": status.get("error_type"),
                "pnd_error_detail": status.get("error_detail"),
                "pnd_using_cached_data": status.get("using_cached_data"),
                "pnd_skipped_reason": status.get("skipped_reason"),
                "pnd_unit_y": archive.get("unit_y"),
                "pnd_interval_from": archive.get("interval_from"),
                "pnd_interval_to": archive.get("interval_to"),
                "pnd_measurements_count": archive.get("measurements_count"),
                "pnd_json_path": archive.get("json_path"),
            }

            if kind == "pnd_spotreba_obdobi" and isinstance(archive.get("daily_totals"), list):
                attrs["daily_totals"] = archive.get("daily_totals")

            return attrs

        readings = self.coordinator.data.get("readings_by_uid", {}).get(self._uid, [])
        latest = _latest_reading(readings)
        archive = self._archive()
        latest_period = self._latest_period()

        attrs: dict[str, Any] = {
            "uid": self._uid,
            "ean": self._ean,
        }
        attrs.update(self.coordinator.refresh_status_attributes())

        if latest is not None:
            previous = _previous_same_meter(readings, latest)

            attrs.update(
                {
                    "meter_serial": latest.get("sernr"),
                    "last_reading_date": latest.get("datumOdectu"),
                    "last_reading_time": latest.get("casOdectu"),
                    "last_reading_reason": latest.get("duvodOdectuText"),
                    "last_reading_source": latest.get("istablartText"),
                    "last_reading_status": latest.get("statusText"),
                    "vt_unit": latest.get("vtUnitRead"),
                    "nt_unit": latest.get("ntUnitRead"),
                }
            )

            if previous is not None:
                attrs.update(
                    {
                        "previous_reading_date": previous.get("datumOdectu"),
                        "previous_reading_time": previous.get("casOdectu"),
                        "previous_meter_serial": previous.get("sernr"),
                    }
                )

        if latest_period is not None:
            attrs["latest_period"] = latest_period

        if kind in ("archive_readings_count", "archive_periods_count"):
            periods = archive.get("periods", [])
            if isinstance(periods, list):
                attrs["last_12_periods"] = list(reversed(periods[-12:]))

            if archive.get("json_path"):
                attrs["json_path"] = archive.get("json_path")

            if archive.get("csv_path"):
                attrs["csv_path"] = archive.get("csv_path")

        return attrs
