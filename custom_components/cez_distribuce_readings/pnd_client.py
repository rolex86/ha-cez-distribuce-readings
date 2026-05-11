"""Isolated PND client that mirrors the working HA probe flow."""

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

from .api import (
    CezDistribuceAuthError,
    CezDistribuceNetworkError,
    CezDistribuceUnexpectedResponseError,
)
from .const import (
    CAS_BASE_URL,
    CEZ_DISTRIBUCE_BASE_URL,
    CEZ_DISTRIBUCE_CLIENT_ID,
    CLIENT_NAME,
    PND_BASE_URL,
    RESPONSE_TYPE,
    SCOPE,
)

_LOGGER = logging.getLogger(__name__)

TIMEOUT = 30
PND_DEBUG_DIR = Path("/config/cez_distribuce_readings_debug")
PND_SESSION_HEADERS = {
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


class _LoginFormParser(HTMLParser):
    """Extract CAS form action and input fields without depending on portal DOM quirks."""

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


class CezPndClient:
    """Fresh isolated PND fetcher using the known-good probe request sequence."""

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
        self.last_warmup_status_code: int | None = None
        self.last_warmup_url: str | None = None
        self.last_data_status_code: int | None = None
        self.last_data_url: str | None = None
        self.session = requests.Session()
        self.session.max_redirects = 30
        self.session.headers.update(PND_SESSION_HEADERS)

        self.service_url = (
            f"{CAS_BASE_URL}/oauth2.0/callbackAuthorize"
            f"?client_id={self.client_id}"
            f"&redirect_uri={urllib.parse.quote(self.base_url)}"
            f"&response_type={RESPONSE_TYPE}"
            f"&client_name={CLIENT_NAME}"
        )
        self.login_url = f"{CAS_BASE_URL}/login?service={urllib.parse.quote(self.service_url)}"
        self.authorize_url = (
            f"{CAS_BASE_URL}/oidc/authorize"
            f"?scope={SCOPE}"
            f"&response_type={RESPONSE_TYPE}"
            f"&redirect_uri={urllib.parse.quote(self.base_url)}"
            f"&client_id={self.client_id}"
        )

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Execute one low-level request without reusing the main integration helpers."""
        try:
            return self.session.request(method, url, timeout=TIMEOUT, **kwargs)
        except requests.Timeout as err:
            raise CezDistribuceNetworkError(f"PND request timed out for {url}") from err
        except requests.RequestException as err:
            raise CezDistribuceNetworkError(f"PND request failed for {url}") from err

    def _is_expected_oauth_redirect_404(self, response: requests.Response) -> bool:
        """Return true for the expected CAS callback 404 that still yields valid cookies."""
        if response.status_code != 404:
            return False

        parsed = urllib.parse.urlparse(response.url)
        query = urllib.parse.parse_qs(parsed.query)
        return response.url.startswith(self.base_url) and "code" in query

    def _sanitize_url(self, value: str | None) -> str | None:
        """Mask sensitive OAuth query values before persisting debug dumps."""
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

    def _safe_headers(self, headers: Any) -> dict[str, str]:
        """Hide sensitive header values in debug output."""
        result: dict[str, str] = {}

        for key, value in dict(headers or {}).items():
            lowered = str(key).lower()
            if lowered in {"cookie", "authorization", "set-cookie", "x-request-token"}:
                result[str(key)] = "***"
            else:
                result[str(key)] = str(value)

        return result

    def _dump_response(
        self,
        kind: str,
        response: requests.Response,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Persist failed warm-up/data responses for HA-side comparison with the probe."""
        try:
            PND_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            base = PND_DEBUG_DIR / f"{stamp}_{kind}"
            content_type = response.headers.get("content-type", "")
            meta = {
                "captured_at": datetime.now().isoformat(),
                "kind": kind,
                "status_code": response.status_code,
                "final_url": self._sanitize_url(response.url),
                "content_type": content_type,
                "redirect_history": [
                    {
                        "status_code": item.status_code,
                        "url": self._sanitize_url(item.url),
                        "location": self._sanitize_url(item.headers.get("location")),
                    }
                    for item in response.history
                ],
                "request": {
                    "method": response.request.method if response.request else None,
                    "url": self._sanitize_url(response.request.url) if response.request else None,
                    "headers": self._safe_headers(response.request.headers) if response.request else {},
                },
                "session_cookie_count": len(self.session.cookies),
                "response_cookie_count": len(response.cookies),
                "response_headers": self._safe_headers(response.headers),
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

    def _parse_login_form(self, html_text: str) -> tuple[str, dict[str, str]]:
        """Parse the CAS login form exactly like the working HA probe."""
        parser = _LoginFormParser()
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
                    "action": self.login_url,
                    "inputs": parser.global_inputs,
                }
            else:
                raise CezDistribuceAuthError("CAS login form does not contain execution token")

        action = html.unescape(str(selected_form.get("action") or self.login_url))
        form_action = urllib.parse.urljoin(self.login_url, action)
        payload = dict(selected_form.get("inputs") or {})
        payload.update(
            {
                "username": self.username,
                "password": self.password,
                "_eventId": "submit",
                "geolocation": payload.get("geolocation", ""),
            }
        )
        return form_action, payload

    def _looks_like_login_page(self, response: requests.Response) -> bool:
        """Detect CAS/login HTML instead of the expected PND or token content."""
        url = response.url.lower()
        if "cas.cez.cz" in url and "/login" in url:
            return True

        text = response.text[:5000].lower()
        return (
            'name="execution"' in text
            or 'id="fm1"' in text
            or "<title>login" in text
        )

    def _extract_token(self, payload: Any) -> str | None:
        """Extract X-Request-Token from known token payload shapes."""
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
                    nested = self._extract_token(value)
                    if nested:
                        return nested

        return None

    def _prepare_authenticated_session(self) -> None:
        """Run the exact CAS + token sequence that works in the HA probe."""
        response = self._request("GET", self.login_url)
        try:
            response.raise_for_status()
        except requests.RequestException as err:
            raise CezDistribuceNetworkError("Unable to load CAS login page") from err

        form_action, form_payload = self._parse_login_form(response.text)

        response = self._request(
            "POST",
            form_action,
            data=form_payload,
            headers={
                "Origin": "https://cas.cez.cz",
                "Referer": self.login_url,
            },
        )
        if not self._is_expected_oauth_redirect_404(response):
            try:
                response.raise_for_status()
            except requests.RequestException as err:
                raise CezDistribuceAuthError(
                    f"CAS login submit failed with HTTP {response.status_code}"
                ) from err

        response = self._request("GET", self.authorize_url)
        if not self._is_expected_oauth_redirect_404(response):
            try:
                response.raise_for_status()
            except requests.RequestException as err:
                raise CezDistribuceAuthError(
                    f"CAS authorize failed with HTTP {response.status_code}"
                ) from err

        token_url = f"{self.base_url}/rest-auth-api?path=/token/get"
        response = self._request("GET", token_url)
        try:
            token_payload = response.json()
        except ValueError:
            _LOGGER.warning("PND token response is not JSON, continuing without X-Request-Token")
            return

        token = self._extract_token(token_payload)
        if token:
            self.session.headers.update({"X-Request-Token": token})

    def get_chart_data(
        self,
        id_device_set: str | int,
        interval_from: str,
        interval_to: str,
        id_assembly: int = -1001,
    ) -> tuple[Any, dict[str, Any]]:
        """Fetch one PND chart payload using the exact working probe sequence."""
        self._prepare_authenticated_session()

        warmup_url = f"{PND_BASE_URL}/external/dashboard/view"
        warmup_response = self._request("GET", warmup_url, headers=PND_WARMUP_HEADERS)
        self.last_warmup_status_code = warmup_response.status_code
        self.last_warmup_url = warmup_response.url
        _LOGGER.warning(
            "PND warm-up response: status=%s url=%s",
            warmup_response.status_code,
            warmup_response.url,
        )
        if warmup_response.status_code >= 400:
            self._dump_response("pnd_warmup", warmup_response)
        if warmup_response.status_code in (401, 403) or self._looks_like_login_page(warmup_response):
            raise CezDistribuceAuthError(
                f"PND warm-up failed: HTTP {warmup_response.status_code} at {warmup_response.url}"
            )

        payload = {
            "format": "chart",
            "idAssembly": id_assembly,
            "idDeviceSet": str(id_device_set),
            "intervalFrom": interval_from,
            "intervalTo": interval_to,
            "compareFrom": None,
            "opmId": None,
            "electrometerId": None,
        }
        data_url = f"{PND_BASE_URL}/external/data"
        data_response = self._request(
            "POST",
            data_url,
            json=payload,
            headers=PND_DATA_HEADERS,
        )
        self.last_data_status_code = data_response.status_code
        self.last_data_url = data_response.url
        _LOGGER.warning(
            "PND data response: status=%s url=%s content_type=%s",
            data_response.status_code,
            data_response.url,
            data_response.headers.get("content-type"),
        )
        if data_response.status_code >= 400:
            self._dump_response("pnd_data", data_response, payload=payload)
        if data_response.status_code in (401, 403) or self._looks_like_login_page(data_response):
            raise CezDistribuceAuthError(
                f"PND data auth failed: HTTP {data_response.status_code} at {data_response.url}"
            )
        if data_response.status_code >= 400:
            raise CezDistribuceNetworkError(
                f"PND data request failed: HTTP {data_response.status_code} at {data_response.url}"
            )

        try:
            raw_payload = data_response.json()
        except ValueError as err:
            raise CezDistribuceAuthError(
                "PND data response is not JSON: "
                f"HTTP {data_response.status_code}, content-type={data_response.headers.get('content-type')}"
            ) from err

        if isinstance(raw_payload, dict):
            status_code = raw_payload.get("statusCode")
            if status_code in (401, 403):
                raise CezDistribuceAuthError(
                    f"PND data auth failed: statusCode {status_code} at {data_response.url}"
                )
            if status_code not in (None, 200):
                raise CezDistribuceUnexpectedResponseError(
                    f"PND data response returned statusCode={status_code}"
                )
            if "data" in raw_payload:
                raw_payload = raw_payload["data"]

        return raw_payload, {
            "warmup_status_code": warmup_response.status_code,
            "warmup_url": warmup_response.url,
            "data_status_code": data_response.status_code,
            "data_url": data_response.url,
        }
