"""Standalone PND client using the exact proven pnd_probe_ha.py request flow.

This module is deliberately self-contained and does not use the main
CezDistribuceClient session/helpers. The PND portal is sensitive to small
request/session differences, so the flow below mirrors the working probe:

fresh requests.Session -> CAS login -> CAS authorize -> token -> PND warm-up
-> POST /external/data.
"""

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

# Keep these constants local on purpose. They must match the working probe exactly
# and must not be affected by any main ČEZ client configuration.
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

PND_WARMUP_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://pnd.cezdistribuce.cz/",
}

PND_DATA_HEADERS = {
    "Origin": "https://pnd.cezdistribuce.cz",
    "Referer": "https://pnd.cezdistribuce.cz/cezpnd2/external/dashboard/view",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
}


class LoginFormParser(HTMLParser):
    """Extract the CAS login form in the same way as pnd_probe_ha.py."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self.current_form: dict[str, Any] | None = None
        self.global_inputs: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {key: value or "" for key, value in attrs_list}

        if tag.lower() == "form":
            self.current_form = {"action": attrs.get("action") or "", "inputs": {}}
            return

        if tag.lower() != "input":
            return

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
    """Mask OAuth-like query secrets in debug dumps."""
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
    """Parse CAS login form exactly like the working probe."""
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
            selected_form = {"action": login_url, "inputs": parser.global_inputs}
        else:
            raise CezDistribuceAuthError("CAS login form does not contain execution token")

    action = html.unescape(str(selected_form.get("action") or login_url))
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
    return 'name="execution"' in text or 'id="fm1"' in text or "<title>login" in text


def extract_token(payload: Any) -> str | None:
    """Extract X-Request-Token from known ČEZ token payload shapes."""
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
    """One-shot PND client, kept as close as possible to the working probe."""

    def __init__(
        self,
        username: str,
        password: str,
        base_url: str | None = None,
        client_id: str | None = None,
    ) -> None:
        self.username = username
        self.password = password
        # base_url/client_id are intentionally ignored. The PND flow must use
        # exactly the constants above, matching the successful probe.
        self.last_warmup_status_code: int | None = None
        self.last_warmup_url: str | None = None
        self.last_data_status_code: int | None = None
        self.last_data_url: str | None = None
        self._session_cookie_count = 0
        self._debug_dir: Path | None = None

    @property
    def debug_dir(self) -> Path | None:
        """Return the debug directory used by the last fetch attempt."""
        return self._debug_dir

    def _request(
        self,
        session: requests.Session,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        """Execute one request exactly through the local probe-style session."""
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
        """Persist request/response diagnostics for the current PND attempt."""
        try:
            if self._debug_dir is None:
                PND_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                self._debug_dir = PND_DEBUG_DIR / f"pnd_client_{stamp}"
                self._debug_dir.mkdir(parents=True, exist_ok=True)

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            base = self._debug_dir / f"{stamp}_{kind}"
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
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            _LOGGER.warning("PND client debug dump saved: meta=%s body=%s", meta_path, body_path)
        except Exception as err:
            _LOGGER.warning("Unable to write PND debug dump for %s: %s", kind, err)

    def get_chart_data(
        self,
        id_device_set: str | int,
        interval_from: str,
        interval_to: str,
        id_assembly: int = -1001,
    ) -> Any:
        """Fetch PND chart data with the exact standalone probe sequence."""
        self.last_warmup_status_code = None
        self.last_warmup_url = None
        self.last_data_status_code = None
        self.last_data_url = None
        self._session_cookie_count = 0
        self._debug_dir = None

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
        login_url = f"{CAS_BASE_URL}/login?service={urllib.parse.quote(service_url)}"
        authorize_url = (
            f"{CAS_BASE_URL}/oidc/authorize"
            f"?scope={SCOPE}"
            f"&response_type={RESPONSE_TYPE}"
            f"&redirect_uri={urllib.parse.quote(CEZ_BASE_URL)}"
            f"&client_id={CEZ_CLIENT_ID}"
        )

        try:
            _LOGGER.warning("PND standalone client start")

            # 1) CAS login page
            response = self._request(session, "GET", login_url)
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

            # 2) CAS login submit
            response = self._request(
                session,
                "POST",
                form_action,
                data=form_payload,
                headers={"Origin": "https://cas.cez.cz", "Referer": login_url},
            )
            if looks_like_login_page(response):
                self._dump_response(response, kind="02_cas_login_submit_login_page")
                raise CezDistribuceAuthError("CAS login submit ended on login page")
            if response.status_code >= 400:
                self._dump_response(response, kind="02_cas_login_submit_error")
                raise CezDistribuceNetworkError(
                    f"CAS login submit failed: HTTP {response.status_code} at {response.url}"
                )

            # 3) CAS authorize
            response = self._request(session, "GET", authorize_url)
            if looks_like_login_page(response):
                self._dump_response(response, kind="03_cas_authorize_login_page")
                raise CezDistribuceAuthError("CAS authorize ended on login page")
            if response.status_code >= 400:
                self._dump_response(response, kind="03_cas_authorize_error")
                raise CezDistribuceNetworkError(
                    f"CAS authorize failed: HTTP {response.status_code} at {response.url}"
                )

            # 4) ČEZ token
            token_url = f"{CEZ_BASE_URL}/rest-auth-api?path=/token/get"
            response = self._request(session, "GET", token_url)
            if response.status_code >= 400:
                self._dump_response(response, kind="04_cez_token_error")
                raise CezDistribuceNetworkError(
                    f"ČEZ token request failed: HTTP {response.status_code} at {response.url}"
                )

            token = None
            try:
                token = extract_token(response.json())
            except Exception as err:
                _LOGGER.warning("PND standalone token JSON parse failed, continuing without token: %r", err)

            if token:
                session.headers.update({"X-Request-Token": token})

            # 5) PND warm-up. Keep probe behavior: dump response, but do not stop here.
            warmup_url = f"{PND_BASE_URL}/external/dashboard/view"
            response = self._request(session, "GET", warmup_url, headers=PND_WARMUP_HEADERS)
            self.last_warmup_status_code = response.status_code
            self.last_warmup_url = response.url
            self._session_cookie_count = len(session.cookies)
            self._dump_response(response, kind="05_pnd_warmup")
            _LOGGER.warning("PND warm-up response: status=%s url=%s", response.status_code, response.url)

            # 6) PND data POST
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
                headers=PND_DATA_HEADERS,
            )
            self.last_data_status_code = response.status_code
            self.last_data_url = response.url
            self._session_cookie_count = len(session.cookies)
            self._dump_response(response, kind="06_pnd_data", payload=pnd_payload)
            _LOGGER.warning(
                "PND data response: status=%s url=%s content_type=%s",
                response.status_code,
                response.url,
                response.headers.get("content-type"),
            )

            content_type = (response.headers.get("content-type") or "").lower()
            if response.status_code == 200 and "application/json" in content_type:
                _LOGGER.warning(
                    "PND standalone client success: warmup_status=%s data_status=%s debug_dir=%s",
                    self.last_warmup_status_code,
                    self.last_data_status_code,
                    self._debug_dir,
                )
                return response.json()

            if looks_like_login_page(response):
                raise CezDistribuceAuthError(
                    f"PND data request ended on login page: HTTP {response.status_code} at {response.url}"
                )

            if response.status_code in (401, 403):
                raise CezDistribuceAuthError(
                    f"PND data auth failed: HTTP {response.status_code} at {response.url}"
                )

            raise CezDistribuceNetworkError(
                f"PND data request failed: HTTP {response.status_code} at {response.url}; debug={self._debug_dir}"
            )
        finally:
            session.close()
