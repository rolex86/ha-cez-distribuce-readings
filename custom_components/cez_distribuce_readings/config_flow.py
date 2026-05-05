"""Config flow for ČEZ Distribuce Readings."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector

from .api import (
    CezDistribuceAuthError,
    CezDistribuceClient,
    CezDistribuceNetworkError,
)
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
    _reauth_entry: config_entries.ConfigEntry | None = None

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "CezDistribuceOptionsFlow":
        """Return the options flow handler."""
        return CezDistribuceOptionsFlow(config_entry)

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

            except CezDistribuceAuthError as err:
                _LOGGER.exception(
                    "ČEZ Distribuce setup auth failed. Error type=%s, error=%s",
                    type(err).__name__,
                    err,
                )
                errors["base"] = "invalid_auth"
            except CezDistribuceNetworkError as err:
                _LOGGER.exception(
                    "ČEZ Distribuce setup network failed. Error type=%s, error=%s",
                    type(err).__name__,
                    err,
                )
                errors["base"] = "cannot_connect"
            except Exception as err:
                _LOGGER.exception(
                    "ČEZ Distribuce setup unexpected error. Error type=%s, error=%s",
                    type(err).__name__,
                    err,
                )
                errors["base"] = "unknown"
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

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        """Handle start of reauthentication flow."""
        entry_id = entry_data.get("entry_id")
        if not entry_id:
            return self.async_abort(reason="unknown")

        self._reauth_entry = self.hass.config_entries.async_get_entry(entry_id)
        if self._reauth_entry is None:
            return self.async_abort(reason="unknown")

        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ):
        """Confirm and store new password for reauthentication."""
        if self._reauth_entry is None:
            return self.async_abort(reason="unknown")

        errors: dict[str, str] = {}
        username = self._reauth_entry.data[CONF_USERNAME]

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            client = CezDistribuceClient(username=username, password=password)
            try:
                await self.hass.async_add_executor_job(client.login)
                await self.hass.async_add_executor_job(client.get_supply_points)
            except CezDistribuceAuthError:
                errors["base"] = "invalid_auth"
            except CezDistribuceNetworkError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                updated_data = dict(self._reauth_entry.data)
                updated_data[CONF_PASSWORD] = password
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry,
                    data=updated_data,
                )
                await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        data_schema = vol.Schema(
            {
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.PASSWORD,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=data_schema,
            errors=errors,
        )


class CezDistribuceOptionsFlow(config_entries.OptionsFlow):
    """Handle options for ČEZ Distribuce Readings."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ):
        """Manage integration options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_scan_interval = self._config_entry.options.get(
            CONF_SCAN_INTERVAL,
            self._config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MIN),
        )
        current_detailed_history = self._config_entry.options.get(
            CONF_DETAILED_HISTORY,
            self._config_entry.data.get(CONF_DETAILED_HISTORY, True),
        )

        data_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=current_scan_interval,
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
                    default=current_detailed_history,
                ): selector.BooleanSelector(),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
        )