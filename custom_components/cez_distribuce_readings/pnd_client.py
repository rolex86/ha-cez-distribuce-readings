"""Standalone PND client based on the working /config/pnd_probe_ha.py flow."""

from __future__ import annotations

import html
import json
import logging
import urllib.parse
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import requests

from .api import CezDistribuceAuthError, CezDistribuceNetworkError

_LOGGER = logging.getLogger(__name__)

# These constants are intentionally hardcoded to match the proven
# /config/pnd_probe_ha.py flow byte-for-byte as closely as possible.
CAS_BASE_URL = "https://cas.cez.cz/cas"
CEZ_BASE_URL = "https://dip.cezdistribuce.cz/irj/portal"
PND_BASE_URL = "https://pnd.cezdistribuce.cz/cezpnd2"
CEZ_CLIENT_ID = "fjR3ZL9zrtsNcDQF.onpremise.dip.sap.dipcezdistribucecz.prod"
CLIENT_NAME = "CasOAuthClient"
RESPONSE_TYPE = "code"
SCOPE = "openid"

TIMEOUT = 30
PND_DEBUG_DIR = Path("/config/cez_distribuce_readings_debug")

BROWSER_HEADERS = {
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


class LoginFormParser(HTMLParser):
    """Extract login form action and input values from CAS HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self.current_form: dict[str, Any] | None = None
        self.global_inputs: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {key: value or "" for key, value in attrs_list}

        if tag.lower() == "form":
            self.current_form = {
                "action": attrs.get("action") or "",
                "inputs": {},
            }
            return

        if tag.lower() == "input":
            name = attrs.get("name")
            if not name:
                return

            value = attrs.get("value") or ""

            if self.current_form is not None:
                self.current_form["inputs"][name] = value
            else:
                self.global_inputs[name] = value

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form" and self.current_form is not None:
            self.forms.append(self.current_form)
            self.current_form = None


def sanitize_url(value: str | None) -> str | None:
    """Mask OAuth-like query secrets in debug output."""
    if not value:
        return value

    parsed = urllib.parse.urlparse(value)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)

    masked: list[tuple[str, str]] = []
    for key, val in query:
        if key.lower() in {"code", "ticket", "state", "nonce"}:
            masked.append((key, "***"))
        else:
            masked.append((key, val))

    return urllib.parse.urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urllib.parse.urlencode(masked),
            parsed.fragment,
        )
    )


def safe_headers(headers: Any) -> dict[str, str]:
    """Return headers with sensitive values hidden."""
    result: dict[str, str] = {}

    for key, value in dict(headers or {}).items():
        lowered = str(key).lower()

        if lowered in {"cookie", "authorization", "set-cookie", "x-request-token"}:
            result[str(key)] = "***"
        else:
            result[str(key)] = str(value)

    return result


def parse_login_form(
    html_text: str,
    login_url: str,
    username: str,
    password: str,
) -> tuple[str, dict[str, str]]:
    """Parse CAS login form using the same strategy as pnd_probe_ha.py."""
    parser = LoginFormParser()
    parser.feed(html_text)

    selected_form: dict[str, Any] | None = None

    for form in parser.forms:
        inputs = form.get("inputs") or {}
        if "execution" in inputs:
            selected_form = form
            break

    if selected_form is None:
        if "execution" in parser.global_inputs:
            selected_form = {
                "action": login_url,
                "inputs": parser.global_inputs,
            }
        else:
            raise CezDistribuceAuthError("CAS login form does not contain execution token")

    action = str(selected_form.get("action") or login_url)
    action = html.unescape(action)
    form_action = urllib.parse.urljoin(login_url, action)

    payload = dict(selected_form.get("inputs") or {})
    payload.update(
        {
            "username": username,
            "password": password,
            "_eventId": "submit",
            "geolocation": payload.get("geolocation", ""),
        }
    )

    return form_action, payload


def looks_like_login_page(response: requests.Response) -> bool:
    """Detect whether the response is still a CAS login page."""
    url = response.url.lower()

    if "cas.cez.cz" in url and "/login" in url:
        return True

    text = response.text[:5000].lower()

    return (
        'name="execution"' in text
        or 'id="fm1"' in text
        or "<title>login" in text
    )


def extract_token(payload: Any) -> str | None:
    """Extract X-Request-Token from known ČEZ payload shapes."""
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
                nested = extract_token(value)
                if nested:
                    return nested

        for value in payload.values():
            if isinstance(value, dict):
                nested = extract_token(value)
                if nested:
                    return nested

    return None


class CezPndClient:
    """One-shot PND client that mirrors the working probe flow."""

    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password
        self.last_warmup_status_code: int | None = None
        self.last_warmup_url: str | None = None
        self.last_data_status_code: int | None = None
        self.last_data_url: str | None = None
        self._session_cookie_count = 0

    def _request(
        self,
        session: requests.Session,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        """Execute a low-level request in the local one-shot session."""
        try:
            return session.request(method, url, timeout=TIMEOUT, **kwargs)
        except requests.Timeout as err:
            raise CezDistribuceNetworkError(f"PND request timed out for {url}") from err
        except requests.RequestException as err:
            raise CezDistribuceNetworkError(f"PND request failed for {url}") from err

    def _dump_response(
        self,
        response: requests.Response,
        *,
        kind: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Persist response diagnostics for comparison with the working probe."""
        try:
            PND_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            base = PND_DEBUG_DIR / f"{stamp}_{kind}"
            content_type = response.headers.get("content-type", "")

            meta = {
                "captured_at": datetime.now().isoformat(),
                "kind": kind,
                "status_code": response.status_code,
                "final_url": sanitize_url(response.url),
                "content_type": content_type,
                "redirect_history": [
                    {
                        "status_code": item.status_code,
                        "url": sanitize_url(item.url),
                        "location": sanitize_url(item.headers.get("location")),
                    }
                    for item in response.history
                ],
                "request": {
                    "method": response.request.method if response.request else None,
                    "url": sanitize_url(response.request.url) if response.request else None,
                    "headers": safe_headers(response.request.headers) if response.request else {},
                },
                "session_cookie_count": self._session_cookie_count,
                "response_cookie_count": len(response.cookies),
                "response_headers": safe_headers(response.headers),
                "body_preview": response.text[:3000],
                "payload": payload,
            }

            meta_path = base.with_suffix(".json")
            if "html" in content_type.lower():
                body_path = base.with_suffix(".html")
            elif "json" in content_type.lower():
                body_path = base.with_suffix(".response.json")
            else:
                body_path = base.with_suffix(".txt")

            body_path.write_bytes(response.content)
            meta["body_path"] = str(body_path)
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _LOGGER.warning("PND debug dump saved: meta=%s body=%s", meta_path, body_path)
        except Exception as err:
            _LOGGER.warning("Unable to write PND debug dump for %s: %s", kind, err)

    def _log_response(self, label: str, response: requests.Response) -> None:
        """Log compact status diagnostics."""
        _LOGGER.warning(
            "%s: status=%s url=%s content_type=%s history=%s",
            label,
            response.status_code,
            sanitize_url(response.url),
            response.headers.get("content-type"),
            [
                (
                    item.status_code,
                    sanitize_url(item.url),
                    sanitize_url(item.headers.get("location")),
                )
                for item in response.history
            ],
        )

    def get_chart_data(
        self,
        id_device_set: str | int,
        interval_from: str,
        interval_to: str,
        id_assembly: int = -1001,
    ) -> Any:
        """Fetch PND chart data using the proven standalone HA probe flow."""
        self.last_warmup_status_code = None
        self.last_warmup_url = None
        self.last_data_status_code = None
        self.last_data_url = None
        self._session_cookie_count = 0

        session = requests.Session()
        session.max_redirects = 30
        session.headers.update(BROWSER_HEADERS)

        service_url = (
            f"{CAS_BASE_URL}/oauth2.0/callbackAuthorize"
            f"?client_id={CEZ_CLIENT_ID}"
            f"&redirect_uri={urllib.parse.quote(CEZ_BASE_URL)}"
            f"&response_type={RESPONSE_TYPE}"
            f"&client_name={CLIENT_NAME}"
        )
        login_url = (
            f"{CAS_BASE_URL}/login"
            f"?service={urllib.parse.quote(service_url)}"
        )
        authorize_url = (
            f"{CAS_BASE_URL}/oidc/authorize"
            f"?scope={SCOPE}"
            f"&response_type={RESPONSE_TYPE}"
            f"&redirect_uri={urllib.parse.quote(CEZ_BASE_URL)}"
            f"&client_id={CEZ_CLIENT_ID}"
        )

        _LOGGER.warning("PND standalone flow start")

        response = self._request(session, "GET", login_url)
        self._log_response("PND CAS login page", response)
        try:
            response.raise_for_status()
        except requests.RequestException as err:
            self._dump_response(response, kind="01_cas_login_page_error")
            raise CezDistribuceNetworkError("Unable to load CAS login page") from err

        form_action, form_payload = parse_login_form(
            response.text,
            login_url,
            self.username,
            self.password,
        )

        response = self._request(
            session,
            "POST",
            form_action,
            data=form_payload,
            headers={
                "Origin": "https://cas.cez.cz",
                "Referer": login_url,
            },
        )
        self._log_response("PND CAS login submit", response)

        if looks_like_login_page(response):
            self._dump_response(response, kind="02_cas_login_submit_login_page")
            raise CezDistribuceAuthError("CAS login submit ended on login page")

        if response.status_code >= 400:
            self._dump_response(response, kind="02_cas_login_submit_error")
            raise CezDistribuceNetworkError(
                f"CAS login submit failed: HTTP {response.status_code} at {response.url}"
            )

        response = self._request(session, "GET", authorize_url)
        self._log_response("PND CAS authorize", response)

        if looks_like_login_page(response):
            self._dump_response(response, kind="03_cas_authorize_login_page")
            raise CezDistribuceAuthError("CAS authorize ended on login page")

        if response.status_code >= 400:
            self._dump_response(response, kind="03_cas_authorize_error")
            raise CezDistribuceNetworkError(
                f"CAS authorize failed: HTTP {response.status_code} at {response.url}"
            )

        token_url = f"{CEZ_BASE_URL}/rest-auth-api?path=/token/get"
        response = self._request(session, "GET", token_url)
        self._log_response("PND ČEZ token", response)

        if response.status_code >= 400:
            self._dump_response(response, kind="04_cez_token_error")
            raise CezDistribuceNetworkError(
                f"ČEZ token request failed: HTTP {response.status_code} at {response.url}"
            )

        token = None
        try:
            token = extract_token(response.json())
        except Exception as err:
            _LOGGER.warning("PND token JSON parse failed, continuing without token: %r", err)

        if token:
            session.headers.update({"X-Request-Token": token})
            _LOGGER.warning("PND X-Request-Token loaded")
        else:
            _LOGGER.warning("PND X-Request-Token not found, continuing")

        warmup_url = f"{PND_BASE_URL}/external/dashboard/view"
        response = self._request(
            session,
            "GET",
            warmup_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://pnd.cezdistribuce.cz/",
            },
        )
        self.last_warmup_status_code = response.status_code
        self.last_warmup_url = response.url
        self._session_cookie_count = len(session.cookies)
        self._log_response("PND warm-up", response)
        self._dump_response(response, kind="05_pnd_warmup")

        # Match the working probe: do not stop here. Even if warm-up ever returns
        # an HTML error, the data POST is the decisive request and gives better diagnostics.
        if looks_like_login_page(response):
            raise CezDistribuceAuthError(
                f"PND warm-up ended on login page: HTTP {response.status_code} at {response.url}"
            )

        pnd_payload = {
            "format": "chart",
            "idAssembly": id_assembly,
            "idDeviceSet": str(id_device_set),
            "intervalFrom": interval_from,
            "intervalTo": interval_to,
            "compareFrom": None,
            "opmId": None,
            "electrometerId": None,
        }

        response = self._request(
            session,
            "POST",
            f"{PND_BASE_URL}/external/data",
            json=pnd_payload,
            headers={
                "Origin": "https://pnd.cezdistribuce.cz",
                "Referer": "https://pnd.cezdistribuce.cz/cezpnd2/external/dashboard/view",
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
            },
        )
        self.last_data_status_code = response.status_code
        self.last_data_url = response.url
        self._session_cookie_count = len(session.cookies)
        self._log_response("PND data", response)
        self._dump_response(response, kind="06_pnd_data", payload=pnd_payload)

        content_type = response.headers.get("content-type", "").lower()

        if response.status_code == 200 and "application/json" in content_type:
            data = response.json()
            _LOGGER.warning(
                "PND standalone flow success: type=%s keys=%s",
                type(data).__name__,
                list(data.keys()) if isinstance(data, dict) else None,
            )
            return data

        if looks_like_login_page(response):
            raise CezDistribuceAuthError(
                f"PND data request ended on login page: HTTP {response.status_code} at {response.url}"
            )

        if response.status_code in (401, 403):
            raise CezDistribuceAuthError(
                f"PND data auth failed: HTTP {response.status_code} at {response.url}"
            )

        raise CezDistribuceNetworkError(
            f"PND data request failed: HTTP {response.status_code} at {response.url}"
        )
