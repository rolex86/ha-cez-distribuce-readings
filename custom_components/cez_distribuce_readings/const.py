"""Constants for ČEZ Distribuce Readings."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "cez_distribuce_readings"

CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_DETAILED_HISTORY = "detailed_history"
CONF_PND_ENABLED = "pnd_enabled"
CONF_PND_DEVICE_SET_ID = "pnd_device_set_id"
CONF_PND_TARGET = "pnd_target"
CONF_PND_UPDATE_INTERVAL_MIN = "pnd_update_interval_min"

DEFAULT_SCAN_INTERVAL_MIN = 360
DEFAULT_SCAN_INTERVAL = timedelta(minutes=DEFAULT_SCAN_INTERVAL_MIN)
DEFAULT_PND_ENABLED = False
DEFAULT_PND_DEVICE_SET_ID = ""
DEFAULT_PND_TARGET = ""
DEFAULT_PND_UPDATE_INTERVAL_MIN = 60
MIN_PND_UPDATE_INTERVAL_MIN = 30

CAS_BASE_URL = "https://cas.cez.cz/cas"
CLIENT_NAME = "CasOAuthClient"
RESPONSE_TYPE = "code"
SCOPE = "openid"

CEZ_DISTRIBUCE_CLIENT_ID = (
    "fjR3ZL9zrtsNcDQF.onpremise.dip.sap.dipcezdistribucecz.prod"
)
CEZ_DISTRIBUCE_BASE_URL = "https://dip.cezdistribuce.cz/irj/portal"
PND_INTERVAL_HOURS = 0.25

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]
