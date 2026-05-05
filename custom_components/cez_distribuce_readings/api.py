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


class CezDistribuceNetworkError(CezDistribuceError):
    """Network or HTTP transport error."""


class CezDistribuceUnexpectedResponseError(CezDistribuceError):
    """Portal returned an unexpected response payload."""


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
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id

        self.session = requests.Session()
        self.session.max_redirects = 10
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "application/json,text/plain,*/*;q=0.8"
                ),
                "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
            }
        )
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

    def _is_expected_oauth_redirect_404(self, response: requests.Response) -> bool:
        """Return true when CAS ended on the expected portal callback URL.

        ČEZ CAS can redirect to:
        https://dip.cezdistribuce.cz/irj/portal?code=...

        In a browser this continues inside the portal. In requests, the final
        portal callback can end as HTTP 404 while the session cookies were still
        issued correctly. For this specific URL shape, the 404 is not fatal.
        """
        if response.status_code != 404:
            return False

        parsed = urllib.parse.urlparse(response.url)
        query = urllib.parse.parse_qs(parsed.query)

        return response.url.startswith(self.base_url) and "code" in query

    def _raise_unless_expected_oauth_redirect(
        self,
        label: str,
        response: requests.Response,
    ) -> None:
        """Raise for status, except for the expected final OAuth callback 404."""
        if self._is_expected_oauth_redirect_404(response):
            _LOGGER.debug(
                "%s ended with expected OAuth redirect 404. url=%s",
                label,
                response.url,
            )
            return

        response.raise_for_status()

    def _login_form_payload(self, html: str) -> tuple[str, dict[str, str]]:
        """Build CAS login form action and payload from the actual HTML form."""
        soup = BeautifulSoup(html, "html.parser")
        execution_input = soup.find("input", {"name": "execution"})

        if not execution_input or not execution_input.get("value"):
            _LOGGER.error(
                "CAS login page did not contain execution token. body_start=%r",
                html[:1000],
            )
            raise CezDistribuceAuthError("CAS login form did not contain execution token")

        form = execution_input.find_parent("form") or soup.find("form")
        form_action = self.login_url

        if form and form.get("action"):
            form_action = urllib.parse.urljoin(self.login_url, form.get("action"))

        payload: dict[str, str] = {}

        if form:
            for input_tag in form.find_all("input"):
                name = input_tag.get("name")
                if not name:
                    continue
                payload[name] = input_tag.get("value", "")

        payload.update(
            {
                "username": self.username,
                "password": self.password,
                "execution": execution_input["value"],
                "_eventId": "submit",
                "geolocation": payload.get("geolocation", ""),
            }
        )

        _LOGGER.debug(
            "CAS login form prepared. action=%s fields=%s",
            form_action,
            sorted(payload.keys()),
        )

        return form_action, payload

    def _reset_session(self) -> None:
        """Drop portal cookies and request token before a forced relogin."""
        self.session.cookies.clear()
        self.session.headers.pop("X-Request-Token", None)
        self._logged_in = False

    def _request_with_network_errors(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        """Execute HTTP request and map transport errors to integration exceptions."""
        try:
            return self.session.request(method, url, timeout=TIMEOUT, **kwargs)
        except requests.Timeout as err:
            raise CezDistribuceNetworkError(f"ČEZ request timed out for {url}") from err
        except requests.RequestException as err:
            raise CezDistribuceNetworkError(f"ČEZ request failed for {url}") from err

    def login(self) -> None:
        """Login and refresh portal X-Request-Token."""
        _LOGGER.debug("Logging in to ČEZ Distribuce portal")

        response = self._request_with_network_errors("GET", self.login_url)
        self._debug_response("CAS login page response", response)
        try:
            response.raise_for_status()
        except requests.RequestException as err:
            raise CezDistribuceNetworkError("Unable to load CAS login page") from err

        form_action, form_payload = self._login_form_payload(response.text)

        response = self._request_with_network_errors(
            "POST",
            form_action,
            data=form_payload,
            headers={
                "Origin": CAS_BASE_URL.replace("/cas", ""),
                "Referer": self.login_url,
            },
        )
        self._debug_response("CAS login submit response", response)

        if response.status_code in (400, 401, 403):
            _LOGGER.error(
                "CAS login submit failed. status=%s url=%s content_type=%s body_start=%r",
                response.status_code,
                response.url,
                response.headers.get("content-type"),
                response.text[:1500],
            )
            raise CezDistribuceAuthError(
                f"CAS login submit failed with HTTP {response.status_code}"
            )

        self._raise_unless_expected_oauth_redirect("CAS login submit response", response)

        if "login" in response.url.lower() and "cas.cez.cz" in response.url.lower():
            _LOGGER.error(
                "CAS login submit ended on login page again. This usually means invalid credentials "
                "or unsupported login flow. body_start=%r",
                response.text[:1000],
            )
            raise CezDistribuceAuthError("CAS login did not leave login page")

        response = self._request_with_network_errors("GET", self.authorize_url)
        self._debug_response("CAS authorize response", response)
        self._raise_unless_expected_oauth_redirect("CAS authorize response", response)

        self.refresh_api_token()
        self._logged_in = True
        _LOGGER.debug("ČEZ Distribuce login completed successfully")

    def force_login(self) -> None:
        """Force a clean login after the portal returned an expired-session page."""
        _LOGGER.debug("Forcing fresh ČEZ Distribuce login")
        self._reset_session()
        self.login()

    def ensure_logged_in(self) -> None:
        """Ensure the session is logged in."""
        if not self._logged_in:
            self.login()

    def _extract_token_from_payload(self, payload: Any) -> str | None:
        """Extract X-Request-Token from possible ČEZ token payload shapes."""
        if isinstance(payload, str):
            token = payload.strip()
            return token or None

        if isinstance(payload, dict):
            for key in (
                "data",
                "token",
                "requestToken",
                "xRequestToken",
                "X-Request-Token",
                "xsrfToken",
                "csrfToken",
            ):
                value = payload.get(key)

                if isinstance(value, str) and value.strip():
                    return value.strip()

                if isinstance(value, dict):
                    nested = self._extract_token_from_payload(value)
                    if nested:
                        return nested

            for value in payload.values():
                if isinstance(value, dict):
                    nested = self._extract_token_from_payload(value)
                    if nested:
                        return nested

        return None

    def _looks_like_html(self, response: requests.Response) -> bool:
        """Return true when the portal returned HTML instead of JSON.

        This usually means the ČEZ portal session expired and the request was
        redirected to a portal shell/login-like HTML page, even with HTTP 200.
        """
        content_type = (response.headers.get("content-type") or "").lower()

        if "text/html" in content_type or "application/xhtml" in content_type:
            return True

        text_start = response.text[:500].lstrip().lower()
        return text_start.startswith("<!doctype html") or text_start.startswith("<html")

    def _json_or_auth_error(
        self,
        response: requests.Response,
        label: str,
    ) -> Any:
        """Decode JSON or raise an auth error when HTML was returned."""
        if self._looks_like_html(response):
            _LOGGER.warning(
                "%s returned HTML instead of JSON. Treating it as expired ČEZ session. "
                "status=%s url=%s content_type=%s body_start=%r",
                label,
                response.status_code,
                response.url,
                response.headers.get("content-type"),
                response.text[:1000],
            )
            raise CezDistribuceAuthError("ČEZ portal returned HTML instead of JSON")

        try:
            return response.json()
        except ValueError as err:
            _LOGGER.error(
                "%s is not JSON. status=%s url=%s content_type=%s body_start=%r",
                label,
                response.status_code,
                response.url,
                response.headers.get("content-type"),
                response.text[:1000],
            )
            raise CezDistribuceError(
                f"ČEZ portal returned non-JSON response from {response.url}"
            ) from err

    def refresh_api_token(self) -> None:
        """Fetch and store X-Request-Token."""
        url = f"{self.base_url}/rest-auth-api?path=/token/get"
        response = self._request_with_network_errors("GET", url)
        self._debug_response("ČEZ token response", response)
        try:
            response.raise_for_status()
        except requests.RequestException as err:
            raise CezDistribuceNetworkError("Unable to fetch ČEZ API token") from err

        payload = self._json_or_auth_error(response, "ČEZ token response")
        token = self._extract_token_from_payload(payload)

        if not token:
            _LOGGER.error(
                "Unexpected ČEZ token payload shape. type=%s keys=%s",
                type(payload).__name__,
                list(payload.keys()) if isinstance(payload, dict) else None,
            )
            raise CezDistribuceAuthError("Unable to fetch X-Request-Token")

        self.session.headers.update({"X-Request-Token": token})
        _LOGGER.debug("ČEZ X-Request-Token loaded successfully")

    def _request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        """Call portal JSON endpoint and unwrap ČEZ response envelope."""
        self.ensure_logged_in()

        last_error: Exception | None = None

        for attempt in range(LOGIN_RETRIES):
            _LOGGER.debug(
                "ČEZ request attempt=%s method=%s url=%s",
                attempt + 1,
                method,
                url,
            )

            response = self._request_with_network_errors(method, url, **kwargs)
            self._debug_response("ČEZ JSON response", response)

            if response.status_code in (401, 403):
                _LOGGER.debug(
                    "Portal returned HTTP %s, forcing fresh login",
                    response.status_code,
                )
                self.force_login()
                continue

            try:
                response.raise_for_status()
            except requests.RequestException as err:
                raise CezDistribuceNetworkError(
                    f"ČEZ endpoint returned HTTP error for {url}"
                ) from err

            try:
                payload = self._json_or_auth_error(response, "ČEZ JSON response")
            except CezDistribuceAuthError as err:
                last_error = err
                if attempt + 1 >= LOGIN_RETRIES:
                    break

                _LOGGER.debug(
                    "ČEZ returned non-JSON/HTML response, forcing relogin and retry"
                )
                self.force_login()
                continue

            _LOGGER.debug(
                "ČEZ JSON payload received. type=%s keys=%s",
                type(payload).__name__,
                list(payload.keys()) if isinstance(payload, dict) else None,
            )

            if isinstance(payload, dict):
                status_code = payload.get("statusCode")

                if status_code in (401, 403):
                    _LOGGER.debug(
                        "Portal returned JSON statusCode %s, forcing fresh login",
                        status_code,
                    )
                    self.force_login()
                    continue

                if status_code not in (None, 200):
                    _LOGGER.error("ČEZ portal returned error payload=%r", payload)
                    raise CezDistribuceUnexpectedResponseError(
                        f"ČEZ portal returned statusCode={status_code}: {payload}"
                    )

                if "data" in payload:
                    return payload["data"]

            return payload

        if last_error is not None:
            raise CezDistribuceAuthError(
                "Unable to complete authenticated portal request after relogin"
            ) from last_error

        raise CezDistribuceUnexpectedResponseError(
            "Unable to complete authenticated portal request"
        )

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

        raise CezDistribuceUnexpectedResponseError(
            f"Unexpected meter reading history payload type: {type(data).__name__}"
        )

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

        response = self._request_with_network_errors("GET", url)
        self._debug_response("ČEZ signals export response", response)
        try:
            response.raise_for_status()
        except requests.RequestException as err:
            raise CezDistribuceNetworkError("Unable to download signals export") from err
        return response.content
