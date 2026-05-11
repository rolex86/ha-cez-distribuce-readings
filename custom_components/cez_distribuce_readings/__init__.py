"""ČEZ Distribuce Readings integration."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

import voluptuous as vol

from .api import CezDistribuceClient
from .const import (
    CONF_DETAILED_HISTORY,
    CONF_PASSWORD,
    CONF_PND_DEVICE_SET_ID,
    CONF_PND_ENABLED,
    CONF_PND_ID_ASSEMBLY,
    CONF_PND_TARGET,
    CONF_PND_UPDATE_INTERVAL_MIN,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DEFAULT_PND_DEVICE_SET_ID,
    DEFAULT_PND_ENABLED,
    DEFAULT_PND_ID_ASSEMBLY,
    DEFAULT_PND_TARGET,
    DEFAULT_PND_UPDATE_INTERVAL_MIN,
    DEFAULT_SCAN_INTERVAL_MIN,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import CezDistribuceCoordinator
from .pnd import current_month_interval, format_pnd_datetime
from .pnd_client import CezPndClient

_LOGGER = logging.getLogger(__name__)
_SERVICE_DEBUG_PND_FETCH = "debug_pnd_fetch"
_ATTR_ENTRY_ID = "entry_id"


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options are changed."""
    await hass.config_entries.async_reload(entry.entry_id)


def _run_debug_pnd_fetch(
    coordinator: CezDistribuceCoordinator,
    entry_id: str,
) -> dict[str, object]:
    """Run one direct PND probe from the HA Core process."""
    client = CezPndClient(
        username=coordinator.client.username,
        password=coordinator.client.password,
    )
    interval_start, interval_end = current_month_interval(datetime.now())
    interval_from = format_pnd_datetime(interval_start)
    interval_to = format_pnd_datetime(interval_end)
    payload = client.get_chart_data(
        id_device_set=coordinator.pnd_device_set_id,
        id_assembly=coordinator.pnd_id_assembly,
        interval_from=interval_from,
        interval_to=interval_to,
    )

    debug_dir = client.debug_dir or Path("/config/cez_distribuce_readings_debug")
    summary = {
        "entry_id": entry_id,
        "interval_from": interval_from,
        "interval_to": interval_to,
        "warmup_status_code": client.last_warmup_status_code,
        "warmup_url": client.last_warmup_url,
        "data_status_code": client.last_data_status_code,
        "data_url": client.last_data_url,
        "payload_type": type(payload).__name__,
        "debug_dir": str(debug_dir),
    }
    if isinstance(payload, dict):
        summary["payload_keys"] = list(payload.keys())[:20]
        summary["unitY"] = payload.get("unitY")
        summary["series_count"] = len(payload.get("series") or []) if isinstance(payload.get("series"), list) else None

    summary_path = debug_dir / "service_debug_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


async def _async_handle_debug_pnd_fetch(hass: HomeAssistant, call: ServiceCall) -> None:
    """Run the temporary debug PND fetch service."""
    coordinators: dict[str, CezDistribuceCoordinator] = hass.data.get(DOMAIN, {})
    if not coordinators:
        raise HomeAssistantError("ČEZ Distribuce integration is not loaded")

    entry_id = str(call.data.get(_ATTR_ENTRY_ID) or "").strip()
    coordinator: CezDistribuceCoordinator | None = None

    if entry_id:
        coordinator = coordinators.get(entry_id)
        if coordinator is None:
            raise HomeAssistantError(f"Unknown ČEZ entry_id: {entry_id}")
    else:
        coordinator = next(iter(coordinators.values()))

    if not coordinator.pnd_enabled:
        raise HomeAssistantError("PND is currently disabled in integration options")

    if not coordinator.pnd_device_set_id:
        raise HomeAssistantError("PND deviceSet is empty in integration options")

    resolved_entry_id = entry_id or next(
        key for key, value in coordinators.items() if value is coordinator
    )
    _LOGGER.warning(
        "Starting temporary ČEZ PND debug fetch. entry_id=%s device_set=%s target=%s",
        resolved_entry_id,
        coordinator.pnd_device_set_id,
        coordinator.pnd_target,
    )

    try:
        summary = await hass.async_add_executor_job(_run_debug_pnd_fetch, coordinator, resolved_entry_id)
    except Exception as err:
        _LOGGER.warning("Temporary ČEZ PND debug fetch failed: %s: %s", type(err).__name__, err)
        raise

    _LOGGER.warning(
        "Temporary ČEZ PND debug fetch finished. summary=%s",
        summary,
    )


def _ensure_debug_service_registered(hass: HomeAssistant) -> None:
    """Register the temporary debug PND fetch service once."""
    if hass.services.has_service(DOMAIN, _SERVICE_DEBUG_PND_FETCH):
        return

    schema = vol.Schema({vol.Optional(_ATTR_ENTRY_ID): str})
    hass.services.async_register(
        DOMAIN,
        _SERVICE_DEBUG_PND_FETCH,
        lambda call: _async_handle_debug_pnd_fetch(hass, call),
        schema=schema,
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up integration from config entry."""
    hass.data.setdefault(DOMAIN, {})

    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    scan_interval_min = entry.options.get(
        CONF_SCAN_INTERVAL,
        entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MIN),
    )
    detailed_history = entry.options.get(
        CONF_DETAILED_HISTORY,
        entry.data.get(CONF_DETAILED_HISTORY, True),
    )
    pnd_enabled = entry.options.get(
        CONF_PND_ENABLED,
        entry.data.get(CONF_PND_ENABLED, DEFAULT_PND_ENABLED),
    )
    pnd_device_set_id = entry.options.get(
        CONF_PND_DEVICE_SET_ID,
        entry.data.get(CONF_PND_DEVICE_SET_ID, DEFAULT_PND_DEVICE_SET_ID),
    )
    pnd_id_assembly = entry.options.get(
        CONF_PND_ID_ASSEMBLY,
        entry.data.get(CONF_PND_ID_ASSEMBLY, DEFAULT_PND_ID_ASSEMBLY),
    )
    pnd_target = entry.options.get(
        CONF_PND_TARGET,
        entry.data.get(CONF_PND_TARGET, DEFAULT_PND_TARGET),
    )
    pnd_update_interval_min = entry.options.get(
        CONF_PND_UPDATE_INTERVAL_MIN,
        entry.data.get(CONF_PND_UPDATE_INTERVAL_MIN, DEFAULT_PND_UPDATE_INTERVAL_MIN),
    )

    client = CezDistribuceClient(username=username, password=password)

    coordinator = CezDistribuceCoordinator(
        hass=hass,
        client=client,
        scan_interval=timedelta(minutes=scan_interval_min),
        detailed_history=detailed_history,
        pnd_enabled=bool(pnd_enabled),
        pnd_device_set_id=str(pnd_device_set_id or ""),
        pnd_id_assembly=int(pnd_id_assembly),
        pnd_target=str(pnd_target or ""),
        pnd_update_interval_min=int(pnd_update_interval_min),
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator
    _ensure_debug_service_registered(hass)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload integration."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN] and hass.services.has_service(DOMAIN, _SERVICE_DEBUG_PND_FETCH):
            hass.services.async_remove(DOMAIN, _SERVICE_DEBUG_PND_FETCH)

    return unload_ok
