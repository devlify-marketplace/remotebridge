#!/usr/bin/env python3
"""Mint a relay token for a device by hand (for a host that doesn't use the admin console).

    python3 relay_token.py --secret "$RELAY_SECRET" --id office-pc
    python3 relay_token.py --secret "$RELAY_SECRET" --id office-pc --ttl 30d    # expires in 30 days

Give the printed value to the host as --relay-token (or $REMOTEBRIDGE_RELAY_TOKEN). Hosts enrolled with
the admin console never need this - they fetch their own token (and, when the console sets RELAY_TOKEN_TTL,
refresh it on their own). A token minted by hand cannot be refreshed: the host stops being able to register
when it expires, so mint a new one before then. Without --ttl the token never expires, and a relay running
with --require-expiry will refuse it.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import time  # noqa: E402
from relay import make_expiring_token, make_token  # noqa: E402

def parse_ttl(text: str) -> int:
    """'90' / '90s' / '15m' / '12h' / '30d' -> seconds. Exits with a clear message on anything else."""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    text = text.strip().lower()
    mult = units.get(text[-1:], None)
    digits = text[:-1] if mult else text
    if not digits.isdigit() or int(digits) <= 0:
        sys.exit(f"bad --ttl {text!r}: use seconds or a number with s/m/h/d, e.g. 12h")
    return int(digits) * (mult or 1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--secret", default=os.environ.get("RELAY_SECRET", ""), help="the relay's secret (default $RELAY_SECRET)")
    ap.add_argument("--id", required=True, help="the device ID the host registers as")
    ap.add_argument("--ttl", help="make the token expire after this long: seconds, or a number with s/m/h/d "
                                  "(e.g. 12h, 30d). Default: never expires")
    a = ap.parse_args()
    secret = a.secret.split(",")[0].strip()
    if not secret:
        sys.exit("no secret: pass --secret or set RELAY_SECRET")
    if a.ttl:
        print(make_expiring_token(secret, a.id, int(time.time()) + parse_ttl(a.ttl)))
    else:
        print(make_token(secret, a.id))
