"""
Phase 7 - Extended protocol for multi-user sessions.

Adds to the existing protocol (protocol.py):
  1. View-only mode indication (sent with auth request/response)
  2. Session info broadcast (list of connected viewers, modes, durations)
  3. Viewer join/leave events (so all viewers know who else is in the session)
  4. Session permissions query (a viewer can ask "am I allowed to send input?")

These are built on top of the existing message types; the core framing
and video/input/file/clipboard logic remains unchanged.
"""

import json
import struct

# --- Auth extensions (Phase 7) ------------------------------------------
# The MSG_AUTH_REQUEST and MSG_AUTH_RESPONSE now include view_mode field.

# View mode: 0 = control (has input), 1 = view_only (screen only)
VIEW_MODE_CONTROL = 0
VIEW_MODE_VIEW_ONLY = 1

# These replace (or extend) the Phase 2 auth pack functions:

def pack_auth_request_p7(viewer_id: str, view_mode: int, password: str = "", totp_code: str = "") -> bytes:
    """
    Extended auth request with view_mode.
    view_mode: 0 = control, 1 = view_only
    """
    return json.dumps({
        "viewer_id": viewer_id,
        "view_mode": view_mode,
        "password": password,
        "totp_code": totp_code,
    }).encode("utf-8")


def unpack_auth_request_p7(payload: bytes) -> dict:
    """Returns dict with 'viewer_id', 'view_mode', 'password', 'totp_code'."""
    data = json.loads(payload.decode("utf-8"))
    # Backward compatible: if view_mode is missing, assume control
    if "view_mode" not in data:
        data["view_mode"] = VIEW_MODE_CONTROL
    return data


def pack_auth_response_p7(approved: bool, view_mode: int, reason: str = "") -> bytes:
    """
    Extended auth response echoing the view_mode for confirmation.
    """
    return json.dumps({
        "approved": approved,
        "view_mode": view_mode,
        "reason": reason,
    }).encode("utf-8")


def unpack_auth_response_p7(payload: bytes) -> dict:
    """Returns dict with 'approved', 'view_mode', 'reason'."""
    data = json.loads(payload.decode("utf-8"))
    if "view_mode" not in data:
        data["view_mode"] = VIEW_MODE_CONTROL
    return data


# --- Session info broadcast (Phase 7) -----------------------------------
# After auth, the host sends a snapshot of who's in the session.
# All viewers receive this when they join, and again whenever someone
# joins/leaves.

MSG_SESSION_INFO = 0x60
MSG_VIEWER_JOINED = 0x61
MSG_VIEWER_LEFT = 0x62


def pack_session_info(viewers_list: list) -> bytes:
    """
    viewers_list: [
      {
        "viewer_id": "alice",
        "view_mode": 0,  # 0=control, 1=view_only
        "address": "192.168.1.5:54321",
        "connected_duration": 123.45,  # seconds
      },
      ...
    ]
    """
    return json.dumps({"viewers": viewers_list}).encode("utf-8")


def unpack_session_info(payload: bytes) -> dict:
    """Returns {"viewers": [list of viewer info]}."""
    return json.loads(payload.decode("utf-8"))


def pack_viewer_joined(viewer_id: str, view_mode: int, address: str) -> bytes:
    """Broadcast when a new viewer joins."""
    return json.dumps({
        "viewer_id": viewer_id,
        "view_mode": view_mode,
        "address": address,
    }).encode("utf-8")


def unpack_viewer_joined(payload: bytes) -> dict:
    """Returns {"viewer_id", "view_mode", "address"}."""
    return json.loads(payload.decode("utf-8"))


def pack_viewer_left(viewer_id: str) -> bytes:
    """Broadcast when a viewer disconnects."""
    return json.dumps({"viewer_id": viewer_id}).encode("utf-8")


def unpack_viewer_left(payload: bytes) -> dict:
    """Returns {"viewer_id"}."""
    return json.loads(payload.decode("utf-8"))


# --- Permission query (Phase 7) ------------------------------------------
# A viewer can ask the host "am I allowed to send input?" to handle
# the case where it reconnects after the control viewer has changed.

MSG_PERMISSIONS_QUERY = 0x63
MSG_PERMISSIONS_RESPONSE = 0x64


def pack_permissions_query() -> bytes:
    """Viewer asks: can I send input?"""
    return b""


def pack_permissions_response(can_send_input: bool, reason: str = "") -> bytes:
    """Host responds: yes/no and why."""
    return json.dumps({
        "can_send_input": can_send_input,
        "reason": reason,
    }).encode("utf-8")


def unpack_permissions_response(payload: bytes) -> dict:
    """Returns {"can_send_input": bool, "reason": str}."""
    return json.loads(payload.decode("utf-8"))


# --- Backward compatibility ------------------------------------------
# The old phase 2 functions are still available for viewers that don't
# know about phase 7 yet. Host always responds with the extended format
# so new viewers can join alongside old ones (old viewers just ignore
# extra fields in the response).

import protocol as proto

# Alias the old names to the new ones for forward compatibility
# (Host will use the new pack functions; viewers can use old ones
#  and still be understood since unpack_auth_request_p7 is backward-compatible)
