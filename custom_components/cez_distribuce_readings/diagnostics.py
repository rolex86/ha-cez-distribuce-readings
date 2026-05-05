"""Diagnostics support for ČEZ Distribuce Readings."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_PASSWORD, CONF_USERNAME, DOMAIN

REDACTED = {CONF_USERNAME, CONF_PASSWORD}


def _sanitize_archive(archive: dict[str, Any]) -> dict[str, Any]:
    """Keep only non-sensitive archive summary fields."""
    return {
        "readings_count": archive.get("readings_count"),
        "periods_count": archive.get("periods_count"),
        "first_reading_date": archive.get("first_reading_date"),
        "last_reading_date": archive.get("last_reading_date"),
        "has_export_files": bool(archive.get("json_path") or archive.get("csv_path")),
    }


def _build_data_summary(data: dict[str, Any]) -> dict[str, Any]:
    """Build anonymized summary from coordinator data."""
    points = data.get("points", [])
    details_by_uid = data.get("details_by_uid", {})
    readings_by_uid = data.get("readings_by_uid", {})
    signals_by_uid = data.get("signals_by_uid", {})
    archives_by_uid = data.get("archives_by_uid", {})

    return {
        "points_count": len(points) if isinstance(points, list) else None,
        "detail_uids_count": len(details_by_uid) if isinstance(details_by_uid, dict) else None,
        "reading_uids_count": len(readings_by_uid) if isinstance(readings_by_uid, dict) else None,
        "signal_uids_count": len(signals_by_uid) if isinstance(signals_by_uid, dict) else None,
        "archives_summary_by_uid": {
            uid: _sanitize_archive(archive)
            for uid, archive in archives_by_uid.items()
            if isinstance(uid, str) and isinstance(archive, dict)
        }
        if isinstance(archives_by_uid, dict)
        else {},
        "sample_point_keys": sorted(points[0].keys()) if points and isinstance(points[0], dict) else [],
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = hass.data[DOMAIN].get(config_entry.entry_id)
    data = coordinator.data if coordinator and isinstance(coordinator.data, dict) else {}

    return {
        "entry": async_redact_data(dict(config_entry.data), REDACTED),
        "options": dict(config_entry.options),
        "coordinator": {
            "last_update_success": coordinator.last_update_success if coordinator else None,
            "last_exception": repr(coordinator.last_exception) if coordinator else None,
            "update_interval_seconds": coordinator.update_interval.total_seconds()
            if coordinator and coordinator.update_interval
            else None,
        },
        "data_summary": _build_data_summary(data),
    }
