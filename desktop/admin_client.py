"""
Phase 9 - host-side admin console client.

Talks to admin/server.py over plain HTTP+JSON (stdlib urllib only - the
admin console is the piece that took on the Flask dependency, not every
host). Everything here is opt-in and fails soft: a host started without
--admin-url never touches this module's network functions at all, and
one started *with* --admin-url that can't currently reach the console
keeps running on the last policy it successfully fetched (or the fully
permissive DEFAULT_POLICY if it has never reached one) rather than
refusing connections. An admin console being unreachable is an ops
problem to notice and fix, not a reason to lock every host in the
building out of unattended access at once.

DEFAULT_POLICY's shape must stay in sync with admin/policy.py's
group_to_policy() output - it's what a host enforces when Phase 9 isn't
configured at all, which needs to mean the same thing as the Default
group's permissive settings do on the console side.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ENROLLMENT_FILE = "admin_enrollment.json"
POLICY_REFRESH_SECONDS = 300

DEFAULT_POLICY = {
    "group": None,  # None = "no admin console configured", vs. a real group name once enrolled
    "allow_unattended_access": True,
    "allow_file_transfer": True,
    "allow_clipboard": True,
    "allow_chat": True,
    "allow_whiteboard": True,
    "allow_printing": True,
    "allow_voice": True,
    "require_view_only": False,
    "max_viewers": 10,
    "max_session_minutes": 0,
    "require_whitelist": False,
    "allowed_viewer_ids": [],
    "blocked_viewer_ids": [],
    "auto_update": True,
}

# Phase 10: branding shown before/without any admin console at all.
DEFAULT_BRANDING = {"display_name": "RemoteBridge", "support_url": ""}

DEPLOY_CONFIG_FILE = "deploy_config.json"


class AdminUnavailable(Exception):
    """Raised when the admin console can't be reached or returns an error.
    Callers generally catch this and fall back to a cached/default policy
    rather than letting it interrupt a session."""


def _request(admin_url: str, path: str, method: str = "GET", body: dict = None,
              token: str = None, timeout: float = 5.0) -> dict:
    url = admin_url.rstrip("/") + path
    # A console on a free hosting tier (e.g. Render) sleeps when idle and takes up to ~a minute to
    # wake. Hosts talking to one can raise the floor: REMOTEBRIDGE_ADMIN_TIMEOUT=60
    try:
        timeout = max(timeout, float(os.environ.get("REMOTEBRIDGE_ADMIN_TIMEOUT", "0")))
    except ValueError:
        pass
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", str(e))
        except Exception:
            detail = str(e)
        raise AdminUnavailable(f"{path} -> HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise AdminUnavailable(f"{path} -> {e}") from e


# --- Enrollment (once per host, then cached to disk) -----------------------

def load_enrollment(path: str = ENROLLMENT_FILE):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def load_deploy_config(path: str = DEPLOY_CONFIG_FILE) -> dict:
    """Phase 10: an IT-authored file (see deploy/deploy_config.template.json)
    dropped next to the host executable by a scripted/MSI install, so a
    silently-installed host knows its admin_url/enrollment_key without
    anyone typing CLI flags on each machine. Returns {} if absent - a
    manually-run host with no such file behaves exactly like Phase 9."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_enrollment(entry: dict, path: str = ENROLLMENT_FILE) -> None:
    with open(path, "w") as f:
        json.dump(entry, f, indent=2)


def enroll(admin_url: str, device_id: str, enrollment_key: str,
           display_name: str = None, timeout: float = 5.0) -> dict:
    """One-time call: registers this device_id with the console (or, if it's
    already enrolled, just gets its existing token back - enroll_device on
    the server side is idempotent). Returns {"device_id", "report_token",
    "group"}."""
    body = {"device_id": device_id, "enrollment_key": enrollment_key}
    if display_name:
        body["display_name"] = display_name
    return _request(admin_url, "/api/v1/enroll", method="POST", body=body, timeout=timeout)


def ensure_enrolled(admin_url: str, device_id: str, enrollment_key: str,
                     display_name: str = None, path: str = ENROLLMENT_FILE) -> dict:
    """Loads a cached enrollment for this admin_url/device_id if one exists;
    otherwise enrolls and caches the result. Raises AdminUnavailable if
    enrollment is needed but the console can't be reached - callers should
    treat that as fatal at startup (unlike a policy-fetch failure later,
    there's no reasonable fallback for "never got a report_token")."""
    cached = load_enrollment(path)
    if cached and cached.get("admin_url") == admin_url and cached.get("device_id") == device_id:
        return cached

    result = enroll(admin_url, device_id, enrollment_key, display_name, timeout=10.0)
    entry = {"admin_url": admin_url, "device_id": device_id,
              "report_token": result["report_token"], "group": result.get("group")}
    save_enrollment(entry, path)
    return entry


# --- Policy fetch / refresh --------------------------------------------

def fetch_policy(admin_url: str, report_token: str, client_version: str = None,
                  mac_address: str = None, timeout: float = 5.0) -> dict:
    path = "/api/v1/policy"
    params = {}
    if client_version:
        params["client_version"] = client_version
    if mac_address:
        params["mac_address"] = mac_address
    if params:
        path += "?" + "&".join(f"{k}={urllib.parse.quote(v)}" for k, v in params.items())
    return _request(admin_url, path, token=report_token, timeout=timeout)


# --- Phase 10: branding + release ------------------------------------------

def fetch_relay_token(admin_url: str, report_token: str, timeout: float = 5.0):
    """Phase 13: the token this device presents to an authenticating relay (see server/relay.py).
    Returns None when the console isn't configured for relay auth (an open relay needs none)."""
    return fetch_relay_token_info(admin_url, report_token, timeout)[0]


def fetch_relay_token_info(admin_url: str, report_token: str, timeout: float = 5.0) -> tuple:
    """Like fetch_relay_token, but returns (token, expires_in_seconds). expires_in is None for a token that
    doesn't expire, including from a console that predates expiring tokens (it sends no such field)."""
    reply = _request(admin_url, "/api/v1/relay-token", token=report_token, timeout=timeout)
    expires_in = reply.get("expires_in")
    if not isinstance(expires_in, (int, float)) or isinstance(expires_in, bool) or expires_in <= 0:
        expires_in = None
    return reply.get("token"), expires_in


def fetch_branding(admin_url: str, timeout: float = 5.0) -> dict:
    """Unauthenticated on purpose - see admin/server.py's api_branding. Falls
    back to DEFAULT_BRANDING (not an exception) since branding is cosmetic;
    a host or viewer should never fail to start over an unreachable console
    just to show the right name in a window title."""
    try:
        return _request(admin_url, "/api/v1/branding", timeout=timeout)
    except AdminUnavailable:
        return dict(DEFAULT_BRANDING)


def fetch_release(admin_url: str, report_token: str, timeout: float = 5.0):
    """Returns {} if the org has never published a release (server-side
    default), or raises AdminUnavailable on an actual connection/auth
    failure - callers should treat the latter the same as a policy-refresh
    failure (log it, keep going on whatever was last known)."""
    return _request(admin_url, "/api/v1/release", token=report_token, timeout=timeout)


# --- Phase 11: Wake-on-LAN self-reporting + one-time preauth check --------

def get_own_mac_address() -> str:
    """Best-effort: uuid.getnode() falls back to a randomly-generated
    number on a machine it can't read a real MAC from, indistinguishable
    from a real one at this layer - fine here, since the worst case is
    Wake-on-LAN just not working for that device (it already needs a
    correct MAC set manually in that case, same as any host that predates
    Phase 11 and never reports one at all)."""
    node = uuid.getnode()
    return ":".join(f"{(node >> (8 * i)) & 0xff:02x}" for i in reversed(range(6)))


def check_preauth(admin_url: str, report_token: str, viewer_id: str, timeout: float = 3.0) -> bool:
    """Fails closed and NEVER raises - unlike every other function in this
    module, a caller in the middle of deciding whether to approve a
    connection (see host_p11.py's perform_host_auth_with_preauth) must
    never be blocked or crashed by this; "couldn't check" and "checked,
    not preauthorized" have to look identical to that caller."""
    try:
        result = _request(admin_url, f"/api/v1/preauth/{urllib.parse.quote(viewer_id)}",
                           token=report_token, timeout=timeout)
        return bool(result.get("preauthorized"))
    except AdminUnavailable:
        return False


class PolicyState:
    """Thread-safe holder for "the policy this host currently enforces",
    refreshed in the background so a running host picks up admin changes
    without a restart. Starts at DEFAULT_POLICY (or an already-cached
    policy, if the caller has one) and only ever moves to a *newer
    successfully-fetched* policy - a transient network blip never resets
    a host back to permissive defaults out from under a live session."""

    def __init__(self, initial: dict = None):
        self._lock = threading.Lock()
        self._policy = dict(initial or DEFAULT_POLICY)

    def get(self) -> dict:
        with self._lock:
            return dict(self._policy)

    def _set(self, policy: dict) -> None:
        with self._lock:
            self._policy = dict(policy)

    def refresh_once(self, admin_url: str, report_token: str, client_version: str = None,
                      mac_address: str = None) -> bool:
        """Returns True on a successful fetch (and updates the held policy),
        False if the console couldn't be reached (held policy is unchanged).
        Phase 10: client_version, if given, is reported to the console on
        this same request (see admin/server.py's api_policy) so it shows up
        on the Devices page - no separate round-trip needed. Phase 11:
        mac_address likewise, for Wake-on-LAN."""
        try:
            policy = fetch_policy(admin_url, report_token, client_version=client_version,
                                   mac_address=mac_address)
        except AdminUnavailable as e:
            print(f"[admin_client] Could not refresh policy, keeping last known settings: {e}")
            return False
        self._set(policy)
        return True

    def run_refresh_loop(self, admin_url: str, report_token: str,
                          interval_seconds: int = POLICY_REFRESH_SECONDS,
                          client_version: str = None, mac_address: str = None) -> None:
        """Intended to run in its own daemon thread for the life of the host
        process. The first refresh happens immediately (via refresh_once at
        startup, before this loop starts) - this loop is just the repeat."""
        while True:
            time.sleep(interval_seconds)
            self.refresh_once(admin_url, report_token, client_version=client_version,
                               mac_address=mac_address)


# --- Session reporting (best-effort, never blocks the caller) -----------

def report_event(admin_url: str, report_token: str, event: str, **fields) -> None:
    """Synchronous; raises AdminUnavailable on failure. Most callers want
    report_event_background instead so a slow/unreachable console can
    never stall an actual remote-control session."""
    body = {"event": event, **fields}
    _request(admin_url, "/api/v1/events", method="POST", body=body, token=report_token, timeout=5.0)


def report_event_background(admin_url: str, report_token: str, event: str, **fields) -> None:
    """Fire-and-forget: spawns a daemon thread and swallows AdminUnavailable
    (logging it) so a host is never blocked on - or crashed by - admin
    reporting. Session events are attempt/start/end, one per viewer
    connection, so this is at most a handful of threads per session, not
    a per-frame cost."""

    def _run():
        try:
            report_event(admin_url, report_token, event, **fields)
        except AdminUnavailable as e:
            print(f"[admin_client] Could not report '{event}' event to admin console: {e}")

    threading.Thread(target=_run, daemon=True, name=f"admin-report-{event}").start()
