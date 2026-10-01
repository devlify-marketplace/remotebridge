"""
Phase 2 - authentication and access control.

Everything a host needs to decide whether an incoming connection is
allowed to proceed, plus the config file that stores its settings:

  - Session confirmation prompt: if the connection isn't auto-approved,
    the host operator is asked at the console (with a timeout that
    defaults to reject, so an unattended host doesn't accidentally
    stay open forever waiting for a human who isn't there).
  - Password-protected unattended access: if a password is configured
    and the viewer supplies the correct one, the session is approved
    without a prompt. Passwords are never stored in plaintext - only
    a salted PBKDF2 hash lives in host_config.json.
  - Two-factor authentication: an optional second factor (TOTP, e.g.
    Google Authenticator) layered on top of the unattended password.
    2FA requires a password to already be set - it's a second factor,
    not a replacement for one.
  - Whitelisting: an optional list of allowed viewer IDs. Empty list =
    no whitelist restriction (any ID may attempt a connection, subject
    to the checks above).

None of this requires a server or user accounts - it's all local,
per-host config, matching the CLI host/viewer model from Phases 0-1.
"""

import base64
import hashlib
import json
import os
import queue
import secrets
import threading

CONFIG_PATH = "host_config.json"

DEFAULT_CONFIG = {
    "unattended_password_hash": None,
    "unattended_password_salt": None,
    "totp_secret": None,
    "totp_enabled": False,
    "whitelist": [],  # empty = allow any viewer ID (still needs a prompt or password)
    "confirmation_timeout_seconds": 20,
}


# --- Config -----------------------------------------------------------

def load_config(path: str = CONFIG_PATH) -> dict:
    if not os.path.exists(path):
        return dict(DEFAULT_CONFIG)
    with open(path, "r") as f:
        data = json.load(f)
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)
    return merged


def save_config(config: dict, path: str = CONFIG_PATH) -> None:
    with open(path, "w") as f:
        json.dump(config, f, indent=2)


# --- Password hashing (PBKDF2-HMAC-SHA256, never store plaintext) -----

def hash_password(password: str, salt: bytes = None) -> tuple:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return base64.b64encode(salt).decode(), base64.b64encode(digest).decode()


def verify_password(password: str, salt_b64: str, hash_b64: str) -> bool:
    if not password or not salt_b64 or not hash_b64:
        return False
    salt = base64.b64decode(salt_b64)
    expected = base64.b64decode(hash_b64)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return secrets.compare_digest(digest, expected)


# --- TOTP two-factor -----------------------------------------------------

def generate_totp_secret() -> str:
    import pyotp
    return pyotp.random_base32()


def verify_totp(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    import pyotp
    return pyotp.TOTP(secret).verify(code, valid_window=1)


# --- Whitelist ----------------------------------------------------------

def is_whitelisted(viewer_id: str, whitelist: list) -> bool:
    if not whitelist:
        return True
    return viewer_id in whitelist


# --- Console confirmation prompt (with timeout, defaults to reject) ---

def prompt_accept(viewer_id: str, addr, timeout: int) -> bool:
    print(f"\n[host] Incoming connection from {addr} (id: {viewer_id})")
    answer_q = queue.Queue()

    def ask():
        try:
            ans = input(f"[host] Accept this connection? [y/N] (auto-reject in {timeout}s): ")
        except EOFError:
            ans = ""
        answer_q.put(ans)

    t = threading.Thread(target=ask, daemon=True)
    t.start()
    try:
        ans = answer_q.get(timeout=timeout)
    except queue.Empty:
        print("[host] No response - auto-rejecting.")
        return False
    return ans.strip().lower() in ("y", "yes")


# --- Failed-login throttling ----------------------------------------------

class AuthThrottle:
    """Backoff lockout for failed password/2FA attempts.

    Failures are counted under THREE keys at once, because no single key is
    trustworthy on its own:
      - the viewer's network address (host part only). Through a relay every
        viewer shows up with the relay's address, so relay-mode callers pass
        addr=None and this key is skipped (see decide_host_auth's
        throttle_addr) - otherwise one attacker would lock everyone out.
      - the claimed viewer ID (the attacker picks this, so rotating IDs defeats
        it alone - but it stops a guesser hammering one identity).
      - one global bucket with a much higher threshold, which bounds the total
        guess rate no matter how the attacker rotates the other two.
    A key locks after `free_attempts` consecutive failures; the lock then
    doubles per further failure (base_delay, 2x, 4x ...) up to max_delay.
    A success clears that viewer's address and ID keys, never the global one.
    """

    GLOBAL_KEY = ("global", "*")

    def __init__(self, free_attempts: int = 5, base_delay: float = 30.0,
                 max_delay: float = 3600.0, global_free_attempts: int = 50,
                 clock=None):
        import time as _time
        self.free_attempts = free_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.global_free_attempts = global_free_attempts
        self._clock = clock or _time.monotonic
        self._lock = threading.Lock()
        self._state = {}  # key -> [consecutive_failures, locked_until]

    @staticmethod
    def _host_part(addr) -> str:
        a = str(addr or "unknown")
        if a.startswith("[") and "]" in a:          # [::1]:5000
            return a[1:a.index("]")]
        if a.count(":") == 1:                       # 1.2.3.4:5000
            return a.split(":")[0]
        return a                                    # bare host or bare IPv6

    def _keys(self, viewer_id, addr) -> list:
        # addr=None means "the address is not the viewer's" (relay mode: it is
        # the relay's, shared by everyone) - skip that key rather than let one
        # attacker lock out every legitimate viewer behind the same relay.
        keys = [("id", (viewer_id or "unknown").strip().lower() or "unknown"),
                self.GLOBAL_KEY]
        if addr is not None:
            keys.insert(0, ("addr", self._host_part(addr)))
        return keys

    def _free_for(self, key) -> int:
        return self.global_free_attempts if key == self.GLOBAL_KEY else self.free_attempts

    def retry_after(self, viewer_id, addr) -> float:
        """Seconds until an attempt is allowed again; 0 if not locked."""
        now = self._clock()
        with self._lock:
            waits = [self._state[k][1] - now for k in self._keys(viewer_id, addr)
                     if k in self._state]
        return max([w for w in waits if w > 0], default=0.0)

    def record_failure(self, viewer_id, addr) -> None:
        now = self._clock()
        with self._lock:
            for key in self._keys(viewer_id, addr):
                fails, _ = self._state.get(key, [0, 0.0])
                fails += 1
                over = fails - self._free_for(key)
                locked_until = 0.0
                if over >= 0:
                    locked_until = now + min(self.base_delay * (2 ** over), self.max_delay)
                self._state[key] = [fails, locked_until]

    def record_success(self, viewer_id, addr) -> None:
        with self._lock:
            for key in self._keys(viewer_id, addr):
                if key != self.GLOBAL_KEY:
                    self._state.pop(key, None)


# One throttle per host process; decide_host_auth uses it unless a caller
# (tests, or a host wanting different limits) passes its own.
DEFAULT_THROTTLE = AuthThrottle()


# --- Full handshake, host side ------------------------------------------

def perform_host_auth(conn, config: dict, addr) -> dict:
    """
    Runs the auth handshake on the host side of the already-TLS-wrapped
    video channel. Reads one MSG_AUTH_REQUEST and decides whether to
    approve it. Does NOT send the response - the caller does that so it
    can log the outcome first.

    Returns:
        {"approved": bool, "viewer_id": str, "decision": str, "reason": str}

    decision is one of:
        auto_unattended       - password (+2FA if enabled) verified, no prompt
        manual_accept         - operator accepted at the console
        rejected_whitelist    - viewer ID not on the whitelist
        rejected_password     - unattended password supplied but wrong
        rejected_totp         - 2FA enabled, code missing or wrong
        rejected_manual       - operator declined or the prompt timed out
        protocol_error        - didn't get a well-formed auth request
    """
    import protocol as proto

    try:
        msg_type, payload = proto.recv_message(conn)
        if msg_type != proto.MSG_AUTH_REQUEST:
            return {"approved": False, "viewer_id": "unknown",
                    "decision": "protocol_error", "reason": "expected an auth request first"}
        req = proto.unpack_auth_request(payload)
    except (ValueError, KeyError, ConnectionError, OSError):
        return {"approved": False, "viewer_id": "unknown",
                "decision": "protocol_error", "reason": "malformed auth request"}

    return decide_host_auth(req.get("viewer_id"), req.get("password", ""),
                             req.get("totp_code", ""), config, addr)


def decide_host_auth(viewer_id, password: str, totp_code: str, config: dict, addr,
                      preauth_check=None, throttle=None,
                      throttle_addr="__use_addr__") -> dict:
    """
    The decision half of perform_host_auth, for a caller that has ALREADY
    read and parsed the auth request off the socket (host_p7 and later do -
    they need the request's view_mode, which the Phase 2 wire format didn't
    carry, so they parse it themselves). Those hosts used to then call
    perform_host_auth, which tried to read a *second* auth request from a
    socket the viewer had only sent one to - a deadlock on a real
    connection (a fake socket that returns EOF made it look like a
    "malformed auth request" instead, which is how it hid behind tests
    that mocked this function out). Calling this instead reads nothing.

    preauth_check, if given, is called with the (normalized) viewer_id at
    exactly one point: after unattended-password checks have had their
    say and before falling back to a blocking console prompt. Returning
    True approves the connection as auto_preauthorized. It's a hook
    rather than logic living here because what counts as "preauthorized"
    (Phase 11: a one-time ID the admin console issued) is the caller's
    business - this module stays free of any admin-console knowledge.

    Same return shape and same decision values as perform_host_auth, plus:
        auto_preauthorized    - preauth_check approved it
        rejected_throttled    - too many recent failures; nothing was checked
                                (no password verification, no console prompt)

    throttle: an AuthThrottle (defaults to the process-wide one).
    throttle_addr: address to throttle on; pass None in relay mode, where
    `addr` is the relay's and says nothing about the viewer. Only failed
    password/2FA attempts count; a whitelist miss or a declined console prompt
    does not, since neither is a guessing attack on a secret.
    """
    viewer_id = (viewer_id or "unknown").strip() or "unknown"
    password = password or ""
    totp_code = totp_code or ""
    if throttle is None:
        throttle = DEFAULT_THROTTLE
    if throttle_addr == "__use_addr__":
        throttle_addr = addr

    wait = throttle.retry_after(viewer_id, throttle_addr)
    if wait > 0:
        return {"approved": False, "viewer_id": viewer_id, "decision": "rejected_throttled",
                "reason": f"too many failed attempts; try again in {int(wait) + 1} seconds"}

    if not is_whitelisted(viewer_id, config.get("whitelist", [])):
        return {"approved": False, "viewer_id": viewer_id,
                "decision": "rejected_whitelist", "reason": "viewer ID is not on the whitelist"}

    pw_hash = config.get("unattended_password_hash")
    pw_salt = config.get("unattended_password_salt")

    if pw_hash and pw_salt and password:
        if not verify_password(password, pw_salt, pw_hash):
            throttle.record_failure(viewer_id, throttle_addr)
            return {"approved": False, "viewer_id": viewer_id,
                    "decision": "rejected_password", "reason": "incorrect unattended-access password"}
        if config.get("totp_enabled"):
            if not verify_totp(config.get("totp_secret", ""), totp_code):
                throttle.record_failure(viewer_id, throttle_addr)
                return {"approved": False, "viewer_id": viewer_id,
                        "decision": "rejected_totp", "reason": "missing or incorrect 2FA code"}
        throttle.record_success(viewer_id, throttle_addr)
        return {"approved": True, "viewer_id": viewer_id,
                "decision": "auto_unattended", "reason": "unattended password (+2FA) verified"}

    if preauth_check is not None and preauth_check(viewer_id):
        return {"approved": True, "viewer_id": viewer_id,
                "decision": "auto_preauthorized",
                "reason": "one-time connection authorized via the admin console REST API"}

    # No valid unattended credentials supplied - fall back to asking
    # whoever is sitting at the host.
    timeout = config.get("confirmation_timeout_seconds", 20)
    if prompt_accept(viewer_id, addr, timeout):
        return {"approved": True, "viewer_id": viewer_id,
                "decision": "manual_accept", "reason": "accepted at host console"}
    return {"approved": False, "viewer_id": viewer_id,
            "decision": "rejected_manual", "reason": "declined or timed out at host console"}
