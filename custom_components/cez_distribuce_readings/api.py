"""Small synchronous client for the ČEZ Distribuce portal."""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any

import requests
from bs4 import BeautifulSoup

from .const import (
    CAS_BASE_URL,
    CEZ_DISTRIBUCE_BASE_URL,
    CEZ_DISTRIBUCE_CLIENT_ID,
    CLIENT_NAME,
    RESPONSE_TYPE,
    SCOPE,
)

_LOGGER = logging.getLogger(__name__)

TIMEOUT = 30
LOGIN_RETRIES = 2


class CezDistribuceError(Exception):
    """Base ČEZ Distribuce API error."""


class CezDistribuceAuthError(CezDistribuceError):
    """Authentication error."""


class CezDistribuceClient:
    """Synchronous client used from HA executor jobs."""

    def __init__(
        self,
        username: str,
        password: str,
        base_url: str = CEZ_DISTRIBUCE_BASE_URL,
        client_id: str = CEZ_DISTRIBUCE_CLIENT_ID,
    ) -> None:
        self.username = username
        self.password = password
        self.base_url = base_url
        self.client_id = client_id

        self.session = requests.Session()
        self.session.max_redirects = 10
        self._logged_in = False

        self.service_url = (
            f"{CAS_BASE_URL}/oauth2.0/callbackAuthorize"
            f"?client_id={self.client_id}"
            f"&redirect_uri={urllib.parse.quote(self.base_url)}"
            f"&response_type={RESPONSE_TYPE}"
            f"&client_name={CLIENT_NAME}"
        )

        self.login_url = (
            f"{CAS_BASE_URL}/login"
            f"?service={urllib.parse.quote(self.service_url)}"
        )

        self.authorize_url = (
            f"{CAS_BASE_URL}/oidc/authorize"
            f"?scope={SCOPE}"
            f"&response_type={RESPONSE_TYPE}"
            f"&redirect_uri={urllib.parse.quote(self.base_url)}"
            f"&client_id={self.client_id}"
        )

    def _debug_response(self, label: str, response: requests.Response) -> None:
        """Log safe response diagnostics."""
        _LOGGER.debug(
            "%s: status=%s url=%s content_type=%s history=%s",
            label,
            response.status_code,
            response.url,
            response.headers.get("content-type"),
            [(item.status_code, item.url) for item in response.history],
        )

    def login(self) -> None:
        """Login and refresh portal X-Request-Token."""
        _LOGGER.debug("Logging in to ČEZ Distribuce portal")

        response = self.session.get(self.login_url, timeout=TIMEOUT)
        self._debug_response("CAS login page response", response)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        execution_input = soup.find("input", {"name": "execution"})

        if not execution_input or not execution_input.get("value"):
            _LOGGER.error(
                "CAS login page did not contain execution token. body_start=%r",
                response.text[:1000],
            )
            raise CezDistribuceAuthError("CAS login form did not contain execution token")

        response = self.session.post(
            self.login_url,
            data={
                "username": self.username,
                "password": self.password,
                "execution": execution_input["value"],
                "_eventId": "submit",
                "geolocation": "",
            },
            timeout=TIMEOUT,
        )
        self._debug_response("CAS login submit response", response)
        response.raise_for_status()

        if "login" in response.url.lower() and "cas.cez.cz" in response.url.lower():
            _LOGGER.error(
                "CAS login submit ended on login page again. This usually means invalid credentials "
                "or unsupported login flow. body_start=%r",
                response.text[:1000],
            )
            raise CezDistribuceAuthError("CAS login did not leave login page")

        response = self.session.get(self.authorize_url, timeout=TIMEOUT)
        self._debug_response("CAS authorize response", response)
        response.raise_for_status()

        self.refresh_api_token()
        self._logged_in = True
        _LOGGER.debug("ČEZ Distribuce login completed successfully")

    def ensure_logged_in(self) -> None:
        """Ensure the session is logged in."""
        if not self._logged_in:
            self.login()

    def refresh_api_token(self) -> None:
        """Fetch and store X-Request-Token."""
        url = f"{self.base_url}/rest-auth-api?path=/token/get"
        response = self.session.get(url, timeout=TIMEOUT)
        self._debug_response("ČEZ token response", response)
        response.raise_for_status()

        try:
            token = response.json()
        except ValueError as err:
            _LOGGER.error(
                "ČEZ token response is not JSON. status=%s content_type=%s body_start=%r",
                response.status_code,
                response.headers.get("content-type"),
                response.text[:1000],
            )
            raise CezDistribuceAuthError("Unable to fetch JSON X-Request-Token") from err

        if not isinstance(token, str) or not token:
            _LOGGER.error("Unexpected ČEZ token payload type=%s value=%r", type(token).__name__, token)
            raise CezDistribuceAuthError("Unable to fetch X-Request-Token")

        self.session.headers.update({"X-Request-Token": token})
        _LOGGER.debug("ČEZ X-Request-Token loaded successfully")

    def _request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        """Call portal JSON endpoint and unwrap ČEZ response envelope."""
        self.ensure_logged_in()

        for attempt in range(LOGIN_RETRIES):
            _LOGGER.debug("ČEZ request attempt=%s method=%s url=%s", attempt + 1, method, url)

            response = self.session.request(method, url, timeout=TIMEOUT, **kwargs)
            self._debug_response("ČEZ JSON response", response)

            if response.status_code == 401:
                _LOGGER.debug("Portal returned HTTP 401, refreshing login")
                self._logged_in = False
                self.login()
                continue

            response.raise_for_status()

            content_type = response.headers.get("content-type", "")

            try:
                payload = response.json()
            except ValueError as err:
                _LOGGER.error(
                    "ČEZ response is not JSON. url=%s status=%s content_type=%s body_start=%r",
                    response.url,
                    response.status_code,
                    content_type,
                    response.text[:1000],
                )
                raise CezDistribuceError(
                    f"ČEZ portal returned non-JSON response from {response.url}"
                ) from err

            _LOGGER.debug(
                "ČEZ JSON payload received. type=%s keys=%s",
                type(payload).__name__,
                list(payload.keys()) if isinstance(payload, dict) else None,
            )

            if isinstance(payload, dict):
                status_code = payload.get("statusCode")

                if status_code == 401:
                    _LOGGER.debug("Portal returned JSON statusCode 401, refreshing token")
                    self.refresh_api_token()
                    continue

                if status_code not in (None, 200):
                    _LOGGER.error("ČEZ portal returned error payload=%r", payload)
                    raise CezDistribuceError(
                        f"ČEZ portal returned statusCode={status_code}: {payload}"
                    )

                if "data" in payload:
                    return payload["data"]

            return payload

        raise CezDistribuceAuthError("Unable to complete authenticated portal request")

    def get_supply_points(self) -> Any:
        """Return supply points available to the logged-in user."""
        url = (
            f"{self.base_url}/vyhledani-om"
            "?path=/vyhledaniom/zakladniInfo/50/PREHLED_OM_CELEK"
        )
        return self._request_json(
            "POST",
            url,
            json={"nekontrolovatPrislusnostOM": False},
        )

    def get_supply_point_detail(self, uid: str) -> Any:
        """Return supply point detail."""
        url = f"{self.base_url}/prehled-om?path=supply-point-detail/{uid}"
        return self._request_json("GET", url)

    def get_meter_reading_history(
        self,
        uid: str,
        detailed: bool = True,
    ) -> list[dict[str, Any]]:
        """Return meter reading history for a supply point UID."""
        detailed_text = "true" if detailed else "false"
        url = (
            f"{self.base_url}/prehled-om"
            f"?path=supply-point-detail/meter-reading-history/{uid}/{detailed_text}"
        )
        data = self._request_json("POST", url, json={})

        if isinstance(data, list):
            return data

        return []

    def get_signals(self, ean: str) -> Any:
        """Return HDO / signal switching times for EAN."""
        url = f"{self.base_url}/prehled-om?path=supply-point-detail/signals/{ean}"
        return self._request_json("GET", url)

    def get_signals_export_raw(self, ean: str) -> bytes:
        """Return raw signals export.

        This is kept as a fallback/debug helper. The endpoint may return CSV, XLS,
        HTML or another non-JSON format depending on the portal implementation.
        """
        self.ensure_logged_in()

        url = (
            f"{self.base_url}/prehled-uctu"
            f"?path=dashboard/supply-point/signals/export/{ean}"
        )

        response = self.session.get(url, timeout=TIMEOUT)
        self._debug_response("ČEZ signals export response", response)
        response.raise_for_status()
        return response.content