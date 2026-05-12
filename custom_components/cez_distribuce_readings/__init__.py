"""CEZ Distribuce Readings integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import CezDistribuceClient
from .const import (
    CONF_DETAILED_HISTORY,
    CONF_PASSWORD,
    CONF_PND_DEVICE_SET_ID,
    CONF_PND_ENABLED,
    CONF_PND_TARGET,
    CONF_PND_UPDATE_INTERVAL_MIN,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DEFAULT_PND_DEVICE_SET_ID,
    DEFAULT_PND_ENABLED,
    DEFAULT_PND_TARGET,
    DEFAULT_PND_UPDATE_INTERVAL_MIN,
    DEFAULT_SCAN_INTERVAL_MIN,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import CezDistribuceCoordinator


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options are changed."""
    await hass.config_entries.async_reload(entry.entry_id)


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
        pnd_target=str(pnd_target or ""),
        pnd_update_interval_min=int(pnd_update_interval_min),
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload integration."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok
