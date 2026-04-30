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
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import CezDistribuceCoordinator, extract_ean


@dataclass(frozen=True, kw_only=True)
class CezSensorDescription(SensorEntityDescription):
    """ČEZ sensor description."""

    kind: str


SENSORS: tuple[CezSensorDescription, ...] = (
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
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    CezSensorDescription(
        key="last_period_nt",
        translation_key="last_period_nt",
        kind="last_period_nt",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    CezSensorDescription(
        key="last_period_total",
        translation_key="last_period_total",
        kind="last_period_total",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
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

        for description in SENSORS:
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

    @property
    def native_value(self) -> float | None:
        """Return sensor state."""
        readings = self.coordinator.data.get("readings_by_uid", {}).get(self._uid, [])
        latest = _latest_reading(readings)

        if latest is None:
            return None

        kind = self.entity_description.kind

        if kind == "state_vt":
            return _round_decimal(_vt(latest))

        if kind == "state_nt":
            return _round_decimal(_nt(latest))

        if kind == "state_total":
            return _round_decimal(_vt(latest) + _nt(latest))

        previous = _previous_same_meter(readings, latest)

        if previous is None:
            return None

        if kind == "last_period_vt":
            return _round_decimal(_vt(latest) - _vt(previous))

        if kind == "last_period_nt":
            return _round_decimal(_nt(latest) - _nt(previous))

        if kind == "last_period_total":
            latest_total = _vt(latest) + _nt(latest)
            previous_total = _vt(previous) + _nt(previous)
            return _round_decimal(latest_total - previous_total)

        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return attributes."""
        readings = self.coordinator.data.get("readings_by_uid", {}).get(self._uid, [])
        latest = _latest_reading(readings)

        if latest is None:
            return {
                "uid": self._uid,
                "ean": self._ean,
            }

        previous = _previous_same_meter(readings, latest)

        attrs: dict[str, Any] = {
            "uid": self._uid,
            "ean": self._ean,
            "meter_serial": latest.get("sernr"),
            "last_reading_date": latest.get("datumOdectu"),
            "last_reading_time": latest.get("casOdectu"),
            "last_reading_reason": latest.get("duvodOdectuText"),
            "last_reading_source": latest.get("istablartText"),
            "last_reading_status": latest.get("statusText"),
            "vt_unit": latest.get("vtUnitRead"),
            "nt_unit": latest.get("ntUnitRead"),
        }

        if previous is not None:
            attrs.update(
                {
                    "previous_reading_date": previous.get("datumOdectu"),
                    "previous_reading_time": previous.get("casOdectu"),
                    "previous_meter_serial": previous.get("sernr"),
                }
            )

        return attrs