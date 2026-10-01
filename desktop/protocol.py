"""
Phase 1 - shared wire protocol.

Every message on the wire is: [1-byte type][4-byte big-endian length][payload].
Video frames and input events use the same framing so both channels
(video socket and input socket) can reuse the same send/recv helpers.

Mouse coordinates are sent as normalized floats in [0.0, 1.0], relative
to the host's screen (or the viewer's displayed frame, which is the
same thing) - NOT raw pixels. That means the viewer's window can be
any size and the host can be any resolution; the host just multiplies
by its own screen width/height to get real pixel coordinates. No
resolution negotiation needed.
"""

import json
import struct

MSG_VIDEO_FRAME = 0x01
MSG_MOUSE_MOVE = 0x02
MSG_MOUSE_CLICK = 0x03
MSG_MOUSE_SCROLL = 0x04
MSG_KEY_EVENT = 0x05

# Phase 2 - auth handshake, sent once on the video channel before any
# video frames or input events flow.
MSG_AUTH_REQUEST = 0x10
MSG_AUTH_RESPONSE = 0x11

_HEADER_FMT = ">BI"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)

_BUTTON_TO_BYTE = {"left": 0, "right": 1, "middle": 2}
_BYTE_TO_BUTTON = {v: k for k, v in _BUTTON_TO_BYTE.items()}


def send_message(sock, msg_type: int, payload: bytes = b"") -> None:
    sock.sendall(struct.pack(_HEADER_FMT, msg_type, len(payload)) + payload)


def _recv_exact(sock, num_bytes: int) -> bytes:
    chunks = []
    remaining = num_bytes
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("Connection closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_message(sock):
    """Returns (msg_type: int, payload: bytes)."""
    header = _recv_exact(sock, _HEADER_SIZE)
    msg_type, length = struct.unpack(_HEADER_FMT, header)
    payload = _recv_exact(sock, length) if length else b""
    return msg_type, payload


# --- Mouse move -------------------------------------------------------

def pack_mouse_move(x_norm: float, y_norm: float) -> bytes:
    return struct.pack(">ff", x_norm, y_norm)


def unpack_mouse_move(payload: bytes):
    return struct.unpack(">ff", payload)  # (x_norm, y_norm)


# --- Mouse click --------------------------------------------------------

def pack_mouse_click(x_norm: float, y_norm: float, button: str, pressed: bool) -> bytes:
    return struct.pack(">ffBB", x_norm, y_norm, _BUTTON_TO_BYTE[button], int(pressed))


def unpack_mouse_click(payload: bytes):
    x, y, button_byte, pressed = struct.unpack(">ffBB", payload)
    return x, y, _BYTE_TO_BUTTON[button_byte], bool(pressed)  # (x, y, button, pressed)


# --- Mouse scroll -------------------------------------------------------

def pack_mouse_scroll(dx: float, dy: float) -> bytes:
    return struct.pack(">ff", dx, dy)


def unpack_mouse_scroll(payload: bytes):
    return struct.unpack(">ff", payload)  # (dx, dy)


# --- Keyboard -------------------------------------------------------------
# key_name is a string: a single printable character ("a", "5", "@"),
# or a pynput special-key name ("space", "enter", "backspace", "shift",
# "ctrl_l", "esc", "f1", ...). Host maps this string back to a pynput key.

def pack_key_event(key_name: str, pressed: bool) -> bytes:
    name_bytes = key_name.encode("utf-8")
    return struct.pack(">B", int(pressed)) + name_bytes


def unpack_key_event(payload: bytes):
    pressed = bool(payload[0])
    key_name = payload[1:].decode("utf-8")
    return key_name, pressed  # (key_name, pressed)


# --- Auth handshake (Phase 2) ------------------------------------------
# JSON payloads - small, infrequent (once per session), so framing
# efficiency doesn't matter here the way it does for video/input.

def pack_auth_request(viewer_id: str, password: str = "", totp_code: str = "") -> bytes:
    return json.dumps({
        "viewer_id": viewer_id,
        "password": password,
        "totp_code": totp_code,
    }).encode("utf-8")


def unpack_auth_request(payload: bytes) -> dict:
    return json.loads(payload.decode("utf-8"))


def pack_auth_response(approved: bool, reason: str = "") -> bytes:
    return json.dumps({"approved": approved, "reason": reason}).encode("utf-8")


def unpack_auth_response(payload: bytes) -> dict:
    return json.loads(payload.decode("utf-8"))


# --- Clipboard sync (Phase 3) -------------------------------------------
# Sent over the control channel. Text payloads are just UTF-8 bytes;
# images are raw PNG bytes. No JSON wrapper needed since there's
# nothing but the content itself to carry.

MSG_CLIPBOARD_TEXT = 0x20
MSG_CLIPBOARD_IMAGE = 0x21


def pack_clipboard_text(text: str) -> bytes:
    return text.encode("utf-8")


def unpack_clipboard_text(payload: bytes) -> str:
    return payload.decode("utf-8")


def pack_clipboard_image(png_bytes: bytes) -> bytes:
    return png_bytes


def unpack_clipboard_image(payload: bytes) -> bytes:
    return payload


# --- File transfer & remote browsing (Phase 3) --------------------------
# Also sent over the control channel. Metadata messages are JSON;
# MSG_FILE_CHUNK is binary-framed (transfer_id + offset header, then
# raw file bytes) since chunks are the hot path and JSON-encoding
# binary data would be wasteful.

MSG_FILE_LIST_REQUEST = 0x30
MSG_FILE_LIST_RESPONSE = 0x31
MSG_FILE_SEND_REQUEST = 0x32
MSG_FILE_SEND_ACCEPT = 0x33
MSG_FILE_CHUNK = 0x34
MSG_FILE_COMPLETE = 0x35
MSG_FILE_PULL_REQUEST = 0x36

_CHUNK_HEADER_FMT = ">IQ"
_CHUNK_HEADER_SIZE = struct.calcsize(_CHUNK_HEADER_FMT)


def pack_file_list_request(directory: str) -> bytes:
    return json.dumps({"dir": directory}).encode("utf-8")


def unpack_file_list_request(payload: bytes) -> dict:
    return json.loads(payload.decode("utf-8"))


def pack_file_list_response(directory: str, entries: list) -> bytes:
    return json.dumps({"dir": directory, "entries": entries}).encode("utf-8")


def unpack_file_list_response(payload: bytes) -> dict:
    return json.loads(payload.decode("utf-8"))


def pack_file_send_request(transfer_id: int, filename: str, size: int) -> bytes:
    return json.dumps({"transfer_id": transfer_id, "filename": filename, "size": size}).encode("utf-8")


def unpack_file_send_request(payload: bytes) -> dict:
    return json.loads(payload.decode("utf-8"))


def pack_file_send_accept(transfer_id: int, accepted: bool, resume_offset: int, reason: str = "") -> bytes:
    return json.dumps({
        "transfer_id": transfer_id,
        "accepted": accepted,
        "resume_offset": resume_offset,
        "reason": reason,
    }).encode("utf-8")


def unpack_file_send_accept(payload: bytes) -> dict:
    return json.loads(payload.decode("utf-8"))


def pack_file_chunk(transfer_id: int, offset: int, data: bytes) -> bytes:
    return struct.pack(_CHUNK_HEADER_FMT, transfer_id, offset) + data


def unpack_file_chunk(payload: bytes):
    transfer_id, offset = struct.unpack(_CHUNK_HEADER_FMT, payload[:_CHUNK_HEADER_SIZE])
    return transfer_id, offset, payload[_CHUNK_HEADER_SIZE:]  # (transfer_id, offset, data)


def pack_file_complete(transfer_id: int, ok: bool, message: str = "") -> bytes:
    return json.dumps({"transfer_id": transfer_id, "ok": ok, "message": message}).encode("utf-8")


def unpack_file_complete(payload: bytes) -> dict:
    return json.loads(payload.decode("utf-8"))


def pack_file_pull_request(path: str) -> bytes:
    return json.dumps({"path": path}).encode("utf-8")


def unpack_file_pull_request(payload: bytes) -> dict:
    return json.loads(payload.decode("utf-8"))


# --- Adaptive bitrate feedback (Phase 4) ---------------------------------
# Sent by the viewer over the control channel, roughly once a second
# (see adaptive.StatsReporter), reporting what it actually received so
# the host can react. This is a lagging, viewer-measured signal - not a
# bandwidth probe - but it's enough to notice when the viewer can't
# keep up and back off, or when things are comfortable and it's safe
# to raise quality/fps again.

MSG_STATS_REPORT = 0x40


def pack_stats_report(measured_fps: float, measured_kbps: float) -> bytes:
    return struct.pack(">ff", measured_fps, measured_kbps)


def unpack_stats_report(payload: bytes):
    return struct.unpack(">ff", payload)  # (measured_fps, measured_kbps)


# --- Multi-monitor support (Phase 4) -------------------------------------
# Also over the control channel. The host enumerates its own monitors;
# the viewer picks one to switch capture to. mss's own numbering (index
# 0 = virtual bounding box of everything, 1.. = individual monitors) is
# kept as-is so it lines up with what host.py's mss.monitors returns.

MSG_MONITOR_LIST_REQUEST = 0x50
MSG_MONITOR_LIST_RESPONSE = 0x51
MSG_MONITOR_SWITCH = 0x52


def pack_monitor_list_request() -> bytes:
    return b""


def pack_monitor_list_response(monitors: list, active_index: int) -> bytes:
    return json.dumps({"monitors": monitors, "active": active_index}).encode("utf-8")


def unpack_monitor_list_response(payload: bytes) -> dict:
    return json.loads(payload.decode("utf-8"))


def pack_monitor_switch(index: int) -> bytes:
    return json.dumps({"index": index}).encode("utf-8")


def unpack_monitor_switch(payload: bytes) -> dict:
    return json.loads(payload.decode("utf-8"))
