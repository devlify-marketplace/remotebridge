"""
Phase 9 - one new control-channel message.

Everything else Phase 9 adds (enrollment, policy fetch, session
reporting) happens over a separate HTTP side-channel to the admin
console (see admin_client.py) - it never touches the video/input/
control/audio sockets between host and viewer at all. The one thing
that *does* need a wire message is telling a connected viewer, in the
moment, that something they just tried is turned off by admin policy -
otherwise a viewer whose file transfer silently does nothing has no way
to tell "that's blocked" apart from "that's broken".

An older viewer (Phase 7/8) that doesn't import this module will simply
never recognize MSG_POLICY_DENIED and ignore it, same forward-compatible
handling protocol_p7.py already relies on - the action is still blocked
host-side either way, this message is purely a courtesy notice.
"""

import json

MSG_POLICY_DENIED = 0x80


def pack_policy_denied(action: str, reason: str) -> bytes:
    """action is a short machine-readable tag (e.g. "file_transfer",
    "clipboard", "chat", "whiteboard", "printing", "voice", "session")
    naming what was blocked; reason is the human-readable string an
    admin wrote (or a default) suitable for showing directly to the
    viewer."""
    return json.dumps({"action": action, "reason": reason}).encode("utf-8")


def unpack_policy_denied(payload: bytes) -> dict:
    """Returns {"action": str, "reason": str}."""
    return json.loads(payload.decode("utf-8"))
