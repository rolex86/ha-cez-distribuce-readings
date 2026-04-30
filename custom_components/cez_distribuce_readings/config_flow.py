"""Config flow for ČEZ Distribuce Readings."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector

from .api import CezDistribuceClient
from .const import (
    CONF_DETAILED_HISTORY,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DEFAULT_SCAN_INTERVAL_MIN,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class CezDistribuceReadingsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle config flow."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ):
        """Handle user step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]

            _LOGGER.debug("Starting ČEZ Distribuce config flow validation for username=%s", username)

            client = CezDistribuceClient(username=username, password=password)

            try:
                _LOGGER.debug("Validating ČEZ login")
                await self.hass.async_add_executor_job(client.login)

                _LOGGER.debug("Validating ČEZ supply points loading")
                supply_points = await self.hass.async_add_executor_job(client.get_supply_points)

                _LOGGER.debug(
                    "ČEZ supply points validation response type=%s",
                    type(supply_points).__name__,
                )

            except Exception as err:
                _LOGGER.exception(
                    "ČEZ Distribuce setup failed. Error type=%s, error=%s",
                    type(err).__name__,
                    err,
                )
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(username)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title="ČEZ Distribuce odečty",
                    data=user_input,
                )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.TEXT,
                    )
                ),
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.PASSWORD,
                    )
                ),
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=DEFAULT_SCAN_INTERVAL_MIN,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=30,
                        max=1440,
                        step=30,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="min",
                    )
                ),
                vol.Optional(
                    CONF_DETAILED_HISTORY,
                    default=True,
                ): selector.BooleanSelector(),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )