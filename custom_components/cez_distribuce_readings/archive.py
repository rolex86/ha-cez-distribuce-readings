"""Reading archive helpers for ČEZ Distribuce Readings."""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


def _parse_decimal(value: Any) -> Decimal | None:
    """Parse ČEZ decimal string."""
    if value is None:
        return None

    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _round_decimal(value: Decimal | None, places: int = 3) -> float | None:
    """Convert Decimal to rounded float."""
    if value is None:
        return None

    return float(round(value, places))


def _reading_datetime(item: dict[str, Any]) -> datetime:
    """Build datetime from ČEZ datumOdectu + casOdectu."""
    date_part = str(item.get("datumOdectu", "")).split("T")[0] or "1970-01-01"
    time_part = str(item.get("casOdectu") or "0000").zfill(4)

    return datetime.fromisoformat(
        f"{date_part}T{time_part[:2]}:{time_part[2:]}:00"
    )


def _valid_readings(readings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return valid readings sorted oldest first."""
    valid = [
        item
        for item in readings
        if item.get("statusText") in (None, "Validní")
    ]

    return sorted(valid, key=_reading_datetime)


def _safe_filename(value: str) -> str:
    """Return safe filename part."""
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_") or "unknown"


def build_archive(readings: list[dict[str, Any]]) -> dict[str, Any]:
    """Build normalized reading + period archive.

    Periods are calculated only within the same meter serial number. This avoids
    false negative/huge deltas when ČEZ replaces the physical meter.
    """
    valid_raw = _valid_readings(readings)
    normalized_readings: list[dict[str, Any]] = []

    for item in valid_raw:
        vt = _parse_decimal(item.get("stavVt")) or Decimal("0")
        nt = _parse_decimal(item.get("stavNt")) or Decimal("0")
        total = vt + nt
        read_at = _reading_datetime(item)

        normalized_readings.append(
            {
                "read_at": read_at.isoformat(),
                "read_date": read_at.date().isoformat(),
                "read_time": read_at.strftime("%H:%M"),
                "meter_serial": item.get("sernr"),
                "vt_kwh": _round_decimal(vt),
                "nt_kwh": _round_decimal(nt),
                "total_kwh": _round_decimal(total),
                "vt_unit": item.get("vtUnitRead"),
                "nt_unit": item.get("ntUnitRead"),
                "reason": item.get("duvodOdectuText"),
                "source": item.get("istablartText"),
                "status": item.get("statusText"),
            }
        )

    periods: list[dict[str, Any]] = []
    previous_raw: dict[str, Any] | None = None
    previous_norm: dict[str, Any] | None = None

    for current_raw, current_norm in zip(valid_raw, normalized_readings, strict=False):
        if previous_raw is None or previous_norm is None:
            previous_raw = current_raw
            previous_norm = current_norm
            continue

        if current_raw.get("sernr") != previous_raw.get("sernr"):
            previous_raw = current_raw
            previous_norm = current_norm
            continue

        previous_dt = _reading_datetime(previous_raw)
        current_dt = _reading_datetime(current_raw)

        days = (current_dt.date() - previous_dt.date()).days
        if days <= 0:
            previous_raw = current_raw
            previous_norm = current_norm
            continue

        previous_vt = _parse_decimal(previous_raw.get("stavVt")) or Decimal("0")
        previous_nt = _parse_decimal(previous_raw.get("stavNt")) or Decimal("0")
        current_vt = _parse_decimal(current_raw.get("stavVt")) or Decimal("0")
        current_nt = _parse_decimal(current_raw.get("stavNt")) or Decimal("0")

        vt_delta = current_vt - previous_vt
        nt_delta = current_nt - previous_nt
        total_delta = vt_delta + nt_delta

        if vt_delta < 0 or nt_delta < 0 or total_delta < 0:
            previous_raw = current_raw
            previous_norm = current_norm
            continue

        total_float = _round_decimal(total_delta) or 0
        vt_float = _round_decimal(vt_delta) or 0
        nt_float = _round_decimal(nt_delta) or 0

        nt_share = round((nt_float / total_float) * 100, 2) if total_float else None
        vt_share = round((vt_float / total_float) * 100, 2) if total_float else None
        avg_daily = round(total_float / days, 3) if days else None

        periods.append(
            {
                "from": previous_norm["read_at"],
                "to": current_norm["read_at"],
                "from_date": previous_norm["read_date"],
                "to_date": current_norm["read_date"],
                "days": days,
                "meter_serial": current_norm["meter_serial"],
                "vt_kwh": vt_float,
                "nt_kwh": nt_float,
                "total_kwh": total_float,
                "avg_kwh_day": avg_daily,
                "vt_share_percent": vt_share,
                "nt_share_percent": nt_share,
                "reason": current_norm.get("reason"),
                "source": current_norm.get("source"),
                "status": current_norm.get("status"),
                "previous_total_kwh": previous_norm.get("total_kwh"),
                "current_total_kwh": current_norm.get("total_kwh"),
            }
        )

        previous_raw = current_raw
        previous_norm = current_norm

    latest_reading = normalized_readings[-1] if normalized_readings else None
    latest_period = periods[-1] if periods else None

    return {
        "readings": normalized_readings,
        "periods": periods,
        "latest_reading": latest_reading,
        "latest_period": latest_period,
        "readings_count": len(normalized_readings),
        "periods_count": len(periods),
    }


def save_archive(
    archive: dict[str, Any],
    archive_dir: Path,
    key: str,
) -> dict[str, str]:
    """Save archive to JSON and CSV files."""
    archive_dir.mkdir(parents=True, exist_ok=True)

    safe_key = _safe_filename(key)

    json_path = archive_dir / f"readings_{safe_key}.json"
    csv_path = archive_dir / f"periods_{safe_key}.csv"

    json_path.write_text(
        json.dumps(archive, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    period_fields = [
        "from",
        "to",
        "from_date",
        "to_date",
        "days",
        "meter_serial",
        "vt_kwh",
        "nt_kwh",
        "total_kwh",
        "avg_kwh_day",
        "vt_share_percent",
        "nt_share_percent",
        "reason",
        "source",
        "status",
        "previous_total_kwh",
        "current_total_kwh",
    ]

    with csv_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=period_fields, delimiter=";")
        writer.writeheader()
        for row in archive.get("periods", []):
            writer.writerow({field: row.get(field) for field in period_fields})

    return {
        "json_path": str(json_path),
        "csv_path": str(csv_path),
    }
