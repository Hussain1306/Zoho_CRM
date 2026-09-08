"""
Zoho OAuth + CRM API client.

Credentials come from environment variables ONLY - never hardcoded,
never committed. Set these in your .env locally, and in Render's
dashboard (Environment tab) for the deployed version:

    ZOHO_CLIENT_ID
    ZOHO_CLIENT_SECRET
    ZOHO_REFRESH_TOKEN
    ZOHO_ACCOUNTS_URL   (default: https://accounts.zoho.com)
    ZOHO_API_DOMAIN     (default: https://www.zohoapis.com)

The access token is short-lived (~1hr) and we don't persist it -
we just fetch a fresh one at process start and refresh on demand.
"""

import os
import time
import httpx
from dotenv import load_dotenv

load_dotenv()

ZOHO_CLIENT_ID = os.environ["ZOHO_CLIENT_ID"]
ZOHO_CLIENT_SECRET = os.environ["ZOHO_CLIENT_SECRET"]
ZOHO_REFRESH_TOKEN = os.environ["ZOHO_REFRESH_TOKEN"]
ZOHO_ACCOUNTS_URL = os.environ.get("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.com")
ZOHO_API_DOMAIN = os.environ.get("ZOHO_API_DOMAIN", "https://www.zohoapis.com")

_token_cache = {"access_token": None, "expires_at": 0}


def get_access_token() -> str:
    """Return a valid access token, refreshing it if expired/missing."""
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    resp = httpx.post(
        f"{ZOHO_ACCOUNTS_URL}/oauth/v2/token",
        data={
            "grant_type": "refresh_token",
            "client_id": ZOHO_CLIENT_ID,
            "client_secret": ZOHO_CLIENT_SECRET,
            "refresh_token": ZOHO_REFRESH_TOKEN,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        # Zoho returns 200 with an error field sometimes, not just non-2xx
        raise RuntimeError(f"Zoho token refresh failed: {data}")

    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + data.get("expires_in", 3600)
    return _token_cache["access_token"]


def _headers() -> dict:
    return {"Authorization": f"Zoho-oauthtoken {get_access_token()}"}


def zoho_get(path: str, params: dict | None = None) -> dict:
    resp = httpx.get(f"{ZOHO_API_DOMAIN}{path}", headers=_headers(), params=params, timeout=20)
    resp.raise_for_status()
    if resp.status_code == 204 or not resp.text:
        # Zoho returns 204 No Content (empty body) for searches with zero
        # matches, instead of 200 with an empty data list. Treat that the
        # same as "found nothing" rather than crashing on json() parsing.
        return {}
    return resp.json()


def zoho_post(path: str, json_body: dict) -> dict:
    resp = httpx.post(f"{ZOHO_API_DOMAIN}{path}", headers=_headers(), json=json_body, timeout=20)
    # Zoho returns 201/200 with per-record success/error status inside the body -
    # don't assume HTTP 2xx means every record succeeded, check the body too.
    resp.raise_for_status()
    return resp.json()


def zoho_put(path: str, json_body: dict) -> dict:
    resp = httpx.put(f"{ZOHO_API_DOMAIN}{path}", headers=_headers(), json=json_body, timeout=20)
    resp.raise_for_status()
    return resp.json()


if __name__ == "__main__":
    # Quick connectivity smoke test - run this yourself once your .env is set up.
    # It hits the cheapest possible endpoint: listing CRM modules (read-only).
    token = get_access_token()
    print("Got access token:", token[:20] + "...")
    result = zoho_get("/crm/v2/settings/modules")
    module_names = [m["api_name"] for m in result.get("modules", [])]
    print(f"Found {len(module_names)} modules, including:", module_names[:10])
