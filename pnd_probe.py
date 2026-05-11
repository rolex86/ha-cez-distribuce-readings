import json
import os
import sys
import urllib.parse
from datetime import datetime

import requests
from bs4 import BeautifulSoup


CAS_BASE_URL = "https://cas.cez.cz/cas"
CEZ_BASE_URL = "https://dip.cezdistribuce.cz/irj/portal"
PND_BASE_URL = "https://pnd.cezdistribuce.cz/cezpnd2"

CEZ_CLIENT_ID = "fjR3ZL9zrtsNcDQF.onpremise.dip.sap.dipcezdistribucecz.prod"
CLIENT_NAME = "CasOAuthClient"
RESPONSE_TYPE = "code"
SCOPE = "openid"

TIMEOUT = 30


def log_response(label, response):
    print(f"\n=== {label} ===")
    print("status:", response.status_code)
    print("url:", response.url)
    print("content-type:", response.headers.get("content-type"))
    print("history:")
    for item in response.history:
        print(" ", item.status_code, item.url)


def save_body(filename, response):
    mode = "wb"
    with open(filename, mode) as f:
        f.write(response.content)
    print(f"saved body: {filename}")


def parse_login_form(html, login_url, username, password):
    soup = BeautifulSoup(html, "html.parser")
    execution_input = soup.find("input", {"name": "execution"})
    if not execution_input or not execution_input.get("value"):
        raise RuntimeError("CAS login page does not contain execution token")

    form = execution_input.find_parent("form") or soup.find("form")
    form_action = login_url

    if form and form.get("action"):
        form_action = urllib.parse.urljoin(login_url, form.get("action"))

    payload = {}

    if form:
        for input_tag in form.find_all("input"):
            name = input_tag.get("name")
            if name:
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

    return form_action, payload


def expected_portal_callback_404(response):
    if response.status_code != 404:
        return False

    parsed = urllib.parse.urlparse(response.url)
    query = urllib.parse.parse_qs(parsed.query)

    return response.url.startswith(CEZ_BASE_URL) and "code" in query


def current_month_interval():
    now = datetime.now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)

    return start.strftime("%d.%m.%Y %H:%M"), end.strftime("%d.%m.%Y %H:%M")


def main():
    username = os.environ.get("CEZ_USER")
    password = os.environ.get("CEZ_PASS")
    device_set = os.environ.get("PND_DEVICE_SET", "92329")
    assembly = int(os.environ.get("PND_ASSEMBLY", "-1001"))

    interval_from, interval_to = current_month_interval()
    interval_from = os.environ.get("PND_FROM", interval_from)
    interval_to = os.environ.get("PND_TO", interval_to)

    if not username or not password:
        print("Missing CEZ_USER or CEZ_PASS env variable")
        sys.exit(2)

    session = requests.Session()
    session.max_redirects = 20
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

    print("\n### 1) CAS login page")
    r = session.get(login_url, timeout=TIMEOUT)
    log_response("CAS login page", r)
    save_body("01_cas_login_page.html", r)
    r.raise_for_status()

    form_action, form_payload = parse_login_form(
        r.text,
        login_url,
        username,
        password,
    )

    print("\n### 2) CAS login submit")
    r = session.post(
        form_action,
        data=form_payload,
        headers={
            "Origin": "https://cas.cez.cz",
            "Referer": login_url,
        },
        timeout=TIMEOUT,
    )
    log_response("CAS login submit", r)
    save_body("02_cas_login_submit.html", r)

    if not expected_portal_callback_404(r):
        r.raise_for_status()

    print("\n### 3) CAS authorize")
    r = session.get(authorize_url, timeout=TIMEOUT)
    log_response("CAS authorize", r)
    save_body("03_cas_authorize.html", r)

    if not expected_portal_callback_404(r):
        r.raise_for_status()

    print("\n### 4) Optional ČEZ token")
    token_url = f"{CEZ_BASE_URL}/rest-auth-api?path=/token/get"
    r = session.get(token_url, timeout=TIMEOUT)
    log_response("ČEZ token", r)
    save_body("04_cez_token.txt", r)

    try:
        token_payload = r.json()
        token = None
        if isinstance(token_payload, str):
            token = token_payload.strip()
        elif isinstance(token_payload, dict):
            for key in ("data", "token", "requestToken", "xRequestToken", "X-Request-Token"):
                value = token_payload.get(key)
                if isinstance(value, str) and value.strip():
                    token = value.strip()
                    break

        if token:
            session.headers.update({"X-Request-Token": token})
            print("X-Request-Token loaded")
        else:
            print("X-Request-Token not found, continuing")
    except Exception as err:
        print("Token parse failed, continuing:", repr(err))

    print("\n### 5) PND warm-up")
    warmup_url = f"{PND_BASE_URL}/external/dashboard/view"
    r = session.get(
        warmup_url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://pnd.cezdistribuce.cz/",
        },
        timeout=TIMEOUT,
    )
    log_response("PND warm-up", r)
    save_body("05_pnd_warmup.html", r)

    print("\n### 6) PND data POST")
    payload = {
        "format": "chart",
        "idAssembly": assembly,
        "idDeviceSet": str(device_set),
        "intervalFrom": interval_from,
        "intervalTo": interval_to,
        "compareFrom": None,
        "opmId": None,
        "electrometerId": None,
    }

    print("payload:")
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    r = session.post(
        f"{PND_BASE_URL}/external/data",
        json=payload,
        headers={
            "Origin": "https://pnd.cezdistribuce.cz",
            "Referer": "https://pnd.cezdistribuce.cz/cezpnd2/external/dashboard/view",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
        },
        timeout=TIMEOUT,
    )
    log_response("PND data", r)
    save_body("06_pnd_data_response.txt", r)

    print("\nbody preview:")
    print(r.text[:2000])

    if r.headers.get("content-type", "").lower().startswith("application/json"):
        try:
            data = r.json()
            print("\nJSON keys:", list(data.keys()) if isinstance(data, dict) else type(data).__name__)
            if isinstance(data, dict):
                print("unitY:", data.get("unitY"))
                print("series count:", len(data.get("series") or []))
        except Exception as err:
            print("JSON parse failed:", repr(err))


if __name__ == "__main__":
    main()