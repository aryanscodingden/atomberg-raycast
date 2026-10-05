#!/usr/bin/env python3
"""Control Atomberg smart fans from the command line.

Credentials come from ~/.config/atomberg/config.json:
    {"api_key": "...", "refresh_token": "..."}
or the env vars ATOMBERG_API_KEY / ATOMBERG_REFRESH_TOKEN.

Get both from the Atomberg Home app: Profile -> Developer Options
(or https://developer.atomberg-iot.com/).
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "https://api.developer.atomberg-iot.com"
CONF_DIR = os.path.expanduser("~/.config/atomberg")
CONF_FILE = os.path.join(CONF_DIR, "config.json")
CACHE_FILE = os.path.join(CONF_DIR, "cache.json")

SPEEDS = range(1, 7)
TIMERS = {0: "off", 1: "1h", 2: "2h", 3: "3h", 4: "6h"}


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default if default is not None else {}


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)


def creds():
    conf = load_json(CONF_FILE)
    key = os.environ.get("ATOMBERG_API_KEY") or conf.get("api_key")
    tok = os.environ.get("ATOMBERG_REFRESH_TOKEN") or conf.get("refresh_token")
    if not key or not tok:
        die(
            f"no credentials. Write {CONF_FILE} as "
            '{"api_key": "...", "refresh_token": "..."} '
            "(Atomberg app -> Profile -> Developer Options)"
        )
    return key, tok


def request(path, bearer, method="GET", body=None):
    api_key, _ = creds()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("X-API-Key", api_key)
    req.add_header("Authorization", f"Bearer {bearer}")
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if e.code == 403:
            detail += "  (is Developer Mode still enabled in the app?)"
        die(f"HTTP {e.code} on {path}: {detail}")
    except urllib.error.URLError as e:
        die(f"cannot reach Atomberg cloud: {e.reason}")


def jwt_exp(token):
    """Read the `exp` claim without verifying the signature."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))["exp"]
    except Exception:
        return 0


def access_token():
    """Return a cached access token, refreshing only when it has expired.

    The developer API is rate limited (~100 calls/day), so every avoidable
    round trip is cached on disk.
    """
    cache = load_json(CACHE_FILE)
    tok = cache.get("access_token")
    if tok and jwt_exp(tok) - 60 > time.time():
        return tok

    _, refresh = creds()
    resp = request("/v1/get_access_token", refresh)
    if resp.get("status") != "Success":
        die(f"token refresh failed: {resp.get('message')}")
    tok = resp["message"]["access_token"]
    cache["access_token"] = tok
    save_json(CACHE_FILE, cache)
    return tok


def api(path, method="GET", body=None):
    resp = request(path, access_token(), method, body)
    if resp.get("status") != "Success":
        die(f"{path} failed: {resp.get('message')}")
    return resp["message"]


def device_list(refresh=False):
    cache = load_json(CACHE_FILE)
    if not refresh and cache.get("devices"):
        return cache["devices"]
    devices = api("/v1/get_list_of_devices")["devices_list"]
    cache["devices"] = devices
    save_json(CACHE_FILE, cache)
    return devices


def resolve(name):
    """Match a device by id, exact name, or unique case-insensitive prefix."""
    devices = device_list()
    if not devices:
        die("no devices on this account")
    if not name:
        if len(devices) == 1:
            return devices[0]["device_id"]
        names = ", ".join(d.get("name", d["device_id"]) for d in devices)
        die(f"multiple devices, name one of: {names}")

    low = name.lower()
    for d in devices:
        if d["device_id"] == name or d.get("name", "").lower() == low:
            return d["device_id"]
    hits = [d for d in devices if d.get("name", "").lower().startswith(low)]
    if len(hits) == 1:
        return hits[0]["device_id"]
    if len(hits) > 1:
        die(f"'{name}' is ambiguous")
    die(f"no device matching '{name}'")


def states():
    return api("/v1/get_device_state?device_id=all")["device_state"]


def send(device, command):
    api("/v1/send_command", "POST", {"device_id": device, "command": command})
    pretty = ", ".join(f"{k}={v}" for k, v in command.items())
    print(f"sent {pretty}")


def show_state(target):
    by_id = {s["device_id"]: s for s in states()}
    for d in device_list():
        if target and d["device_id"] != target:
            continue
        s = by_id.get(d["device_id"], {})
        power = "on" if s.get("power") else "off"
        speed = s.get("last_recorded_speed", "?")
        extras = []
        if s.get("sleep_mode"):
            extras.append("sleep")
        if s.get("led"):
            extras.append("led")
        if s.get("timer_hours"):
            extras.append(f"timer {s['timer_hours']}h")
        tail = f"  [{', '.join(extras)}]" if extras else ""
        print(f"{d.get('name', d['device_id']):<20} {power:<4} speed {speed}{tail}")


USAGE = """usage: atomberg.py <command> [device]

  devices              list fans on the account (--refresh to re-fetch)
  state   [device]     show power / speed / mode
  on      [device]
  off     [device]
  speed N [device]     N = 1..6
  sleep on|off [device]
  light on|off [device]
  timer N [device]     N = 0(off) 1 2 3 4(=6h)
  raw '<json>' [device]   e.g. raw '{"brightness": 50}'
  logout               delete the cached token, device list and saved keys
                       (--cache-only keeps the keys, -f skips the prompt)

Device may be omitted when the account has a single fan."""


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return

    cmd, args = argv[0], argv[1:]

    if cmd == "devices":
        for d in device_list(refresh="--refresh" in args):
            print(f"{d.get('name', '?'):<20} {d['device_id']}  {d.get('series', '')}")
        return

    if cmd == "state":
        show_state(resolve(args[0]) if args else None)
        return

    if cmd in ("on", "off"):
        send(resolve(args[0] if args else None), {"power": cmd == "on"})
        return

    if cmd == "speed":
        if not args or not args[0].isdigit() or int(args[0]) not in SPEEDS:
            die("speed takes 1-6")
        send(resolve(args[1] if len(args) > 1 else None), {"speed": int(args[0])})
        return

    if cmd in ("sleep", "light"):
        if not args or args[0] not in ("on", "off"):
            die(f"{cmd} takes on|off")
        key = "sleep" if cmd == "sleep" else "led"
        send(resolve(args[1] if len(args) > 1 else None), {key: args[0] == "on"})
        return

    if cmd == "timer":
        if not args or not args[0].isdigit() or int(args[0]) not in TIMERS:
            die(f"timer takes {'/'.join(f'{k}={v}' for k, v in TIMERS.items())}")
        send(resolve(args[1] if len(args) > 1 else None), {"timer": int(args[0])})
        return

    if cmd == "logout":
        targets = [CACHE_FILE] if "--cache-only" in args else [CACHE_FILE, CONF_FILE]
        present = [p for p in targets if os.path.exists(p)]
        if not present:
            print("nothing stored")
            return
        if "-f" not in args and "--force" not in args:
            for p in present:
                print(f"will delete {p}")
            if input("continue? [y/N] ").strip().lower() not in ("y", "yes"):
                print("cancelled")
                return
        for p in present:
            os.remove(p)
            print(f"deleted {p}")
        if CONF_FILE in present:
            print(
                "\nYour keys are gone locally. To revoke them at Atomberg's end,\n"
                "turn off Developer Options in the Atomberg Home app."
            )
        return

    if cmd == "raw":
        if not args:
            die("raw needs a JSON object")
        try:
            command = json.loads(args[0])
        except ValueError as e:
            die(f"bad JSON: {e}")
        send(resolve(args[1] if len(args) > 1 else None), command)
        return

    die(f"unknown command '{cmd}'\n\n{USAGE}")


if __name__ == "__main__":
    main(sys.argv[1:])
