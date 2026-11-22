"""Local self-test for the Yandex Smart Home bridge.

Simulates what the Yandex platform does: OAuth code exchange, device list,
state query and (optionally) an action.

The target is hard-coded to the local loopback port the bridge listens on; this
script never contacts an arbitrary host and never follows redirects.

Run:
    BRIDGE_PASSWORD=... python selftest.py [--action on|off|pause|speed]
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8099"
ALLOWED_URLS = {
    f"{BASE}/auth/authorize",
    f"{BASE}/auth/token",
    f"{BASE}/v1.0/user/devices",
    f"{BASE}/v1.0/user/devices/query",
    f"{BASE}/v1.0/user/devices/action",
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN401, D102
        return None


def _call(url: str, data: dict | None = None, headers: dict | None = None):
    """Perform a request, refusing anything that is not one of the known local URLs."""
    if url not in ALLOWED_URLS:
        raise ValueError(f"refusing to call unexpected URL: {url}")
    payload = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=payload, headers=headers or {})
    opener = urllib.request.build_opener(NoRedirect)
    return opener.open(request)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=["on", "off", "pause", "speed"])
    args = parser.parse_args()

    password = os.environ.get("BRIDGE_PASSWORD", "")
    if not password:
        print("BRIDGE_PASSWORD is not set")
        return 2

    redirect = "https://example.org/cb"

    # 1. authorize -> code
    try:
        _call(
            f"{BASE}/auth/authorize",
            {"password": password, "redirect_uri": redirect, "state": "abc", "client_id": "test"},
        )
        print("authorize: no redirect returned")
        return 1
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location", "")
        if "code=" not in location:
            print(f"authorize failed: {exc.code} {location}")
            return 1
        code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["code"][0]
        print(f"authorize: ok (code {code[:8]}...)")

    # 2. token exchange
    token = json.load(_call(f"{BASE}/auth/token", {"grant_type": "authorization_code", "code": code}))
    access = token["access_token"]
    print(f"token: ok (expires_in {token.get('expires_in')})")

    headers = {"Authorization": f"Bearer {access}", "Content-Type": "application/json"}

    # 3. device list
    devices = json.load(_call(f"{BASE}/v1.0/user/devices", headers=headers))
    dev = devices["payload"]["devices"][0]
    print(f"devices: {len(devices['payload']['devices'])}, type={dev['type']}, name={dev['name']}")
    print(f"  capabilities: {[c['type'].split('.')[-1] for c in dev['capabilities']]}")
    print(f"  properties:   {[p['type'].split('.')[-1] for p in dev['properties']]}")

    # 4. query
    state = json.load(
        _post(f"{BASE}/v1.0/user/devices/query", {"devices": [{"id": dev["id"]}]}, headers)
    )
    caps = state["payload"]["devices"][0]["capabilities"]
    props = state["payload"]["devices"][0]["properties"]
    print("query:")
    for cap in caps:
        print(f"  {cap['state']['instance']} = {cap['state']['value']}")
    for prop in props:
        print(f"  {prop['state']['instance']} = {prop['state']['value']}")

    # 5. optional action
    if args.action:
        if args.action == "on":
            caps_in = [{"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": True}}]
        elif args.action == "off":
            caps_in = [{"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": False}}]
        elif args.action == "pause":
            caps_in = [{"type": "devices.capabilities.toggle", "state": {"instance": "pause", "value": True}}]
        else:
            caps_in = [{"type": "devices.capabilities.mode", "state": {"instance": "work_speed", "value": "turbo"}}]

        result = json.load(
            _post(
                f"{BASE}/v1.0/user/devices/action",
                {"payload": {"devices": [{"id": dev["id"], "capabilities": caps_in}]}},
                headers,
            )
        )
        for cap in result["payload"]["devices"][0]["capabilities"]:
            print(f"action {args.action}: {cap['state']['action_result']}")

    return 0


def _post(url: str, body: dict, headers: dict):
    if url not in ALLOWED_URLS:
        raise ValueError(f"refusing to call unexpected URL: {url}")
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    opener = urllib.request.build_opener(NoRedirect)
    return opener.open(request)


if __name__ == "__main__":
    raise SystemExit(main())
