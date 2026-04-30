from __future__ import annotations

import getpass
import json
import urllib.parse

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
    if "code" in query:
        query["code"] = ["<REDACTED>"]
    return urllib.parse.urlunparse(
        parsed._replace(query=urllib.parse.urlencode(query, doseq=True))
    )


def show_response(label: str, response: requests.Response, body: bool = False) -> None:
    print()
    print(f"=== {label} ===")
    print("status:", response.status_code)
    print("url:", safe_url(response.url))
    print("content-type:", response.headers.get("content-type"))
    print("history:", [(r.status_code, safe_url(r.url)) for r in response.history])
    if body:
        print("body_start:", response.text[:1000].replace("\n", "\\n"))


def is_expected_oauth_404(response: requests.Response) -> bool:
    parsed = urllib.parse.urlparse(response.url)
    query = urllib.parse.parse_qs(parsed.query)
    return (
        response.status_code == 404
        and response.url.startswith(BASE_URL)
        and "code" in query
    )


def print_json_shape(label: str, response: requests.Response) -> object | None:
    print()
    print(f"=== {label} JSON ===")
    try:
        payload = response.json()
    except ValueError:
        print("NOT JSON")
        print(response.text[:1000])
        return None

    if isinstance(payload, str):
        print(f"string len={len(payload)} start={payload[:8]!r}")
        return payload

    if isinstance(payload, dict):
        print("dict keys:", list(payload.keys()))
        redacted = {}
        for key, value in payload.items():
            if isinstance(value, str):
                redacted[key] = f"<str len={len(value)} start={value[:8]!r}>"
            else:
                redacted[key] = value
        print(json.dumps(redacted, ensure_ascii=False, indent=2)[:2000])
        return payload

    if isinstance(payload, list):
        print("list len:", len(payload))
        print(json.dumps(payload[:2], ensure_ascii=False, indent=2)[:2000])
        return payload

    print("type:", type(payload).__name__, payload)
    return payload


def extract_token(payload: object) -> str | None:
    if isinstance(payload, str):
        return payload.strip() or None

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


def main() -> None:
    username = input("Username/e-mail: ").strip()
    password = getpass.getpass("Password: ")

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

    session = requests.Session()
    session.max_redirects = 10

    response = session.get(login_url, timeout=TIMEOUT)
    show_response("CAS login page", response)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    execution_input = soup.find("input", {"name": "execution"})

    if not execution_input or not execution_input.get("value"):
        print("ERROR: execution input not found")
        show_response("CAS login page body", response, body=True)
        return

    response = session.post(
        login_url,
        data={
            "username": username,
            "password": password,
            "execution": execution_input["value"],
            "_eventId": "submit",
            "geolocation": "",
        },
        timeout=TIMEOUT,
    )
    show_response("CAS login submit", response, body=response.status_code >= 400)

    if response.status_code >= 400 and not is_expected_oauth_404(response):
        print("STOP: login submit failed")
        return

    response = session.get(authorize_url, timeout=TIMEOUT)
    show_response("CAS authorize", response, body=response.status_code >= 400)

    if response.status_code >= 400 and not is_expected_oauth_404(response):
        print("STOP: authorize failed")
        return

    token_url = f"{BASE_URL}/rest-auth-api?path=/token/get"
    response = session.get(token_url, timeout=TIMEOUT)
    show_response("Token endpoint", response, body=response.status_code >= 400)

    if response.status_code >= 400:
        print("STOP: token endpoint failed")
        return

    payload = print_json_shape("Token endpoint", response)
    token = extract_token(payload)

    if not token:
        print()
        print("RESULT: TOKEN NOT FOUND")
        return

    print()
    print(f"RESULT: TOKEN FOUND len={len(token)} start={token[:8]!r}")

    session.headers.update({"X-Request-Token": token})

    supply_url = (
        f"{BASE_URL}/vyhledani-om"
        "?path=/vyhledaniom/zakladniInfo/50/PREHLED_OM_CELEK"
    )
    response = session.post(
        supply_url,
        json={"nekontrolovatPrislusnostOM": False},
        timeout=TIMEOUT,
    )
    show_response("Supply points endpoint", response, body=response.status_code >= 400)

    if response.status_code >= 400:
        print("STOP: supply points endpoint failed")
        return

    print_json_shape("Supply points endpoint", response)

    print()
    print("DONE")


if __name__ == "__main__":
    main()