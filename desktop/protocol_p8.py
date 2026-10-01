"""
Phase 8 - Protocol extensions for collaboration & support tools.

Adds four message families on top of the existing protocol (protocol.py)
and the Phase 7 multi-user extensions (protocol_p7.py):

  1. Text chat                (MSG_CHAT_TEXT)
  2. Whiteboard / annotation  (MSG_WHITEBOARD_STROKE, _CLEAR, _STATE)
  3. Remote printing          (MSG_PRINT_REQUEST, MSG_PRINT_RESPONSE)
  4. Voice chat                (MSG_VOICE_FRAME, MSG_VOICE_STATE)

All of these ride the existing control channel except voice, which gets
its own dedicated channel (like video/input/control before it) because
audio needs low, steady latency and shouldn't queue up behind file
transfers or whiteboard strokes.

These are additive: nothing here changes the Phase 7 message types, so a
Phase 7-only peer that ignores unknown message types keeps working (it
just won't see chat/whiteboard/print/voice traffic).
"""

import json
import struct
import time

# --- Text chat (Phase 8) -------------------------------------------------

MSG_CHAT_TEXT = 0x70


def pack_chat_message(sender_id: str, text: str, timestamp: float = None) -> bytes:
    """A single chat line, relayed by the host to every other viewer."""
    return json.dumps({
        "sender_id": sender_id,
        "text": text,
        "timestamp": timestamp if timestamp is not None else time.time(),
    }).encode("utf-8")


def unpack_chat_message(payload: bytes) -> dict:
    """Returns {"sender_id", "text", "timestamp"}."""
    return json.loads(payload.decode("utf-8"))


# --- Whiteboard / annotation overlay (Phase 8) ---------------------------
# A "stroke" is one continuous pen-down-to-pen-up drag. Points are sent
# as normalized (0.0-1.0) coordinates, same convention as mouse input in
# protocol.py, so they scale correctly regardless of viewer window size
# or host screen resolution.

MSG_WHITEBOARD_STROKE = 0x71
MSG_WHITEBOARD_CLEAR = 0x72
MSG_WHITEBOARD_STATE = 0x73  # full snapshot, sent to viewers as they join

STROKE_START = "start"
STROKE_POINT = "point"
STROKE_END = "end"


def pack_whiteboard_stroke(stroke_id: str, viewer_id: str, action: str,
                            x_norm: float, y_norm: float,
                            color: str = "#ff3b30", width: int = 3) -> bytes:
    """
    action: "start" (first point of a new stroke; carries color/width),
            "point" (an additional point on an in-progress stroke), or
            "end"   (pen lifted; x_norm/y_norm repeat the last point).
    """
    return json.dumps({
        "stroke_id": stroke_id,
        "viewer_id": viewer_id,
        "action": action,
        "x": x_norm,
        "y": y_norm,
        "color": color,
        "width": width,
    }).encode("utf-8")


def unpack_whiteboard_stroke(payload: bytes) -> dict:
    """Returns {"stroke_id", "viewer_id", "action", "x", "y", "color", "width"}."""
    return json.loads(payload.decode("utf-8"))


def pack_whiteboard_clear(viewer_id: str) -> bytes:
    """One viewer clears the whiteboard for everyone."""
    return json.dumps({"viewer_id": viewer_id}).encode("utf-8")


def unpack_whiteboard_clear(payload: bytes) -> dict:
    return json.loads(payload.decode("utf-8"))


def pack_whiteboard_state(strokes: list) -> bytes:
    """
    Full snapshot sent to a newly-joined viewer so they see what's already
    on the board.

    strokes: [
      {
        "stroke_id": "alice-3",
        "viewer_id": "alice",
        "color": "#ff3b30",
        "width": 3,
        "points": [[0.12, 0.30], [0.13, 0.31], ...],
      },
      ...
    ]
    """
    return json.dumps({"strokes": strokes}).encode("utf-8")


def unpack_whiteboard_state(payload: bytes) -> dict:
    """Returns {"strokes": [...]}."""
    return json.loads(payload.decode("utf-8"))


# --- Remote printing (Phase 8) --------------------------------------------
# Printing assumes the file has already been pushed to the host via the
# Phase 3 file-transfer channel (MSG_FILE_SEND_REQUEST/MSG_FILE_CHUNK) and
# lives in the host's configured download directory. The print request
# just names it; the host resolves and validates the path.

MSG_PRINT_REQUEST = 0x74
MSG_PRINT_RESPONSE = 0x75


def pack_print_request(filename: str, printer: str = "", copies: int = 1) -> bytes:
    """
    filename: name of a file already sitting in the host's download dir
              (no path separators - the host rejects anything else).
    printer:  printer name, or "" for the host's default printer.
    """
    return json.dumps({
        "filename": filename,
        "printer": printer,
        "copies": max(1, copies),
    }).encode("utf-8")


def unpack_print_request(payload: bytes) -> dict:
    """Returns {"filename", "printer", "copies"}."""
    return json.loads(payload.decode("utf-8"))


def pack_print_response(success: bool, message: str, job_id: str = "") -> bytes:
    return json.dumps({
        "success": success,
        "message": message,
        "job_id": job_id,
    }).encode("utf-8")


def unpack_print_response(payload: bytes) -> dict:
    """Returns {"success", "message", "job_id"}."""
    return json.loads(payload.decode("utf-8"))


# --- Voice chat (Phase 8) -------------------------------------------------
# Voice frames are small, frequent, and latency-sensitive, so they're kept
# as a raw binary struct rather than JSON: a 5-byte header (sample rate +
# channel count) followed by raw PCM16 samples. The host does no mixing;
# it just relays each viewer's frames to every other viewer, same as it
# relays video frames but without the broadcast-to-everyone-including-
# sender step.

MSG_VOICE_FRAME = 0x76
MSG_VOICE_STATE = 0x77

_VOICE_HEADER = struct.Struct("!IB")  # sample_rate (uint32), channels (uint8)


def pack_voice_frame(pcm_bytes: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    return _VOICE_HEADER.pack(sample_rate, channels) + pcm_bytes


def unpack_voice_frame(payload: bytes) -> tuple:
    """Returns (sample_rate, channels, pcm_bytes)."""
    sample_rate, channels = _VOICE_HEADER.unpack(payload[:_VOICE_HEADER.size])
    return sample_rate, channels, payload[_VOICE_HEADER.size:]


def pack_voice_state(viewer_id: str, mic_on: bool) -> bytes:
    """Broadcast when a viewer mutes/unmutes, purely for UI indicators."""
    return json.dumps({"viewer_id": viewer_id, "mic_on": mic_on}).encode("utf-8")


def unpack_voice_state(payload: bytes) -> dict:
    """Returns {"viewer_id", "mic_on"}."""
    return json.loads(payload.decode("utf-8"))
