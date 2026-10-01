"""
Phase 10 - one new control-channel message.

Branding (org display name + support URL) lives on the admin console,
not on each host's command line - see admin/store.py's set_branding and
desktop/admin_client.py's fetch_branding. The host is the one enrolled
with the console, so it's the host that knows its org's branding; the
viewer only finds out because the host tells it, right after auth, the
same way Phase 7's MSG_SESSION_INFO works. A viewer might connect to
hosts from different orgs across different sessions, so branding has to
travel with the session, not live as a viewer-side setting.

An older viewer (Phase 9 or earlier) that doesn't import this module
simply never recognizes MSG_BRANDING and ignores it - same
forward-compatible handling as protocol_p9.py's MSG_POLICY_DENIED. It
just keeps showing its own default window title instead of the host
org's branding, nothing breaks.
"""

import json

MSG_BRANDING = 0x81


def pack_branding(display_name: str, support_url: str = "") -> bytes:
    return json.dumps({"display_name": display_name, "support_url": support_url}).encode("utf-8")


def unpack_branding(payload: bytes) -> dict:
    """Returns {"display_name": str, "support_url": str}."""
    data = json.loads(payload.decode("utf-8"))
    data.setdefault("support_url", "")
    return data
