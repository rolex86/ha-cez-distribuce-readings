"""Constants for ČEZ Distribuce Readings."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "cez_distribuce_readings"

CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_DETAILED_HISTORY = "detailed_history"

DEFAULT_SCAN_INTERVAL_MIN = 360
DEFAULT_SCAN_INTERVAL = timedelta(minutes=DEFAULT_SCAN_INTERVAL_MIN)

CAS_BASE_URL = "https://cas.cez.cz/cas"
CLIENT_NAME = "CasOAuthClient"
RESPONSE_TYPE = "code"
SCOPE = "openid"

CEZ_DISTRIBUCE_CLIENT_ID = (
    "fjR3ZL9zrtsNcDQF.onpremise.dip.sap.dipcezdistribucecz.prod"
)
CEZ_DISTRIBUCE_BASE_URL = "https://dip.cezdistribuce.cz/irj/portal"

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]