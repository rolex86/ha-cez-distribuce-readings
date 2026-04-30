from __future__ import annotations

import getpass
import json
import re
import urllib.parse
from typing import Any

import requests
from bs4 import BeautifulSoup


CAS_BASE_URL = "https://cas.cez.cz/cas"
BASE_URL = "https://dip.cezdistribuce.cz/irj/portal"
CLIENT_ID = "fjR3ZL9zrtsNcDQF.onpremise.dip.sap.dipcezdistribucecz.prod"
CLIENT_NAME = "CasOAuthClient"
RESPONSE_TYPE = "code"
SCOPE = "openid"
TIMEOUT = 30


def safe_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("code", "ticket", "execution"):
        if key in query:
            query[key] = ["<REDACTED>"]
    return urllib.parse.urlunparse(
        parsed._replace(query=urllib.parse.urlencode(query, doseq=True))
    )


def mask_ean(value: str | None) -> str | None:
    if not value:
        return value
    text = str(value)
    if len(text) <= 8:
        return text
    return f"{text[:7]}***{text[-3:]}"


def show_response(label: str, response: requests.Response, body: bool = False) -> None:
    print()
    print(f"=== {label} ===")
    print("status:", response.status_code)
    print("url:", safe_url(response.url))
    print("content-type:", response.headers.get("content-type"))
    print("content-disposition:", response.headers.get("content-disposition"))
    print("history:", [(r.status_code, safe_url(r.url)) for r in response.history])
    if body:
        print("body_start:", response.text[:1200].replace("\n", "\\n"))


def is_expected_oauth_404(response: requests.Response) -> bool:
    parsed = urllib.parse.urlparse(response.url)
    query = urllib.parse.parse_qs(parsed.query)
    return (
        response.status_code == 404
        and response.url.startswith(BASE_URL)
        and "code" in query
    )


def build_urls() -> tuple[str, str]:
    service_url = (
        f"{CAS_BASE_URL}/oauth2.0/callbackAuthorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={urllib.parse.quote(BASE_URL)}"
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
        f"&redirect_uri={urllib.parse.quote(BASE_URL)}"
        f"&client_id={CLIENT_ID}"
    )

    return login_url, authorize_url


def login_form_payload(login_url: str, html: str, username: str, password: str) -> tuple[str, dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    execution_input = soup.find("input", {"name": "execution"})

    if not execution_input or not execution_input.get("value"):
        raise RuntimeError("CAS login form does not contain execution token")

    form = execution_input.find_parent("form") or soup.find("form")
    form_action = login_url

    if form and form.get("action"):
        form_action = urllib.parse.urljoin(login_url, form.get("action"))

    payload: dict[str, str] = {}

    if form:
        for input_tag in form.find_all("input"):
            name = input_tag.get("name")
            if not name:
                continue
            payload[name] = input_tag.get("value", "")

    payload.update(
        {
            "username": username,
            "password": password,
            "execution": execution_input["value"],
            "_eventId": "submit",
            "geolocation": payload.get("geolocation", ""),
        }
    )

    print()
    print("CAS form action:", safe_url(form_action))
    print("CAS form fields:", sorted(payload.keys()))

    return form_action, payload


def extract_token(payload: Any) -> str | None:
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
            "result",
            "value",
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


def safe_json_preview(value: Any, max_len: int = 3000) -> str:
    def scrub(obj: Any) -> Any:
        if isinstance(obj, str):
            text = obj.strip()
            if re.fullmatch(r"[A-Za-z0-9_\-]{24,}", text) or re.fullmatch(r"[a-fA-F0-9]{24,}", text):
                return f"<REDACTED_STR len={len(text)} start={text[:6]!r}>"
            return text
        if isinstance(obj, list):
            return [scrub(item) for item in obj[:5]] + ([f"<... {len(obj)-5} more>"] if len(obj) > 5 else [])
        if isinstance(obj, dict):
            result = {}
            for key, item in obj.items():
                key_l = str(key).lower()
                if "token" in key_l or "password" in key_l or "cookie" in key_l:
                    result[key] = "<REDACTED_BY_KEY>"
                elif "ean" in key_l and isinstance(item, str):
                    result[key] = mask_ean(item)
                else:
                    result[key] = scrub(item)
            return result
        return obj

    return json.dumps(scrub(value), ensure_ascii=False, indent=2)[:max_len]


def request_json(session: requests.Session, method: str, url: str, **kwargs: Any) -> Any:
    response = session.request(method, url, timeout=TIMEOUT, **kwargs)
    show_response(f"{method} {url}", response, body=response.status_code >= 400)
    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError:
        print("NOT JSON:")
        print(response.text[:1200])
        return None

    print("payload type:", type(payload).__name__)
    if isinstance(payload, dict):
        print("payload keys:", list(payload.keys()))
        if "statusCode" in payload:
            print("statusCode:", payload.get("statusCode"))
        if "flashMessages" in payload:
            print("flashMessages:", payload.get("flashMessages"))
    print(safe_json_preview(payload))
    return payload


def unwrap_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def find_uids(obj: Any) -> list[str]:
    result: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            uid = value.get("uid")
            if isinstance(uid, str) and uid and uid not in result:
                result.append(uid)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(obj)
    return result


def find_ean_candidates(obj: Any) -> list[str]:
    result: list[str] = []

    def add_candidate(value: Any) -> None:
        if value is None:
            return
        text = str(value).strip()
        digits = re.sub(r"\D", "", text)
        if 12 <= len(digits) <= 24 and digits not in result:
            result.append(digits)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_l = str(key).lower()
                if "ean" in key_l:
                    add_candidate(item)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(obj)
    return result


def main() -> None:
    print("ČEZ HDO/signals probe")
    username = input("Username/e-mail: ").strip()
    password = getpass.getpass("Password: ")

    session = requests.Session()
    session.max_redirects = 10
    session.headers.update(
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

    login_url, authorize_url = build_urls()

    response = session.get(login_url, timeout=TIMEOUT)
    show_response("CAS login page", response)
    response.raise_for_status()

    form_action, form_payload = login_form_payload(login_url, response.text, username, password)

    response = session.post(
        form_action,
        data=form_payload,
        headers={
            "Origin": "https://cas.cez.cz",
            "Referer": login_url,
        },
        timeout=TIMEOUT,
    )
    show_response("CAS login submit", response, body=response.status_code >= 400)

    if response.status_code >= 400 and not is_expected_oauth_404(response):
        print("STOP: CAS login failed")
        return

    response = session.get(authorize_url, timeout=TIMEOUT)
    show_response("CAS authorize", response, body=response.status_code >= 400)

    if response.status_code >= 400 and not is_expected_oauth_404(response):
        print("STOP: CAS authorize failed")
        return

    token_url = f"{BASE_URL}/rest-auth-api?path=/token/get"
    token_payload = request_json(session, "GET", token_url)
    token = extract_token(token_payload)

    if not token:
        print("STOP: token not found")
        return

    print()
    print(f"TOKEN FOUND len={len(token)} start={token[:6]!r}")
    session.headers.update(
        {
            "X-Request-Token": token,
            "Accept": "application/json, text/plain, */*",
        }
    )

    supply_url = (
        f"{BASE_URL}/vyhledani-om"
        "?path=/vyhledaniom/zakladniInfo/50/PREHLED_OM_CELEK"
    )
    supply_payload = request_json(
        session,
        "POST",
        supply_url,
        json={"nekontrolovatPrislusnostOM": False},
    )
    supply_data = unwrap_data(supply_payload)

    uids = find_uids(supply_data)
    eans = find_ean_candidates(supply_data)

    print()
    print("UID candidates:", [f"{uid[:8]}...{uid[-8:]}" for uid in uids])
    print("EAN candidates from supply list:", [mask_ean(ean) for ean in eans])

    for uid in uids[:3]:
        print()
        print("#" * 80)
        print(f"DETAIL FOR UID {uid[:8]}...{uid[-8:]}")
        print("#" * 80)

        detail_url = f"{BASE_URL}/prehled-om?path=supply-point-detail/{uid}"
        detail_payload = request_json(session, "GET", detail_url)
        detail_data = unwrap_data(detail_payload)

        detail_eans = find_ean_candidates(detail_data)
        print("EAN candidates from detail:", [mask_ean(ean) for ean in detail_eans])

        all_eans = []
        for ean in [*detail_eans, *eans]:
            if ean not in all_eans:
                all_eans.append(ean)

        for ean in all_eans[:5]:
            print()
            print("-" * 80)
            print("TEST SIGNALS FOR EAN", mask_ean(ean))
            print("-" * 80)

            signals_url = f"{BASE_URL}/prehled-om?path=supply-point-detail/signals/{ean}"
            request_json(session, "GET", signals_url)

            export_url = f"{BASE_URL}/prehled-uctu?path=dashboard/supply-point/signals/export/{ean}"
            export_response = session.get(export_url, timeout=TIMEOUT)
            show_response("Signals export endpoint", export_response, body=export_response.status_code >= 400)

            content_type = export_response.headers.get("content-type", "")
            if export_response.status_code < 400:
                if "json" in content_type:
                    try:
                        print(safe_json_preview(export_response.json()))
                    except ValueError:
                        print("Export says JSON but parse failed")
                else:
                    print("export body/content first 500 bytes:")
                    print(export_response.content[:500])

    print()
    print("DONE")


if __name__ == "__main__":
    main()