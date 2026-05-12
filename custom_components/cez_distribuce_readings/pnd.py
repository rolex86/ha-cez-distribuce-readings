"""Helpers for processing ČEZ PND 15-minute data."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .const import PND_INTERVAL_HOURS

_VALID_STATUS = "naměřená data OK"


def format_pnd_datetime(value: datetime) -> str:
    """Format datetime for the ČEZ PND endpoint."""
    return value.strftime("%d.%m.%Y %H:%M")


def current_month_interval(now: datetime) -> tuple[datetime, datetime]:
    """Return the current month interval [month start, next month start)."""
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)

    return start, end


def parse_pnd_datetime(value: str) -> datetime:
    """Parse ČEZ PND datetime string."""
    date_part, time_part = str(value).strip().split(" ")

    if time_part == "24:00":
        day = datetime.strptime(date_part, "%d.%m.%Y")
        return day + timedelta(days=1)

    return datetime.strptime(str(value).strip(), "%d.%m.%Y %H:%M")


def _safe_filename(value: str) -> str:
    """Return a filesystem-safe file key."""
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_") or "unknown"


def _round_decimal(value: Decimal | None, places: int = 3) -> float | None:
    """Convert Decimal to a rounded float."""
    if value is None:
        return None

    return float(round(value, places))


def _parse_decimal(value: Any) -> Decimal | None:
    """Parse a decimal value."""
    if value is None:
        return None

    try:
        return Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _parse_pnd_timestamp(value: Any) -> datetime:
    """Parse a PND timestamp from string, epoch seconds, or epoch milliseconds."""
    if isinstance(value, datetime):
        return value

    if isinstance(value, (int, float)):
        timestamp = float(value)
        if abs(timestamp) >= 1_000_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp)

    text = str(value).strip()
    if not text:
        raise ValueError("Empty PND timestamp")

    if text.isdigit():
        return _parse_pnd_timestamp(int(text))

    try:
        return parse_pnd_datetime(text)
    except ValueError:
        pass

    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _extract_unit_y(data: dict[str, Any]) -> str | None:
    """Extract the PND Y-axis unit from known payload shapes."""
    direct_value = data.get("unitY")
    if isinstance(direct_value, str) and direct_value.strip():
        return direct_value.strip()

    y_axis = data.get("yAxis")
    if isinstance(y_axis, dict):
        title = y_axis.get("title")
        if isinstance(title, dict):
            value = title.get("text")
            if isinstance(value, str) and value.strip():
                return value.strip()

    stats = data.get("stats")
    if isinstance(stats, dict):
        for key in ("unitY", "unit", "yUnit"):
            value = stats.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return None


def extract_pnd_measurements(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract valid 15-minute measurements from a PND chart payload."""
    series = data.get("series")
    if not isinstance(series, list):
        raise ValueError("PND data response has unexpected shape: missing series")

    interval_duration = timedelta(hours=PND_INTERVAL_HOURS)
    measurements: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str | None]] = set()

    for series_item in series:
        if not isinstance(series_item, dict):
            continue

        series_name = str(series_item.get("name")).strip() if series_item.get("name") is not None else None
        data_points = series_item.get("data")
        if not isinstance(data_points, list):
            continue

        for point in data_points:
            if not isinstance(point, (list, tuple)) or len(point) < 3:
                continue

            status = str(point[2]).strip() if point[2] is not None else ""
            if status != _VALID_STATUS:
                continue

            try:
                end_time = _parse_pnd_timestamp(point[0])
                kw = _parse_decimal(point[1])
            except (TypeError, ValueError):
                continue

            if kw is None:
                continue

            start_time = end_time - interval_duration
            kwh = kw * Decimal(str(PND_INTERVAL_HOURS))
            signature = (
                start_time.isoformat(),
                end_time.isoformat(),
                str(kw),
                series_name,
            )
            if signature in seen:
                continue
            seen.add(signature)

            measurements.append(
                {
                    "start_time": start_time.isoformat(),
                    "end_time": end_time.isoformat(),
                    "kw": _round_decimal(kw, 6),
                    "kwh": _round_decimal(kwh, 6),
                    "status": status,
                    "series": series_name,
                }
            )

    return sorted(measurements, key=lambda item: item["end_time"])


def build_pnd_archive(
    data: dict[str, Any],
    interval_from: str,
    interval_to: str,
) -> dict[str, Any]:
    """Build a compact PND archive from chart data."""
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected PND chart payload type: {type(data).__name__}")

    measurements = extract_pnd_measurements(data)
    unit_y = _extract_unit_y(data) or "kW"

    total_kwh_decimal = Decimal("0")
    max_kw_decimal: Decimal | None = None
    daily_totals_decimal: dict[str, Decimal] = {}

    for item in measurements:
        start_time = datetime.fromisoformat(item["start_time"])
        kw = _parse_decimal(item["kw"]) or Decimal("0")
        kwh = _parse_decimal(item["kwh"]) or Decimal("0")

        total_kwh_decimal += kwh
        if max_kw_decimal is None or kw > max_kw_decimal:
            max_kw_decimal = kw

        day_key = start_time.date().isoformat()
        daily_totals_decimal[day_key] = daily_totals_decimal.get(day_key, Decimal("0")) + kwh

    total_kwh = _round_decimal(total_kwh_decimal) or 0.0
    daily_totals = [
        {
            "date": day_key,
            "kwh": _round_decimal(kwh_total) or 0.0,
        }
        for day_key, kwh_total in sorted(daily_totals_decimal.items())
    ]
    avg_kwh_day = (
        round(total_kwh / len(daily_totals), 3) if daily_totals else None
    )
    last_valid = measurements[-1] if measurements else None

    return {
        "unit_y": unit_y,
        "interval_from": interval_from,
        "interval_to": interval_to,
        "measurements_count": len(measurements),
        "total_kwh": total_kwh,
        "avg_kwh_day": avg_kwh_day,
        "max_kw": _round_decimal(max_kw_decimal),
        "last_valid": last_valid,
        "daily_totals": daily_totals,
        "measurements": measurements,
    }


def save_pnd_archive(
    archive: dict[str, Any],
    archive_dir: Path,
    key: str,
) -> dict[str, str]:
    """Save full PND archive to a JSON file."""
    archive_dir.mkdir(parents=True, exist_ok=True)

    json_path = archive_dir / f"pnd_{_safe_filename(key)}.json"
    json_path.write_text(
        json.dumps(archive, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {"json_path": str(json_path)}


def external_pnd_export_path(archive_dir: Path, device_set_id: str) -> Path:
    """Return the expected companion add-on export path for one device set."""
    return archive_dir / f"pnd_export_{_safe_filename(str(device_set_id))}.json"


def load_external_pnd_export(
    archive_dir: Path,
    device_set_id: str | int,
) -> dict[str, Any] | None:
    """Load raw PND export written by the companion add-on, if it exists."""
    json_path = external_pnd_export_path(archive_dir, str(device_set_id))

    if not json_path.exists():
        return None

    try:
        raw_text = json_path.read_text(encoding="utf-8-sig")
    except OSError as err:
        raise ValueError(f"External PND export cannot be read: {json_path}: {err}") from err

    if not raw_text.strip():
        raise ValueError(f"External PND export is empty: {json_path}")

    try:
        data = json.loads(raw_text)
    except (ValueError, TypeError) as err:
        raise ValueError(f"External PND export is not valid JSON: {json_path}: {err}") from err

    if not isinstance(data, dict):
        raise ValueError(
            f"External PND export root must be a JSON object: {json_path} "
            f"(got {type(data).__name__})"
        )

    data.setdefault("export_path", str(json_path))
    return data


def load_pnd_archive(archive_dir: Path, key: str) -> dict[str, Any] | None:
    """Load full PND archive from a JSON file, if it exists."""
    json_path = archive_dir / f"pnd_{_safe_filename(key)}.json"

    if not json_path.exists():
        return None

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    if "json_path" not in data:
        data["json_path"] = str(json_path)

    return data
