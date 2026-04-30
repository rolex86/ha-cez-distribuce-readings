"""Binary sensors for ČEZ Distribuce Readings."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import CezDistribuceCoordinator, extract_ean


TIME_RANGE_RE = re.compile(
    r"(?P<sh>\d{1,2}):?(?P<sm>\d{2})\s*[-–]\s*(?P<eh>\d{1,2}):?(?P<em>\d{2})"
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    coordinator: CezDistribuceCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[CezLowTariffBinarySensor] = []

    for point in coordinator.data.get("points", []):
        uid = point.get("uid")
        if not uid:
            continue

        detail = coordinator.data.get("details_by_uid", {}).get(uid, {})
        ean = extract_ean(detail, point)

        entities.append(
            CezLowTariffBinarySensor(
                coordinator=coordinator,
                uid=uid,
                ean=ean,
            )
        )

    async_add_entities(entities)


def _local_tz():
    """Return Home Assistant local timezone."""
    return dt_util.DEFAULT_TIME_ZONE


def _parse_date(value: Any) -> date | None:
    """Parse ČEZ date value.

    ČEZ signal endpoint currently returns dates as DD.MM.YYYY, while some other
    endpoints use ISO-like YYYY-MM-DDTHH:MM:SS strings. Accept both.
    """
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    # ISO datetime/date, for example 2026-04-30T00:00:00 or 2026-04-30.
    try:
        return date.fromisoformat(text.split("T")[0])
    except ValueError:
        pass

    # Czech ČEZ signal date, for example 30.04.2026.
    try:
        return datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError:
        return None


def _parse_time(value: Any) -> time | None:
    """Parse time in HHMM or HH:MM format.

    This helper is for ordinary times only. 24:00 is handled by
    _datetime_from_hm because Python's time type does not allow hour 24.
    """
    if value is None:
        return None

    parsed = _parse_hm(value)
    if parsed is None:
        return None

    hour, minute = parsed

    if hour == 24 and minute == 0:
        return time(0, 0)

    if hour > 23 or minute > 59:
        return None

    return time(hour, minute)


def _parse_hm(value: Any) -> tuple[int, int] | None:
    """Parse HHMM or HH:MM into hour/minute, allowing 24:00."""
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    text = text.replace(":", "")

    if len(text) == 3:
        text = f"0{text}"

    if len(text) != 4 or not text.isdigit():
        return None

    hour = int(text[:2])
    minute = int(text[2:])

    if minute > 59:
        return None

    if hour > 24:
        return None

    if hour == 24 and minute != 0:
        return None

    return hour, minute


def _datetime_from_hm(day: date, hour: int, minute: int) -> datetime:
    """Build local datetime from date/hour/minute, supporting 24:00."""
    if hour == 24 and minute == 0:
        return datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=_local_tz())

    return datetime.combine(day, time(hour, minute), tzinfo=_local_tz())


def _find_date_in_dict(item: dict[str, Any]) -> date | None:
    """Find a date-like field in an item."""
    for key in ("datum", "date", "den", "day"):
        if key in item:
            parsed = _parse_date(item[key])
            if parsed:
                return parsed

    for key, value in item.items():
        key_lower = str(key).lower()
        if "datum" in key_lower or key_lower.endswith("date"):
            parsed = _parse_date(value)
            if parsed:
                return parsed

    return None


def _parse_time_ranges_from_string(day: date, value: str) -> list[tuple[datetime, datetime]]:
    """Parse all time ranges from a text value.

    Example ČEZ value:
    00:00-00:45;   01:45-08:40;   18:10-24:00
    """
    intervals: list[tuple[datetime, datetime]] = []

    for match in TIME_RANGE_RE.finditer(value):
        start_hm = _parse_hm(f"{match.group('sh')}:{match.group('sm')}")
        end_hm = _parse_hm(f"{match.group('eh')}:{match.group('em')}")

        if start_hm is None or end_hm is None:
            continue

        start_dt = _datetime_from_hm(day, start_hm[0], start_hm[1])
        end_dt = _datetime_from_hm(day, end_hm[0], end_hm[1])

        if end_dt <= start_dt:
            end_dt += timedelta(days=1)

        intervals.append((start_dt, end_dt))

    return intervals


def _parse_direct_interval(day: date, item: dict[str, Any]) -> list[tuple[datetime, datetime]]:
    """Parse direct from/to interval from a dict."""
    start_keys = ("od", "from", "start", "zacatek", "casOd", "timeFrom")
    end_keys = ("do", "to", "end", "konec", "casDo", "timeTo")

    lowered = {str(key).lower(): value for key, value in item.items()}

    start_value = None
    end_value = None

    for key in start_keys:
        if key.lower() in lowered:
            start_value = lowered[key.lower()]
            break

    for key in end_keys:
        if key.lower() in lowered:
            end_value = lowered[key.lower()]
            break

    start_hm = _parse_hm(start_value)
    end_hm = _parse_hm(end_value)

    if start_hm is None or end_hm is None:
        return []

    start_dt = _datetime_from_hm(day, start_hm[0], start_hm[1])
    end_dt = _datetime_from_hm(day, end_hm[0], end_hm[1])

    if end_dt <= start_dt:
        end_dt += timedelta(days=1)

    return [(start_dt, end_dt)]


def _extract_intervals(data: Any, inherited_day: date | None = None) -> list[tuple[datetime, datetime]]:
    """Extract signal intervals from a ČEZ response.

    Accepted shapes include:
    - {"signals": [{"datum": "30.04.2026", "casy": "00:00-00:45; ..."}]}
    - envelope-unwrapped data from {"data": {"signals": [...]}}
    - nested lists/dicts with date + intervals
    - string ranges like 01:00-05:00 or 0100-0500
    """
    intervals: list[tuple[datetime, datetime]] = []

    if isinstance(data, list):
        for item in data:
            intervals.extend(_extract_intervals(item, inherited_day))
        return _dedupe_intervals(intervals)

    if isinstance(data, dict):
        day = _find_date_in_dict(data) or inherited_day

        if day:
            intervals.extend(_parse_direct_interval(day, data))

            for key in ("casy", "cas", "times", "intervals", "intervaly"):
                if key in data:
                    intervals.extend(_extract_intervals(data[key], day))

            for value in data.values():
                if isinstance(value, str):
                    intervals.extend(_parse_time_ranges_from_string(day, value))

        for value in data.values():
            if isinstance(value, (dict, list)):
                intervals.extend(_extract_intervals(value, day))

        return _dedupe_intervals(intervals)

    if isinstance(data, str) and inherited_day:
        return _parse_time_ranges_from_string(inherited_day, data)

    return []


def _dedupe_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Remove duplicate intervals."""
    seen: set[tuple[datetime, datetime]] = set()
    result: list[tuple[datetime, datetime]] = []

    for interval in intervals:
        if interval in seen:
            continue
        seen.add(interval)
        result.append(interval)

    return sorted(result, key=lambda row: row[0])


def _active_interval(
    intervals: list[tuple[datetime, datetime]],
    now: datetime,
) -> tuple[datetime, datetime] | None:
    """Return current active interval."""
    for start, end in intervals:
        if start <= now < end:
            return start, end

    return None


def _next_interval(
    intervals: list[tuple[datetime, datetime]],
    now: datetime,
) -> tuple[datetime, datetime] | None:
    """Return next future interval."""
    for start, end in intervals:
        if start > now:
            return start, end

    return None


class CezLowTariffBinarySensor(
    CoordinatorEntity[CezDistribuceCoordinator],
    BinarySensorEntity,
):
    """Binary sensor for low tariff / HDO active state."""

    _attr_has_entity_name = True
    _attr_translation_key = "low_tariff_active"

    def __init__(
        self,
        coordinator: CezDistribuceCoordinator,
        uid: str,
        ean: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._uid = uid
        self._ean = ean

        base_id = ean or uid
        self._attr_unique_id = f"{DOMAIN}_{base_id}_low_tariff_active"

    async def async_added_to_hass(self) -> None:
        """Register minute-based state refresh."""
        await super().async_added_to_hass()

        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._handle_time_update,
                timedelta(minutes=1),
            )
        )

    @callback
    def _handle_time_update(self, now: datetime) -> None:
        """Refresh state from already fetched schedule."""
        self.async_write_ha_state()

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

    def _intervals(self) -> list[tuple[datetime, datetime]]:
        """Return parsed intervals for this supply point."""
        signals = self.coordinator.data.get("signals_by_uid", {}).get(self._uid)
        return _extract_intervals(signals)

    @property
    def is_on(self) -> bool | None:
        """Return true if low tariff is active now."""
        intervals = self._intervals()

        if not intervals:
            return None

        now = dt_util.now()
        return _active_interval(intervals, now) is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return attributes."""
        intervals = self._intervals()
        now = dt_util.now()

        active = _active_interval(intervals, now)
        next_item = _next_interval(intervals, now)

        today = now.date()
        tomorrow = today + timedelta(days=1)

        def format_interval(row: tuple[datetime, datetime]) -> str:
            return f"{row[0].strftime('%H:%M')}-{row[1].strftime('%H:%M')}"

        attrs: dict[str, Any] = {
            "uid": self._uid,
            "ean": self._ean,
            "intervals_loaded": len(intervals),
            "today_intervals": [
                format_interval(row)
                for row in intervals
                if row[0].date() == today
            ],
            "tomorrow_intervals": [
                format_interval(row)
                for row in intervals
                if row[0].date() == tomorrow
            ],
        }

        if active:
            attrs["current_interval_start"] = active[0].isoformat()
            attrs["current_interval_end"] = active[1].isoformat()
            attrs["next_change"] = active[1].isoformat()

        if next_item:
            attrs["next_interval_start"] = next_item[0].isoformat()
            attrs["next_interval_end"] = next_item[1].isoformat()

            if not active:
                attrs["next_change"] = next_item[0].isoformat()

        return attrs
