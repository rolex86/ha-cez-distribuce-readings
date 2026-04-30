"""Binary sensors for ČEZ Distribuce Readings."""

from __future__ import annotations

import re
from dataclasses import dataclass
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


@dataclass(frozen=True)
class SignalPlan:
    """Parsed ČEZ HDO/signal plan."""

    signal_id: str
    rank: int
    is_low_tariff: bool
    intervals: tuple[tuple[datetime, datetime], ...]
    average_daily_hours: float | None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    coordinator: CezDistribuceCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[CezSignalBinarySensor] = []

    for point in coordinator.data.get("points", []):
        uid = point.get("uid")
        if not uid:
            continue

        detail = coordinator.data.get("details_by_uid", {}).get(uid, {})
        ean = extract_ean(detail, point)
        signals = coordinator.data.get("signals_by_uid", {}).get(uid)
        plans = _build_signal_plans(signals)

        if plans:
            for plan in plans:
                entities.append(
                    CezSignalBinarySensor(
                        coordinator=coordinator,
                        uid=uid,
                        ean=ean,
                        signal_id=plan.signal_id,
                        rank=plan.rank,
                        is_low_tariff=plan.is_low_tariff,
                    )
                )
        else:
            # Keep the original low tariff entity available even when ČEZ returns
            # no parsable HDO/signals yet. It will show as unknown with helpful
            # attributes instead of disappearing.
            entities.append(
                CezSignalBinarySensor(
                    coordinator=coordinator,
                    uid=uid,
                    ean=ean,
                    signal_id=None,
                    rank=1,
                    is_low_tariff=True,
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
    """Extract signal intervals from a ČEZ response or a single signal item."""
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


def _signal_items(data: Any) -> list[dict[str, Any]]:
    """Return ČEZ signal item dictionaries from endpoint data."""
    if isinstance(data, dict) and isinstance(data.get("signals"), list):
        return [item for item in data["signals"] if isinstance(item, dict)]

    return []


def _average_daily_hours(intervals: list[tuple[datetime, datetime]]) -> float | None:
    """Return average duration per loaded day."""
    if not intervals:
        return None

    seconds_by_day: dict[date, float] = {}

    for start, end in intervals:
        seconds_by_day[start.date()] = seconds_by_day.get(start.date(), 0.0) + (
            end - start
        ).total_seconds()

    if not seconds_by_day:
        return None

    return round(sum(seconds_by_day.values()) / len(seconds_by_day) / 3600, 2)


def _build_signal_plans(data: Any) -> list[SignalPlan]:
    """Build separate signal plans from ČEZ HDO response.

    The endpoint can return multiple signal channels for one supply point. For
    example, one channel can represent the real low tariff schedule and another
    channel can represent a boiler/controlled-load permission schedule. Mixing
    all channels would incorrectly make the low-tariff binary sensor active for
    more time than it should be.
    """
    items = _signal_items(data)
    grouped: dict[str, list[dict[str, Any]]] = {}

    if items:
        for item in items:
            signal_id = str(item.get("signal") or "unknown")
            grouped.setdefault(signal_id, []).append(item)
    else:
        fallback_intervals = _extract_intervals(data)
        if not fallback_intervals:
            return []
        grouped["default"] = []

    raw_plans: list[tuple[str, list[tuple[datetime, datetime]], float | None]] = []

    if grouped == {"default": []}:
        intervals = _extract_intervals(data)
        raw_plans.append(("default", intervals, _average_daily_hours(intervals)))
    else:
        for signal_id, signal_items_for_id in grouped.items():
            intervals: list[tuple[datetime, datetime]] = []
            for item in signal_items_for_id:
                intervals.extend(_extract_intervals(item))
            intervals = _dedupe_intervals(intervals)
            raw_plans.append((signal_id, intervals, _average_daily_hours(intervals)))

    raw_plans = [row for row in raw_plans if row[1]]
    raw_plans.sort(
        key=lambda row: (
            row[2] if row[2] is not None else 0,
            len(row[1]),
            row[0],
        ),
        reverse=True,
    )

    return [
        SignalPlan(
            signal_id=signal_id,
            rank=index + 1,
            is_low_tariff=index == 0,
            intervals=tuple(intervals),
            average_daily_hours=average_hours,
        )
        for index, (signal_id, intervals, average_hours) in enumerate(raw_plans)
    ]


def _signal_key(signal_id: str | None, rank: int, is_low_tariff: bool) -> str:
    """Return stable unique key suffix for a signal plan."""
    if is_low_tariff:
        return "low_tariff_active"

    source = signal_id or f"signal_{rank}"
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", source).strip("_").lower()
    return f"hdo_signal_{rank}_{slug[:80]}_active"


def _active_interval(
    intervals: list[tuple[datetime, datetime]] | tuple[tuple[datetime, datetime], ...],
    now: datetime,
) -> tuple[datetime, datetime] | None:
    """Return current active interval."""
    for start, end in intervals:
        if start <= now < end:
            return start, end

    return None


def _next_interval(
    intervals: list[tuple[datetime, datetime]] | tuple[tuple[datetime, datetime], ...],
    now: datetime,
) -> tuple[datetime, datetime] | None:
    """Return next future interval."""
    for start, end in intervals:
        if start > now:
            return start, end

    return None


def _duration_hours_for_day(
    intervals: tuple[tuple[datetime, datetime], ...],
    target_day: date,
) -> float:
    """Return total interval duration for a given date based on interval start."""
    seconds = sum(
        (end - start).total_seconds()
        for start, end in intervals
        if start.date() == target_day
    )
    return round(seconds / 3600, 2)


class CezSignalBinarySensor(
    CoordinatorEntity[CezDistribuceCoordinator],
    BinarySensorEntity,
):
    """Binary sensor for one ČEZ low-tariff/HDO signal plan."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CezDistribuceCoordinator,
        uid: str,
        ean: str | None,
        signal_id: str | None,
        rank: int,
        is_low_tariff: bool,
    ) -> None:
        super().__init__(coordinator)
        self._uid = uid
        self._ean = ean
        self._signal_id = signal_id
        self._rank = rank
        self._is_low_tariff = is_low_tariff

        base_id = ean or uid
        signal_key = _signal_key(signal_id, rank, is_low_tariff)
        self._attr_unique_id = f"{DOMAIN}_{base_id}_{signal_key}"

        if is_low_tariff:
            self._attr_name = "Nízký tarif aktivní"
        else:
            self._attr_name = f"HDO signál {rank} aktivní"

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

    def _all_plans(self) -> list[SignalPlan]:
        """Return all parsed signal plans for this supply point."""
        signals = self.coordinator.data.get("signals_by_uid", {}).get(self._uid)
        return _build_signal_plans(signals)

    def _plan(self) -> SignalPlan | None:
        """Return this entity's current signal plan."""
        plans = self._all_plans()

        if self._is_low_tariff:
            for plan in plans:
                if plan.is_low_tariff:
                    return plan
            return None

        for plan in plans:
            if plan.rank == self._rank and plan.signal_id == self._signal_id:
                return plan

        for plan in plans:
            if plan.rank == self._rank:
                return plan

        return None

    @property
    def is_on(self) -> bool | None:
        """Return true if this ČEZ signal is active now."""
        plan = self._plan()

        if plan is None or not plan.intervals:
            return None

        now = dt_util.now()
        return _active_interval(plan.intervals, now) is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return attributes."""
        plan = self._plan()
        all_plans = self._all_plans()
        now = dt_util.now()
        today = now.date()
        tomorrow = today + timedelta(days=1)

        attrs: dict[str, Any] = {
            "uid": self._uid,
            "ean": self._ean,
            "signal_id": self._signal_id,
            "signal_rank": self._rank,
            "is_low_tariff_plan": self._is_low_tariff,
            "available_signals": [plan.signal_id for plan in all_plans],
            "available_signal_daily_hours": {
                plan.signal_id: plan.average_daily_hours for plan in all_plans
            },
        }

        if plan is None:
            attrs.update(
                {
                    "intervals_loaded": 0,
                    "today_intervals": [],
                    "tomorrow_intervals": [],
                }
            )
            return attrs

        active = _active_interval(plan.intervals, now)
        next_item = _next_interval(plan.intervals, now)

        def format_interval(row: tuple[datetime, datetime]) -> str:
            return f"{row[0].strftime('%H:%M')}-{row[1].strftime('%H:%M')}"

        attrs.update(
            {
                "signal_id": plan.signal_id,
                "signal_rank": plan.rank,
                "is_low_tariff_plan": plan.is_low_tariff,
                "average_daily_hours": plan.average_daily_hours,
                "intervals_loaded": len(plan.intervals),
                "today_duration_hours": _duration_hours_for_day(plan.intervals, today),
                "tomorrow_duration_hours": _duration_hours_for_day(plan.intervals, tomorrow),
                "today_intervals": [
                    format_interval(row)
                    for row in plan.intervals
                    if row[0].date() == today
                ],
                "tomorrow_intervals": [
                    format_interval(row)
                    for row in plan.intervals
                    if row[0].date() == tomorrow
                ],
            }
        )

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
