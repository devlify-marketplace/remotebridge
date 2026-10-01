"""Relay authentication tokens (Phase 13) - must match server/relay.py's make_token exactly.

Duplicated rather than imported because the admin console and the relay deploy separately (the
console to Render, the relay to a VPS). tests/test_relay_auth.py asserts the two agree.
"""
import hashlib
import hmac
import os

_TOKEN_CONTEXT = b"remotebridge-relay-v1:"


def make_token(secret: str, device_id: str) -> str:
    return hmac.new(secret.encode("utf-8"), _TOKEN_CONTEXT + device_id.encode("utf-8"),
                    hashlib.sha256).hexdigest()


_EXPIRING_TOKEN_CONTEXT = b"remotebridge-relay-v2:"


def make_expiring_token(secret: str, device_id: str, expires_at: int) -> str:
    """"<expiry>.<signature>" - must match server/relay.py's make_expiring_token exactly (a test asserts it)."""
    expires_at = int(expires_at)
    sig = hmac.new(secret.encode("utf-8"),
                   _EXPIRING_TOKEN_CONTEXT + device_id.encode("utf-8") + b":" + str(expires_at).encode("ascii"),
                   hashlib.sha256).hexdigest()
    return f"{expires_at}.{sig}"


def token_ttl() -> int:
    """$RELAY_TOKEN_TTL in seconds (a plain number, or a number with s/m/h/d). 0 / unset / invalid = the
    original non-expiring tokens. Opt-in on purpose: hosts that predate expiring tokens cannot refresh
    one, so turning this on before they are upgraded would lock them out when their token expires."""
    raw = os.environ.get("RELAY_TOKEN_TTL", "").strip().lower()
    if not raw:
        return 0
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(raw[-1:])
    digits = raw[:-1] if mult else raw
    if not digits.isdigit():
        return 0
    return int(digits) * (mult or 1)


def current_secret() -> str:
    """$RELAY_SECRET. If it lists several (comma-separated, as the relay accepts for rotation), the
    FIRST is the one new tokens are issued from."""
    return (os.environ.get("RELAY_SECRET", "").split(",")[0]).strip()


_REVOCATION_CONTEXT = b"remotebridge-relay-revocation-list-v1"


def revocation_bearer(secret: str) -> str:
    """What the relay presents (as a Bearer credential) when it polls /api/v1/relay/revoked. Must match
    server/relay.py's revocation_bearer; tests/test_relay_revocation.py asserts they agree."""
    return hmac.new(secret.encode("utf-8"), _REVOCATION_CONTEXT, hashlib.sha256).hexdigest()


def all_secrets() -> list:
    """Every secret in $RELAY_SECRET (comma-separated during a rotation)."""
    return [x.strip() for x in os.environ.get("RELAY_SECRET", "").split(",") if x.strip()]
