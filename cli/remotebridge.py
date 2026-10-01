#!/usr/bin/env python3
"""
Phase 11 - command-line client for the admin console's REST API.

A thin wrapper: every subcommand is one call (or a short poll loop) against
/api/v1/ops/ on the admin console (see ../admin/server.py). It exists so a
shell script, cron job, or another system can do these things without a
person in the admin console's web UI:

    remotebridge.py devices                      list enrolled devices
    remotebridge.py sessions <device>            pull a device's session history
    remotebridge.py wake <device> [--wait]       send a Wake-on-LAN packet
    remotebridge.py wait <device>                block until the device checks in
    remotebridge.py connect <device> [--launch]  issue a one-time unattended connection

Standard library only - no pip install needed on the machine running it.

Configuration, highest priority first (so a secret never has to appear on a
command line, where it would show up in shell history and `ps`):
    1. --admin-url / --api-key flags
    2. REMOTEBRIDGE_ADMIN_URL / REMOTEBRIDGE_API_KEY environment variables
    3. a JSON file: $REMOTEBRIDGE_CONFIG, else ~/.remotebridge/cli_config.json
       {"admin_url": "http://...", "api_key": "dvfy_..."}

Exit codes, so scripts can branch on them:
    0  success
    1  the admin console rejected the request or couldn't be reached
    2  usage / configuration problem (nothing was sent)
    3  `wait` (or `wake --wait`) timed out
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

EXIT_OK, EXIT_API_ERROR, EXIT_USAGE, EXIT_TIMEOUT = 0, 1, 2, 3

DEFAULT_VIDEO_PORT = 5000


class CliError(Exception):
    def __init__(self, message: str, code: int = EXIT_API_ERROR):
        super().__init__(message)
        self.code = code


# --- configuration -----------------------------------------------------------

def _config_file_path() -> str:
    return os.environ.get("REMOTEBRIDGE_CONFIG") or os.path.join(
        os.path.expanduser("~"), ".remotebridge", "cli_config.json")


def load_settings(args) -> dict:
    file_cfg = {}
    try:
        with open(_config_file_path()) as f:
            file_cfg = json.load(f)
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as e:
        raise CliError(f"could not read {_config_file_path()}: {e}", EXIT_USAGE)

    admin_url = args.admin_url or os.environ.get("REMOTEBRIDGE_ADMIN_URL") or file_cfg.get("admin_url")
    api_key = args.api_key or os.environ.get("REMOTEBRIDGE_API_KEY") or file_cfg.get("api_key")
    if not admin_url:
        raise CliError("no admin console URL - pass --admin-url, set REMOTEBRIDGE_ADMIN_URL, "
                        f"or put admin_url in {_config_file_path()}", EXIT_USAGE)
    if not api_key:
        raise CliError("no API key - pass --api-key, set REMOTEBRIDGE_API_KEY, or put api_key in "
                        f"{_config_file_path()} (create one on the admin console's Operators page)",
                        EXIT_USAGE)
    return {"admin_url": admin_url.rstrip("/"), "api_key": api_key}


# --- HTTP ---------------------------------------------------------------------

def api(settings: dict, method: str, path: str, body: dict = None, timeout: float = 15.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(settings["admin_url"] + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {settings['api_key']}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("error", "")
        except Exception:
            detail = ""
        raise CliError(f"{method} {path} -> HTTP {e.code}" + (f": {detail}" if detail else ""))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise CliError(f"could not reach the admin console at {settings['admin_url']}: {e}")


def _q(device_id: str) -> str:
    return urllib.parse.quote(device_id, safe="")


# --- output helpers ------------------------------------------------------------

def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2))


def _table(rows: list, columns: list) -> None:
    """columns: [(header, key_or_callable), ...]"""
    def cell(row, spec):
        v = spec(row) if callable(spec) else row.get(spec)
        return "-" if v in (None, "") else str(v)

    table = [[h for h, _ in columns]] + [[cell(r, s) for _, s in columns] for r in rows]
    widths = [max(len(r[i]) for r in table) for i in range(len(columns))]
    for i, r in enumerate(table):
        print("  ".join(c.ljust(widths[j]) for j, c in enumerate(r)).rstrip())
        if i == 0:
            print("  ".join("-" * w for w in widths))


# --- subcommands ---------------------------------------------------------------

def cmd_devices(settings, args) -> int:
    devices = api(settings, "GET", "/api/v1/ops/devices")
    if args.json:
        _print_json(devices)
    else:
        _table(devices, [("DEVICE", "device_id"), ("GROUP", "group_name"), ("VERSION", "current_version"),
                          ("MAC", "mac_address"), ("LAST SEEN", lambda d: (d.get("last_seen_at") or "")[:19].replace("T", " ")),
                          ("FROM", "last_seen_address")])
    return EXIT_OK


def cmd_sessions(settings, args) -> int:
    events = api(settings, "GET", f"/api/v1/ops/devices/{_q(args.device)}/sessions?limit={args.limit}")
    if args.json:
        _print_json(events)
    else:
        _table(events, [("WHEN", lambda e: (e.get("timestamp") or "")[:19].replace("T", " ")),
                         ("EVENT", "event"), ("VIEWER", "viewer_id"), ("DECISION", "decision"),
                         ("ADDRESS", "address"), ("SECS", "duration_seconds")])
    return EXIT_OK


def _last_seen(settings, device_id: str):
    for d in api(settings, "GET", "/api/v1/ops/devices"):
        if d["device_id"] == device_id:
            return d.get("last_seen_at")
    raise CliError(f"no such device: {device_id}")


def _wait_for_checkin(settings, device_id: str, baseline, timeout: float, interval: float) -> int:
    """Blocks until the device's last_seen_at moves past `baseline`. Both values come from the admin
    console itself, so clock differences between this machine and the console don't matter. A host
    checks in as soon as it starts (its policy fetch is the first thing it does), which is what makes
    this a reasonable "it's back up" signal after a Wake-on-LAN packet."""
    def parse(s):
        return datetime.fromisoformat(s) if s else None

    base = parse(baseline)
    deadline = time.time() + timeout
    while time.time() < deadline:
        seen = parse(_last_seen(settings, device_id))
        if seen is not None and (base is None or seen > base):
            return EXIT_OK
        time.sleep(interval)
    return EXIT_TIMEOUT


def cmd_wake(settings, args) -> int:
    baseline = _last_seen(settings, args.device) if args.wait else None
    result = api(settings, "POST", f"/api/v1/ops/devices/{_q(args.device)}/wake", body={})
    print(f"Sent a wake packet to {args.device} ({result.get('mac_address')}).", file=sys.stderr)
    if not args.wait:
        if args.json:
            _print_json(result)
        return EXIT_OK

    print(f"Waiting up to {args.timeout:g}s for {args.device} to check in...", file=sys.stderr)
    code = _wait_for_checkin(settings, args.device, baseline, args.timeout, args.interval)
    print(f"{args.device} is back." if code == EXIT_OK else
          f"Timed out: {args.device} hasn't checked in.", file=sys.stderr)
    return code


def cmd_wait(settings, args) -> int:
    baseline = _last_seen(settings, args.device)
    code = _wait_for_checkin(settings, args.device, baseline, args.timeout, args.interval)
    print(f"{args.device} checked in." if code == EXIT_OK else
          f"Timed out: {args.device} hasn't checked in.", file=sys.stderr)
    return code


def _default_viewer_path() -> str:
    return os.environ.get("REMOTEBRIDGE_VIEWER") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "desktop", "viewer_p12.py")


def cmd_connect(settings, args) -> int:
    info = api(settings, "POST", f"/api/v1/ops/devices/{_q(args.device)}/connect",
               body={"ttl_seconds": args.ttl})

    address = args.host_address
    if not address and info.get("last_seen_address"):
        address = f"{info['last_seen_address']}:{DEFAULT_VIDEO_PORT}"
    if address and ":" not in address:
        address = f"{address}:{DEFAULT_VIDEO_PORT}"

    if args.json and not args.launch:
        _print_json({**info, "address": address})
        return EXIT_OK

    print(f"One-time viewer ID: {info['viewer_id']}   (valid until {info['expires_at'][:19].replace('T', ' ')} UTC, single use)",
          file=sys.stderr)

    if not address:
        if args.launch:
            raise CliError("can't launch: the console has no recorded address for this device - "
                           "pass --host-address host:port", EXIT_USAGE)
        print("No address on record for this device; add --host-address host:port for a ready-to-run command.",
              file=sys.stderr)
        print(info["viewer_id"])
        return EXIT_OK

    command = [sys.executable, os.path.abspath(args.viewer_path or _default_viewer_path()),
               address, "--id", info["viewer_id"]]

    if not args.launch:
        print("Connect with:  " + " ".join(command), file=sys.stderr)
        print(info["viewer_id"])
        return EXIT_OK

    if not os.path.exists(command[1]):
        raise CliError(f"viewer not found at {command[1]} - pass --viewer-path or set REMOTEBRIDGE_VIEWER",
                       EXIT_USAGE)
    print(f"Launching the viewer against {address}...", file=sys.stderr)
    return subprocess.call(command)


# --- entry point ---------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="remotebridge.py", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="Config: --admin-url/--api-key, REMOTEBRIDGE_ADMIN_URL/REMOTEBRIDGE_API_KEY, "
                                        "or ~/.remotebridge/cli_config.json.  Exit codes: 0 ok, 1 API error, "
                                        "2 usage/config, 3 timed out.")
    p.add_argument("--admin-url", help="admin console base URL, e.g. http://admin.example.org:8443")
    p.add_argument("--api-key", help="API key (prefer the env var or config file: flags show up in `ps`)")
    # Same two options again on every subcommand, so `remotebridge.py devices --api-key ...` works as well as
    # `remotebridge.py --api-key ... devices`. SUPPRESS keeps a subcommand that wasn't given the flag from
    # overwriting a value the main parser already captured.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--admin-url", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--api-key", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("devices", help="list enrolled devices", parents=[common])
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_devices)

    s = sub.add_parser("sessions", help="a device's session history (newest first)", parents=[common])
    s.add_argument("device")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_sessions)

    for name, helptext, func in (("wake", "send a Wake-on-LAN packet to a device", cmd_wake),
                                  ("wait", "block until a device next checks in with the console", cmd_wait)):
        s = sub.add_parser(name, help=helptext, parents=[common])
        s.add_argument("device")
        s.add_argument("--timeout", type=float, default=300.0, help="seconds to wait (default 300)")
        s.add_argument("--interval", type=float, default=5.0, help="seconds between polls (default 5)")
        if name == "wake":
            s.add_argument("--wait", action="store_true", help="after sending, wait for the device to check in")
            s.add_argument("--json", action="store_true")
        s.set_defaults(func=func)

    s = sub.add_parser("connect", help="issue a one-time viewer ID the host will accept without a prompt", parents=[common])
    s.add_argument("device")
    s.add_argument("--ttl", type=int, default=300, help="seconds the one-time ID stays valid (default 300)")
    s.add_argument("--host-address", help="host[:port] to connect to; default is the address the device "
                                            "last reached the console from (a hint - may be wrong behind NAT)")
    s.add_argument("--launch", action="store_true",
                   help="run viewer_p12.py against the host now (needs a display on this machine)")
    s.add_argument("--viewer-path", help="path to viewer_p12.py (default: ../desktop/, or $REMOTEBRIDGE_VIEWER)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_connect)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(load_settings(args), args)
    except CliError as e:
        print(f"remotebridge: {e}", file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
