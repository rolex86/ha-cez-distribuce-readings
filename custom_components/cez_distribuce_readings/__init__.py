"""ČEZ Distribuce Readings integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import CezDistribuceClient
from .const import (
    CONF_DETAILED_HISTORY,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DEFAULT_SCAN_INTERVAL_MIN,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import CezDistribuceCoordinator


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

    client = CezDistribuceClient(username=username, password=password)

    coordinator = CezDistribuceCoordinator(
        hass=hass,
        client=client,
        scan_interval=timedelta(minutes=scan_interval_min),
        detailed_history=detailed_history,
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload integration."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok