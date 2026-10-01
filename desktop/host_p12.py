"""
Phase 12 - Scale & polish, host side.

Adds to Phase 11 (everything else is unchanged):

  1. Relay mode for the multi-user host, with failover. host_p7..p11 accepted
     --relay but never used it (only host.py did); this host does. Give
     --relay a comma-separated list (a:6000,b:6000) and it registers on all
     of them, takes whichever relay a viewer reaches first, and keeps
     re-registering with backoff on any relay that drops or is down - see
     relay_client.py and docs/phases/PHASE_12_README.md. Direct mode (no
     --relay) is exactly as before.
  2. In-app support/feedback: a connected viewer's /feedback and /replies
     (MSG_FEEDBACK, protocol_p12.py) are filed with / read from the admin
     console through this host's own enrollment. See feedback_cli.py for the
     operator at the host machine itself.
  3. --lang: interface language for the host's own messages (i18n.py).

Phase 11 - Automation & remote ops, host side.

Adds two things, both driven by the admin console (nothing changes if
--admin-url isn't set):

  1. Wake-on-LAN reachability - this host reports its own MAC address
     alongside its version on every policy fetch (see
     admin_client.get_own_mac_address), so an org's REST API / CLI can
     wake it later without anyone hunting down its MAC by hand. Actually
     *sending* the wake packet is the admin console's job (admin/wol.py),
     since it's a LAN-broadcast operation - nothing for the host itself
     to do here beyond reporting what it is.
  2. One-time unattended connections - accept_viewer passes
     auth.decide_host_auth a preauth_check hook. Before a connection
     falls back to a blocking console prompt, the hook asks the admin
     console whether it currently has a live one-time pre-authorization
     for this exact viewer_id (issued by POST
     /api/v1/ops/devices/<id>/connect, checked via
     admin_client.check_preauth). Only viewer_ids with the "auto-"
     prefix the admin console always generates are even considered, so a
     normal person typing their own name into a viewer never adds a
     network round-trip to their connection attempt.

This is what lets Phase 11's REST API/CLI actually complete a
connection unattended, rather than just returning connection info a
person still has to hand-carry: an external script calls .../connect,
gets back a one-time viewer_id, and runs viewer_p10.py with that ID and
no password at all - no changes to the viewer needed.

Also fixes a bug that Phases 7-10 all have (see auth.decide_host_auth):
accept_viewer read the auth request itself to get view_mode, then called
auth.perform_host_auth, which tried to read the same request again from a
socket the viewer only sent it to once - so no real connection could ever
be approved. accept_viewer now calls decide_host_auth with the fields it
already parsed.

Usage: identical to host_p10.py.
"""

import argparse
import os
import io
import socket
import ssl
import threading
import time

from PIL import Image

import adaptive
import auth
import clipboard_sync
import direct_accept
import console
import file_transfer
import monitors
import pinning
import protocol as proto
import session_log
import protocol_p7 as proto_p7
import protocol_p8 as proto_p8
import protocol_p9 as proto_p9
import protocol_p10 as proto_p10
import protocol_p12 as proto_p12
import i18n
import relay_client
import session_manager
import support
import whiteboard as whiteboard_mod
import print_service
import admin_client
import updater
from i18n import t

# Phase 12: what handle_viewer_control needs to file/read feedback tickets through this
# host's admin-console enrollment. Filled in by run(); both None = no support channel.
_SUPPORT = {"admin_url": None, "report_token": None}

CERT_FILE = "host_cert.pem"
KEY_FILE = "host_key.pem"


def _report(args, event: str, **fields) -> None:
    """Best-effort mirror of a local session_log event to the admin console;
    a no-op if this run wasn't given --admin-url (args.report_token is only
    ever set in run(), right after a successful enrollment)."""
    if getattr(args, "admin_url", None) and getattr(args, "report_token", None):
        admin_client.report_event_background(args.admin_url, args.report_token, event, **fields)


# --- Reused verbatim from host_p7.py (screen capture / networking setup) -

def get_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def make_tls_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.load_cert_chain(CERT_FILE, KEY_FILE)
    except FileNotFoundError:
        raise SystemExit(
            f"Missing {CERT_FILE}/{KEY_FILE}. Run ./generate_cert.sh first."
        )
    try:
        print(t("[host] TLS certificate fingerprint (SHA-256): {fingerprint}  "
                "(viewers can verify it with --pin)", fingerprint=pinning.fingerprint_pem_file(CERT_FILE)))
    except (OSError, ValueError):
        pass
    return ctx


def get_raw_channel_direct(port: int, label: str) -> socket.socket:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(5)
    print(f"[host] [{label}] listening on port {port} for multi-user connections...")
    return server


def accept_connection_direct(server: socket.socket, label: str) -> tuple:
    conn, addr = server.accept()
    print(f"[host] [{label}] viewer connected from {addr}")
    return conn, addr


def capture_frame(sct, monitor, quality: int) -> bytes:
    raw = sct.grab(monitor)
    img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def broadcast_video(session: session_manager.MultiUserSessionManager,
                     active_monitor: monitors.ActiveMonitor,
                     bitrate: adaptive.AdaptiveBitrateController) -> None:
    """Unchanged from Phase 7 (with headless CI stub fallback)."""
    try:
        import mss
        with mss.mss() as sct:
            while session.is_active():
                try:
                    start = time.time()
                    quality, fps = bitrate.get_settings()
                    monitor = sct.monitors[active_monitor.get()]
                    frame = capture_frame(sct, monitor, quality)

                    viewers = session.get_video_recipients()
                    dead_viewers = []
                    for viewer in viewers:
                        try:
                            proto.send_message(viewer.video_conn, proto.MSG_VIDEO_FRAME, frame)
                        except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
                            print(f"[host] [video] viewer '{viewer.viewer_id}' disconnected")
                            dead_viewers.append(viewer)

                    for viewer in dead_viewers:
                        drop_viewer(session, viewer)

                    interval = 1.0 / fps if fps > 0 else 0
                    elapsed = time.time() - start
                    if interval > elapsed:
                        time.sleep(interval - elapsed)

                except Exception as e:
                    print(f"[host] [video] error: {e}")
                    time.sleep(0.1)
    except Exception as err:
        print(f"[host] [video] mss capture unavailable ({err}), using synthetic frame stub")
        img = Image.new("RGB", (1024, 768), (30, 30, 30))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=50)
        stub_frame = buf.getvalue()

        while session.is_active():
            viewers = session.get_video_recipients()
            dead_viewers = []
            for viewer in viewers:
                try:
                    proto.send_message(viewer.video_conn, proto.MSG_VIDEO_FRAME, stub_frame)
                except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
                    dead_viewers.append(viewer)
            for viewer in dead_viewers:
                drop_viewer(session, viewer)
            time.sleep(0.5)


def monitor_geometry(index: int):
    """(left, top, width, height) of mss monitor `index`, or None if mss is
    unavailable or the index is gone (monitor unplugged mid-session)."""
    try:
        import mss
        with mss.mss() as sct:
            if 1 <= index < len(sct.monitors):
                m = sct.monitors[index]
                return m["left"], m["top"], m["width"], m["height"]
    except Exception:
        pass
    return None


def normalized_to_screen(x_norm: float, y_norm: float, geom) -> tuple:
    """Map 0..1 coordinates onto the captured monitor in absolute desktop space.
    The offset matters: monitor 2 does not start at (0, 0)."""
    left, top, width, height = geom
    return left + x_norm * width, top + y_norm * height


def handle_viewer_input(viewer: session_manager.ViewerSession,
                         session: session_manager.MultiUserSessionManager,
                         active_monitor: monitors.ActiveMonitor = None) -> None:
    """Phase 7 input handling; the coordinate mapping now follows the monitor
    being captured (it used to stay pinned to monitor 1 after a switch)."""
    if not viewer.is_control:
        print(f"[host] [input] viewer '{viewer.viewer_id}' is view-only, skipping input handling")
        return

    try:
        from pynput.mouse import Controller as MouseController, Button
        from pynput.keyboard import Controller as KeyboardController, Key
    except Exception:
        class _DummyButton:
            left = "left"
            right = "right"
            middle = "middle"
        Button = _DummyButton()

        class _DummyKey:
            pass
        Key = _DummyKey()

        class MouseController:
            def __init__(self): self.position = (0, 0)
            def press(self, btn): pass
            def release(self, btn): pass
            def scroll(self, dx, dy): pass

        class KeyboardController:
            def press(self, key): pass
            def release(self, key): pass

    mouse = MouseController()
    keyboard = KeyboardController()

    fallback_geom = (0, 0, 1920, 1080)
    geom_cache = {"index": None, "geom": fallback_geom}

    def current_geom():
        # Re-read only when the active monitor changes (or on first use); mss
        # enumeration is too slow to repeat for every mouse-move event.
        idx = active_monitor.get() if active_monitor is not None else 1
        if idx != geom_cache["index"]:
            geom_cache["geom"] = monitor_geometry(idx) or geom_cache["geom"]
            geom_cache["index"] = idx
        return geom_cache["geom"]

    button_map = {"left": Button.left, "right": Button.right, "middle": Button.middle}

    def resolve_key(name: str):
        if len(name) == 1:
            return name
        return getattr(Key, name, None)

    try:
        while session.is_active():
            try:
                msg_type, payload = proto.recv_message(viewer.input_conn)
            except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
                print(f"[host] [input] viewer '{viewer.viewer_id}' disconnected")
                break

            if not session.is_control_viewer(viewer):
                print(f"[host] [input] rejecting input from '{viewer.viewer_id}' (no longer in control)")
                break

            if msg_type == proto.MSG_MOUSE_MOVE:
                x_norm, y_norm = proto.unpack_mouse_move(payload)
                mouse.position = normalized_to_screen(x_norm, y_norm, current_geom())

            elif msg_type == proto.MSG_MOUSE_CLICK:
                x_norm, y_norm, button, pressed = proto.unpack_mouse_click(payload)
                mouse.position = normalized_to_screen(x_norm, y_norm, current_geom())
                btn = button_map[button]
                if pressed:
                    mouse.press(btn)
                else:
                    mouse.release(btn)

            elif msg_type == proto.MSG_MOUSE_SCROLL:
                dx, dy = proto.unpack_mouse_scroll(payload)
                mouse.scroll(dx, dy)

            elif msg_type == proto.MSG_KEY_EVENT:
                key_name, pressed = proto.unpack_key_event(payload)
                key = resolve_key(key_name)
                if key is None:
                    continue
                if pressed:
                    keyboard.press(key)
                else:
                    keyboard.release(key)

    except Exception as e:
        print(f"[host] [input] error for '{viewer.viewer_id}': {e}")
    finally:
        if session.is_control_viewer(viewer):
            drop_viewer(session, viewer)


# --- New in Phase 8: voice relay (Phase 9: gated by allow_voice policy) --

def handle_viewer_audio(viewer: session_manager.ViewerSession,
                         session: session_manager.MultiUserSessionManager,
                         policy_state: admin_client.PolicyState) -> None:
    """Relays this viewer's voice frames to every other viewer's audio
    channel. No mixing happens on the host - each viewer's client mixes
    whatever concurrent frames it receives. Runs in its own thread.

    Phase 9: if policy disallows voice, this still drains the audio
    socket (so the viewer's sends don't back up) but never relays a
    frame, and sends one MSG_POLICY_DENIED up front so the viewer's
    client can tell its operator why they can't hear anyone."""
    voice_allowed = policy_state.get().get("allow_voice", True)
    if not voice_allowed:
        try:
            proto.send_message(viewer.control_conn, proto_p9.MSG_POLICY_DENIED,
                                proto_p9.pack_policy_denied(
                                    "voice", "Voice chat disabled by admin policy"))
        except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
            pass

    try:
        while session.is_active():
            try:
                msg_type, payload = proto.recv_message(viewer.audio_conn)
            except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
                print(f"[host] [audio] viewer '{viewer.viewer_id}' disconnected")
                break

            if not voice_allowed:
                continue  # drain without relaying

            if msg_type == proto_p8.MSG_VOICE_FRAME:
                for other in session.get_audio_recipients(exclude=viewer):
                    try:
                        proto.send_message(other.audio_conn, proto_p8.MSG_VOICE_FRAME, payload)
                    except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
                        pass  # Cleaned up by that viewer's own threads

            elif msg_type == proto_p8.MSG_VOICE_STATE:
                state = proto_p8.unpack_voice_state(payload)
                viewer.mic_muted = not state.get("mic_on", True)
                for other in session.get_all_viewers():
                    if other is not viewer:
                        try:
                            proto.send_message(other.control_conn, proto_p8.MSG_VOICE_STATE, payload)
                        except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
                            pass

    except Exception as e:
        print(f"[host] [audio] error for '{viewer.viewer_id}': {e}")


# --- Extended for Phase 8: chat / whiteboard / print added to the control loop
# --- Extended for Phase 9: each of those (plus clipboard/file transfer)
#     checked against policy before it's allowed to happen

def handle_viewer_control(viewer: session_manager.ViewerSession,
                           session: session_manager.MultiUserSessionManager,
                           clipboard: clipboard_sync.ClipboardSync,
                           file_session: file_transfer.FileTransferSession,
                           bitrate: adaptive.AdaptiveBitrateController,
                           monitor_host: monitors.MonitorHost,
                           board: whiteboard_mod.WhiteboardState,
                           download_dir: str,
                           policy_state: admin_client.PolicyState) -> None:
    """Handles the control channel for one viewer: everything Phase 7 handled
    (clipboard, files, monitors, stats, permissions) plus chat, whiteboard,
    and print requests added in Phase 8 - each of the latter, plus
    clipboard and file transfer, now gated on the live effective policy
    (Phase 9). A denied action sends MSG_POLICY_DENIED back instead of a
    silent no-op, except printing, which reuses its own existing
    MSG_PRINT_RESPONSE channel since the viewer already knows how to
    render that."""

    def control_send(msg_type, payload):
        try:
            proto.send_message(viewer.control_conn, msg_type, payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
            pass

    def broadcast_to_others(msg_type, payload):
        for other in session.get_all_viewers():
            if other is not viewer:
                try:
                    proto.send_message(other.control_conn, msg_type, payload)
                except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
                    pass

    def policy_deny(action: str, reason: str) -> None:
        control_send(proto_p9.MSG_POLICY_DENIED, proto_p9.pack_policy_denied(action, reason))

    try:
        while session.is_active():
            try:
                msg_type, payload = proto.recv_message(viewer.control_conn)
            except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
                print(f"[host] [control] viewer '{viewer.viewer_id}' disconnected")
                break

            policy = policy_state.get()

            # --- Phase 7 handling (Phase 9: gated on allow_clipboard) ---
            if msg_type == proto.MSG_CLIPBOARD_TEXT:
                if not policy.get("allow_clipboard", True):
                    policy_deny("clipboard", "Clipboard sync disabled by admin policy")
                else:
                    clipboard.handle_remote_text(payload)

            elif msg_type == proto.MSG_CLIPBOARD_IMAGE:
                if not policy.get("allow_clipboard", True):
                    policy_deny("clipboard", "Clipboard sync disabled by admin policy")
                else:
                    clipboard.handle_remote_image(payload)

            # --- Phase 7 handling (Phase 9: gated on allow_file_transfer) ---
            elif msg_type in (proto.MSG_FILE_LIST_REQUEST, proto.MSG_FILE_SEND_REQUEST,
                              proto.MSG_FILE_CHUNK, proto.MSG_FILE_PULL_REQUEST):
                if not policy.get("allow_file_transfer", True):
                    policy_deny("file_transfer", "File transfer disabled by admin policy")
                elif msg_type == proto.MSG_FILE_LIST_REQUEST:
                    file_session.handle_file_list_request(payload, control_send)
                elif msg_type == proto.MSG_FILE_SEND_REQUEST:
                    file_session.handle_file_send_request(payload, control_send)
                elif msg_type == proto.MSG_FILE_CHUNK:
                    file_session.handle_file_chunk(payload, control_send)
                elif msg_type == proto.MSG_FILE_PULL_REQUEST:
                    file_session.handle_file_pull_request(payload, control_send)

            elif msg_type == proto.MSG_STATS_REPORT:
                measured_fps, measured_kbps = proto.unpack_stats_report(payload)
                bitrate.record_report(measured_fps, measured_kbps)   # Phase 12: was report_stats(), which doesn't exist

            elif msg_type == proto.MSG_MONITOR_LIST_REQUEST:
                monitor_host.handle_list_request(control_send)

            elif msg_type == proto.MSG_MONITOR_SWITCH:
                monitor_host.handle_switch_request(payload)

            elif msg_type == proto_p7.MSG_PERMISSIONS_QUERY:
                can_send = session.can_send_input(viewer)
                reason = "OK" if can_send else "Control is held by another viewer"
                control_send(proto_p7.MSG_PERMISSIONS_RESPONSE,
                             proto_p7.pack_permissions_response(can_send, reason))

            # --- Phase 8: chat (Phase 9: gated on allow_chat) ---
            elif msg_type == proto_p8.MSG_CHAT_TEXT:
                if not policy.get("allow_chat", True):
                    policy_deny("chat", "Chat disabled by admin policy")
                else:
                    msg = proto_p8.unpack_chat_message(payload)
                    print(f"[chat] {msg['sender_id']}: {msg['text']}")
                    broadcast_to_others(proto_p8.MSG_CHAT_TEXT, payload)

            # --- Phase 8: whiteboard (Phase 9: gated on allow_whiteboard) ---
            elif msg_type == proto_p8.MSG_WHITEBOARD_STROKE:
                if not policy.get("allow_whiteboard", True):
                    policy_deny("whiteboard", "Whiteboard disabled by admin policy")
                else:
                    stroke = proto_p8.unpack_whiteboard_stroke(payload)
                    if stroke["action"] == proto_p8.STROKE_START:
                        board.start_stroke(stroke["stroke_id"], stroke["viewer_id"],
                                            stroke["color"], stroke["width"],
                                            stroke["x"], stroke["y"])
                    elif stroke["action"] == proto_p8.STROKE_POINT:
                        board.add_point(stroke["stroke_id"], stroke["x"], stroke["y"])
                    elif stroke["action"] == proto_p8.STROKE_END:
                        board.end_stroke(stroke["stroke_id"])
                    broadcast_to_others(proto_p8.MSG_WHITEBOARD_STROKE, payload)

            elif msg_type == proto_p8.MSG_WHITEBOARD_CLEAR:
                if not policy.get("allow_whiteboard", True):
                    policy_deny("whiteboard", "Whiteboard disabled by admin policy")
                else:
                    board.clear()
                    broadcast_to_others(proto_p8.MSG_WHITEBOARD_CLEAR, payload)

            # --- Phase 8: remote print (Phase 9: gated on allow_printing) ---
            elif msg_type == proto_p8.MSG_PRINT_REQUEST:
                req = proto_p8.unpack_print_request(payload)
                if not policy.get("allow_printing", True):
                    control_send(proto_p8.MSG_PRINT_RESPONSE,
                                 proto_p8.pack_print_response(
                                     False, "Remote printing disabled by admin policy"))
                elif not viewer.is_control:
                    control_send(proto_p8.MSG_PRINT_RESPONSE,
                                 proto_p8.pack_print_response(
                                     False, "Only the control viewer can print (view-only session)"))
                else:
                    success, message, job_id = print_service.print_file(
                        req["filename"], download_dir, req.get("printer", ""), req.get("copies", 1))
                    print(f"[host] [print] '{viewer.viewer_id}' -> {req['filename']}: {message}")
                    control_send(proto_p8.MSG_PRINT_RESPONSE,
                                 proto_p8.pack_print_response(success, message, job_id))

            # --- Phase 12: support / feedback, filed through this host's own admin enrollment ---
            elif msg_type == proto_p12.MSG_FEEDBACK:
                control_send(proto_p12.MSG_FEEDBACK_RESULT, handle_feedback_request(viewer.viewer_id, payload))

    except Exception as e:
        print(f"[host] [control] error for '{viewer.viewer_id}': {e}")


def handle_feedback_request(viewer_id: str, payload: bytes) -> bytes:
    """Returns the packed MSG_FEEDBACK_RESULT for one viewer's MSG_FEEDBACK. Never
    raises - every failure becomes an ok=False result the viewer can display. A
    viewer's tickets are stamped with (and listed by) its own viewer ID."""
    admin_url, token = _SUPPORT["admin_url"], _SUPPORT["report_token"]
    if not (admin_url and token):
        return proto_p12.pack_feedback_result(False, "This host isn't connected to a support channel")
    try:
        req = proto_p12.unpack_feedback(payload)
    except (ValueError, UnicodeDecodeError):
        return proto_p12.pack_feedback_result(False, "Malformed feedback request")
    try:
        if req["action"] == "submit":
            ticket = support.submit(admin_url, token, req["message"], req["category"],
                                    viewer_id=viewer_id, client_version=updater.CURRENT_VERSION)
            print(f"[host] [support] ticket #{ticket} filed on behalf of '{viewer_id}'")
            return proto_p12.pack_feedback_result(True, ticket_id=ticket)
        items = support.list_tickets(admin_url, token, viewer_id=viewer_id)
        keep = ("id", "category", "message", "status", "reply", "created_at")
        return proto_p12.pack_feedback_result(True, items=[{k: i.get(k) for k in keep} for i in items])
    except admin_client.AdminUnavailable as e:
        # The console's own error text ("message is empty", "too many feedback submissions...")
        # is written for the submitter; connection failures read fine too.
        return proto_p12.pack_feedback_result(False, str(e).split(": ", 1)[-1])


def broadcast_session_info(session: session_manager.MultiUserSessionManager,
                            info_msg: bytes) -> None:
    for viewer in session.get_all_viewers():
        try:
            proto.send_message(viewer.control_conn, proto_p7.MSG_VIEWER_LEFT, info_msg)
        except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
            pass


def drop_viewer(session: session_manager.MultiUserSessionManager,
                 viewer: session_manager.ViewerSession) -> None:
    """Removes a viewer and tells everyone else they left (video/input/control
    paths all funnel disconnects here so it only happens once per viewer)."""
    session.remove_viewer(viewer)
    broadcast_session_info(session, proto_p7.pack_viewer_left(viewer.viewer_id))


def enforce_session_time_limit(viewer: session_manager.ViewerSession,
                                session: session_manager.MultiUserSessionManager,
                                minutes: int) -> None:
    """Phase 9: disconnects a single viewer once policy's max_session_minutes
    has elapsed since they joined, independent of what the rest of the
    session does. The policy value used is a snapshot taken when this
    viewer connected (see accept_viewer) - an admin shortening the limit
    mid-session affects the next viewer to join, not one already counting
    down under the old value. Polls in short intervals rather than one
    long sleep so it notices promptly if the viewer already left."""
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        if viewer not in session.get_all_viewers():
            return  # already disconnected on its own; nothing to enforce
        time.sleep(5)

    if viewer in session.get_all_viewers():
        try:
            proto.send_message(viewer.control_conn, proto_p9.MSG_POLICY_DENIED,
                                proto_p9.pack_policy_denied(
                                    "session", "Session time limit reached (org policy)"))
        except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
            pass
        print(f"[host] [policy] Disconnecting '{viewer.viewer_id}' - session time limit reached")
        drop_viewer(session, viewer)


HANDSHAKE_TIMEOUT = 10.0


def handshake_channels(raw_video: socket.socket, next_input, next_control, next_audio,
                       tls_ctx: ssl.SSLContext, on_video=None) -> tuple:
    """Phase 12 fix. Completes TLS on all four channels, in the order the viewer
    opens them (video, input, control, audio), BEFORE authentication. Returns
    (video, input, control, audio) TLS connections; raises OSError/ssl.SSLError
    (closing whatever was opened) if any step fails or stalls.

    Phases 7-11 instead accepted all four raw sockets first and handshook only
    video, and handshook the other three only AFTER auth. The viewer does the
    opposite: it handshakes each channel as it opens it, and only sends its auth
    request once all four are up. Each side waited for the other, so every
    multi-user connection timed out ("The handshake operation timed out") -
    direct or relayed. `next_input/next_control/next_audio` are callables that
    return the next raw socket for that channel (accepting it, or registering it
    on the relay), so each can be fetched only after the channel before it is done.
    `on_video`, if given, is called with the video TLS connection as soon as it is up, so a caller
    that waits for the other channels can notice the viewer closing it (direct mode does).
    """
    done = []
    try:
        video = _wrap_with_timeout(raw_video, tls_ctx); done.append(video)
        if on_video is not None:
            on_video(video)
        input_conn = _wrap_with_timeout(next_input(), tls_ctx); done.append(input_conn)
        control_conn = _wrap_with_timeout(next_control(), tls_ctx); done.append(control_conn)
        audio_conn = _wrap_with_timeout(next_audio(), tls_ctx); done.append(audio_conn)
        return video, input_conn, control_conn, audio_conn
    except BaseException:
        for c in done:
            try:
                c.close()
            except OSError:
                pass
        raise


def _wrap_with_timeout(raw: socket.socket, tls_ctx: ssl.SSLContext) -> ssl.SSLSocket:
    raw.settimeout(HANDSHAKE_TIMEOUT)
    try:
        conn = tls_ctx.wrap_socket(raw, server_side=True)
    except BaseException:
        raw.close()
        raise
    conn.settimeout(None)
    return conn


def accept_viewer(video_conn: ssl.SSLSocket, input_conn: ssl.SSLSocket,
                   control_conn: ssl.SSLSocket, audio_conn: ssl.SSLSocket,
                   config: dict,
                   session: session_manager.MultiUserSessionManager,
                   board: whiteboard_mod.WhiteboardState,
                   clipboard: clipboard_sync.ClipboardSync,
                   file_session: file_transfer.FileTransferSession,
                   bitrate: adaptive.AdaptiveBitrateController,
                   monitor_host: monitors.MonitorHost,
                   policy_state: admin_client.PolicyState,
                   branding: dict,
                   args, peer_addr: str) -> None:
    """Same flow as Phase 7's accept_viewer, plus wrapping the new audio
    channel and sending a whiteboard snapshot alongside the session-info
    snapshot every new viewer already got in Phase 7.

    Phase 9: the auth result is additionally checked against the live
    policy (org viewer-ID block/allow lists, whether unattended access is
    allowed at all, whether the session is already at its viewer cap, and
    whether every viewer is being forced into view-only) before it's
    honored - a local auth.py approval can still be overruled by org
    policy, but policy can never approve something local auth rejected."""

    print("[host] TLS handshake complete on all channels. Waiting for authentication...")

    try:
        msg_type, payload = proto.recv_message(video_conn)
        if msg_type != proto.MSG_AUTH_REQUEST:
            print(f"[host] Expected AUTH_REQUEST, got {msg_type}")
            for c in (video_conn, input_conn, control_conn, audio_conn):
                c.close()
            return

        try:
            auth_req = proto_p7.unpack_auth_request_p7(payload)
            view_mode = auth_req.get("view_mode", proto_p7.VIEW_MODE_CONTROL)
        except Exception:
            auth_req = proto.unpack_auth_request(payload)
            view_mode = proto_p7.VIEW_MODE_CONTROL

        viewer_id = auth_req.get("viewer_id")
        password = auth_req.get("password", "")
        totp_code = auth_req.get("totp_code", "")

    except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
        print("[host] Viewer disconnected during auth")
        video_conn.close()
        return

    admin_url = getattr(args, "admin_url", None)
    report_token = getattr(args, "report_token", None)

    def preauth_check(candidate_id: str) -> bool:
        # Fast path first: only the "auto-" IDs the admin console mints are
        # ever worth a network round-trip. check_preauth fails closed.
        return bool(admin_url and report_token and candidate_id.startswith("auto-")
                    and admin_client.check_preauth(admin_url, report_token, candidate_id))

    # Through a relay, peer_addr is the relay's, not the viewer's: don't throttle on it.
    result = auth.decide_host_auth(viewer_id, password, totp_code, config, peer_addr,
                                    preauth_check=preauth_check,
                                    throttle_addr=None if getattr(args, "relay", None) else peer_addr)

    # --- Phase 9: org policy can overrule (never overrule-in-the-permitting-
    # direction) whatever local auth.py just decided ---
    policy = policy_state.get()
    if result["approved"]:
        vid = result["viewer_id"]
        if vid in policy.get("blocked_viewer_ids", []):
            result = {"approved": False, "viewer_id": vid, "decision": "rejected_policy_blocklist",
                      "reason": "Viewer ID blocked by org policy"}
        elif policy.get("require_whitelist") and vid not in policy.get("allowed_viewer_ids", []):
            result = {"approved": False, "viewer_id": vid, "decision": "rejected_policy_whitelist",
                      "reason": "Viewer ID is not on the org policy allow-list"}
        elif result["decision"] == "auto_unattended" and not policy.get("allow_unattended_access", True):
            result = {"approved": False, "viewer_id": vid, "decision": "rejected_policy_unattended",
                      "reason": "Unattended access is disabled by admin policy; an operator "
                                "must accept the connection at the host console"}
        elif len(session.get_all_viewers()) >= policy.get("max_viewers", 10):
            result = {"approved": False, "viewer_id": vid, "decision": "rejected_policy_capacity",
                      "reason": f"Session viewer limit reached ({policy.get('max_viewers')} max, org policy)"}
        elif policy.get("require_view_only"):
            view_mode = proto_p7.VIEW_MODE_VIEW_ONLY  # forced below the response is built

    session_log.log_event(
        "attempt", path=args.session_log,
        viewer_id=result["viewer_id"], address=str(peer_addr),
        decision=result["decision"], reason=result["reason"],
    )
    _report(args, "attempt", viewer_id=result["viewer_id"], address=str(peer_addr),
            decision=result["decision"], reason=result["reason"])

    try:
        resp_payload = proto_p7.pack_auth_response_p7(result["approved"], view_mode, result["reason"])
        proto.send_message(video_conn, proto.MSG_AUTH_RESPONSE, resp_payload)
    except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
        print("[host] Viewer disconnected during auth response")
        for c in (video_conn, input_conn, control_conn, audio_conn):
            c.close()
        return

    if not result["approved"]:
        print(t("[host] Connection rejected ({decision}): {reason}", decision=result['decision'], reason=result['reason']))
        for c in (video_conn, input_conn, control_conn, audio_conn):
            c.close()
        return

    print(t("[host] Connection approved ({decision}) for viewer '{viewer}'.",
            decision=result['decision'], viewer=result['viewer_id']))

    is_control = (view_mode == proto_p7.VIEW_MODE_CONTROL)

    viewer = session_manager.ViewerSession(
        viewer_id=result["viewer_id"],
        address=str(peer_addr),
        video_conn=video_conn,
        input_conn=input_conn,
        control_conn=control_conn,
        is_control=is_control,
        audio_conn=audio_conn,
    )

    success, reason = session.add_viewer(viewer)
    if not success:
        print(f"[host] Failed to add viewer: {reason}")
        if "Control already taken" in reason and is_control:
            print(f"[host] Downgrading '{result['viewer_id']}' to view-only mode")
            viewer.is_control = False
            success, reason = session.add_viewer(viewer)
        if not success:
            print(f"[host] Failed even as view-only: {reason}")
            for c in (video_conn, input_conn, control_conn, audio_conn):
                c.close()
            return

    # Session info (Phase 7) + whiteboard snapshot (Phase 8) + org branding
    # (Phase 10) for the new viewer
    viewers_info = session.list_viewers()
    try:
        proto.send_message(viewer.control_conn, proto_p7.MSG_SESSION_INFO,
                            proto_p7.pack_session_info(viewers_info))
        proto.send_message(viewer.control_conn, proto_p8.MSG_WHITEBOARD_STATE,
                            proto_p8.pack_whiteboard_state(board.snapshot()))
        proto.send_message(viewer.control_conn, proto_p10.MSG_BRANDING,
                            proto_p10.pack_branding(branding["display_name"], branding["support_url"]))
    except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
        print("[host] Failed to send join snapshot to new viewer")

    join_msg = proto_p7.pack_viewer_joined(viewer.viewer_id, viewer.is_control, str(peer_addr))
    for other in session.get_all_viewers():
        if other != viewer:
            try:
                proto.send_message(other.control_conn, proto_p7.MSG_VIEWER_JOINED, join_msg)
            except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
                pass

    session_log.log_event("start", path=args.session_log,
                           viewer_id=result["viewer_id"], address=str(peer_addr))
    _report(args, "start", viewer_id=result["viewer_id"], address=str(peer_addr))

    threading.Thread(target=handle_viewer_input,
                      args=(viewer, session, monitor_host.active_monitor), daemon=True, name=f"input-{viewer.viewer_id}").start()

    threading.Thread(target=handle_viewer_audio,
                      args=(viewer, session, policy_state), daemon=True, name=f"audio-{viewer.viewer_id}").start()

    threading.Thread(target=handle_viewer_control,
                      args=(viewer, session, clipboard, file_session, bitrate,
                            monitor_host, board, args.download_dir, policy_state),
                      daemon=True, name=f"control-{viewer.viewer_id}").start()

    if policy.get("max_session_minutes", 0) > 0:
        threading.Thread(target=enforce_session_time_limit,
                          args=(viewer, session, policy["max_session_minutes"]),
                          daemon=True, name=f"policy-timelimit-{viewer.viewer_id}").start()

    # Phase 9: this loop now also exits as soon as *this* viewer leaves,
    # rather than only when the whole multi-user session shuts down - so
    # the "end" event (local log and admin report alike) fires at the
    # moment this viewer actually disconnects, with their own connected
    # duration, instead of only on host shutdown with the session's
    # overall duration. (Phase 7/8 only had the latter; harmless for a
    # single viewer, but under-reported "end" events once several
    # viewers could be in a session together, which Phase 9's session
    # history dashboard depends on being accurate.)
    try:
        while session.is_active() and viewer in session.get_all_viewers():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        duration = round(time.time() - viewer.connected_at, 1)
        drop_viewer(session, viewer)
        session_log.log_event("end", path=args.session_log,
                               viewer_id=result["viewer_id"], address=str(peer_addr),
                               duration_seconds=duration)
        _report(args, "end", viewer_id=result["viewer_id"], address=str(peer_addr),
                duration_seconds=duration)


def update_check_loop(admin_url: str, report_token: str,
                       policy_state: admin_client.PolicyState, interval_seconds: int) -> None:
    """Phase 10: runs for the life of the process, checking the admin
    console's published release against this build's own version once per
    interval. A newer release is downloaded and applied automatically only
    if the CURRENT policy (checked fresh each cycle, not just at startup)
    has auto_update on; otherwise this just prints that one's available
    and leaves the running host alone. Every failure mode (console
    unreachable, download fails, checksum mismatch, not running as a
    frozen build) is caught and logged - a broken update path should never
    take down an otherwise-working remote session."""
    while True:
        time.sleep(interval_seconds)
        try:
            release = admin_client.fetch_release(admin_url, report_token)
        except admin_client.AdminUnavailable as e:
            print(f"[host] [update] Could not check for updates: {e}")
            continue

        if not release or not updater.is_newer(release.get("version", "")):
            continue

        print(f"[host] [update] Version {release['version']} is available "
              f"(running {updater.CURRENT_VERSION}).")
        if not policy_state.get().get("auto_update", True):
            print("[host] [update] auto_update is off for this group; not applying automatically.")
            continue

        try:
            downloaded = updater.fetch_and_verify(release)
            print(f"[host] [update] Downloaded and verified {release['version']}; applying...")
            updater.apply_update(downloaded)  # does not return on success
        except Exception as e:
            print(f"[host] [update] Could not apply update: {e}")


def resolve_relay_token(args):
    """Phase 13: the token an authenticating relay wants with every REGISTER. Precedence: --relay-token /
    $REMOTEBRIDGE_RELAY_TOKEN, else fetched from the admin console (which only issues one to an enrolled
    device, for its own ID). None = assume an open relay. Then asks the relays to verify it, so a wrong
    token produces one clear message here instead of an endless silent re-register loop.

    Also sets args.relay_token_provider: what the relay loop actually uses, so a token that expires is
    renewed from the admin console in the background (a token given by hand cannot be renewed)."""
    say = lambda m: print(f"[host] [relay] {m}")
    token = getattr(args, "relay_token", None) or os.environ.get("REMOTEBRIDGE_RELAY_TOKEN")
    provider = None
    if token:
        provider = relay_client.TokenProvider(token, log=say)
        claim = relay_client_token_expiry(token)
        if claim is not None:
            left = claim - time.time()
            say("This relay token was given by hand and cannot be renewed; it "
                + (f"expires in about {_human_duration(left)}." if left > 0 else "has ALREADY EXPIRED.")
                + " Enroll the host with the admin console (RELAY_TOKEN_TTL) to have it renewed automatically.")
    elif args.admin_url and args.report_token:
        def fetch():
            return admin_client.fetch_relay_token_info(args.admin_url, args.report_token)
        try:
            token, expires_in = fetch()
        except admin_client.AdminUnavailable as e:
            say(f"Could not fetch a relay token from the admin console: {e}")
            token, expires_in = None, None
        provider = relay_client.TokenProvider(token, expires_in, refresh=fetch, log=say)
        if token and expires_in:
            say(f"Relay token expires in {_human_duration(expires_in)}; it will be renewed automatically.")
    args.relay_token_provider = provider or relay_client.TokenProvider(None)
    for relay in relay_client.parse_relays(args.relay):
        verdict = relay_client.check_token(relay, args.id, token)
        if verdict == "unauthorized":
            print(f"[host] [relay] Relay {relay[0]}:{relay[1]} REJECTED this device's token - it will not "
                  f"register here. Give the host the right --relay-token, or enroll it with an admin "
                  f"console that shares this relay's secret.")
        elif verdict == "expired":
            print(f"[host] [relay] Relay {relay[0]}:{relay[1]} says this device's token has EXPIRED - it will "
                  f"not register here. Mint a new one (server/relay_token.py --ttl), or enroll the host with "
                  f"the admin console so it renews its own.")
        elif verdict == "revoked":
            print(f"[host] [relay] Relay {relay[0]}:{relay[1]} has REVOKED this device's access - it will not "
                  f"register here. Ask the administrator to restore it in the admin console.")
        elif verdict.startswith("unreachable"):
            print(f"[host] [relay] Relay {relay[0]}:{relay[1]} not reachable right now ({verdict[13:]}); will keep retrying.")
    return token


def relay_client_token_expiry(token):
    """The expiry an expiring relay token carries (unix seconds), or None. Informational only - the relay
    is the one that enforces it. Duplicates the tiny format check rather than importing the relay."""
    expiry, dot, _sig = (token or "").partition(".")
    return int(expiry) if dot and expiry.isascii() and expiry.isdigit() and len(expiry) <= 12 else None


def _human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'' if n == 1 else 's'}"
    return f"{seconds} second{'' if seconds == 1 else 's'}"


def _relay_token_source(args):
    """What to hand the relay client as `token`: the renewing provider when resolve_relay_token made one,
    else the plain string (a host started some other way, or a test)."""
    return getattr(args, "relay_token_provider", None) or getattr(args, "relay_token", None)


def relay_accept_loop(args, relays: list, tls_ctx, config, session_obj, board, clipboard,
                      file_session, bitrate, monitor_host, policy_state, branding) -> None:
    """Phase 12: relay-mode counterpart of the direct accept loop in run(). Each
    round registers "<id>-video" on every relay, waits for a viewer to pair on
    one, then registers input/control/audio on THAT relay (a session never
    straddles relays) and hands the four sockets to accept_viewer exactly as
    direct mode does. The next round starts immediately, so a second viewer can
    join while the first is still connected."""
    while True:
        try:
            raw_video, relay = relay_client.host_wait_paired(
                relays, f"{args.id}-video", log=lambda m: print(f"[host] [relay] {m}"),
                token=_relay_token_source(args))
            # Register the other names on the relay the viewer chose, before any
            # handshake, so the viewer's next CONNECT always finds them.
            pending = {label: relay_client.host_register_on(relay, f"{args.id}-{label}",
                                                        token=_relay_token_source(args))
                       for label in ("input", "control", "audio")}
        except (OSError, ConnectionError) as e:
            print(f"[host] [relay] could not complete channel setup: {e}")
            time.sleep(1)
            continue
        peer = f"{relay[0]}:{relay[1]}"
        try:
            conns = handshake_channels(raw_video, lambda: pending["input"], lambda: pending["control"],
                                       lambda: pending["audio"], tls_ctx)
        except (OSError, ssl.SSLError) as e:
            print(f"[host] [relay] viewer via {peer} did not complete the TLS handshakes: {e}")
            for sock in pending.values():
                try:
                    sock.close()
                except OSError:
                    pass
            continue
        threading.Thread(target=accept_viewer,
                         args=(*conns, config, session_obj, board, clipboard, file_session, bitrate,
                               monitor_host, policy_state, branding, args, peer),
                         daemon=True, name=f"viewer-relay-{peer}").start()


def serve_direct_viewer(raw_video, video_addr, pairer, tls_ctx, config, session_obj, board,
                        clipboard, file_session, bitrate, monitor_host, policy_state, branding,
                        args) -> None:
    """Direct mode: finish one viewer's four TLS handshakes, then hand it to accept_viewer.

    Runs on its own thread per viewer. Setups from the same source address take turns (the channels
    carry no viewer identity, so two setups from one address would be indistinguishable); setups
    from different addresses run side by side. If the viewer closes its video channel while we wait
    for the others, we stop waiting straight away rather than after HANDSHAKE_TIMEOUT."""
    address = video_addr[0]
    with pairer.gate(address):
        video_ref = []
        try:
            conns = handshake_channels(
                raw_video,
                lambda: pairer.take("input", address, HANDSHAKE_TIMEOUT, watch=video_ref[0]),
                lambda: pairer.take("control", address, HANDSHAKE_TIMEOUT, watch=video_ref[0]),
                lambda: pairer.take("audio", address, HANDSHAKE_TIMEOUT, watch=video_ref[0]),
                tls_ctx, on_video=video_ref.append)
        except (OSError, ssl.SSLError) as e:
            print(f"[host] viewer {video_addr} did not complete the TLS handshakes: {e}")
            return
        except Exception as e:
            print(f"[host] Error setting up viewer {video_addr}: {e}")
            return

    accept_viewer(*conns, config, session_obj, board, clipboard, file_session, bitrate,
                  monitor_host, policy_state, branding, args, str(video_addr))


def run(args) -> None:
    if getattr(args, "lang", None):
        i18n.set_language(args.lang)
    tls_ctx = make_tls_context()
    config = auth.load_config(args.config)

    relays = []
    if args.relay:
        try:
            relays = relay_client.parse_relays(args.relay)
        except ValueError as e:
            raise SystemExit(str(e))
        if not args.id:
            raise SystemExit("--id is required when using --relay")

    # --- Phase 10: deploy_config.json fills in --admin-url/--device-id/
    # --enrollment-key when a scripted/MSI install left one next to this
    # executable and the corresponding flag wasn't passed explicitly. A
    # flag on the command line always wins over the file. ---
    deploy_defaults = admin_client.load_deploy_config(args.deploy_config)
    args.admin_url = args.admin_url or deploy_defaults.get("admin_url")
    args.enrollment_key = args.enrollment_key or deploy_defaults.get("enrollment_key")
    args.device_id = args.device_id or deploy_defaults.get("device_id")

    branding = (admin_client.fetch_branding(args.admin_url) if args.admin_url
                else dict(admin_client.DEFAULT_BRANDING))
    print(f"[host] {branding['display_name']} - host v{updater.CURRENT_VERSION}")
    if branding.get("support_url"):
        print(t("[host] Support: {url}", url=branding['support_url']))
    print(t("[host] Your LAN IP is likely: {ip}", ip=get_lan_ip()))

    # --- Phase 9: admin console enrollment + initial policy fetch. Fully
    # optional - a run with no --admin-url skips all of this and behaves
    # exactly like Phase 8, with policy_state holding admin_client's
    # permissive DEFAULT_POLICY for the life of the process. ---
    policy_state = admin_client.PolicyState()
    args.report_token = None
    if args.admin_url:
        device_id = args.device_id or args.id
        if not device_id:
            raise SystemExit("--admin-url requires --device-id (or --id, which doubles as one)")
        try:
            entry = admin_client.ensure_enrolled(
                args.admin_url, device_id, args.enrollment_key,
                display_name=device_id, path=args.enrollment_file)
        except admin_client.AdminUnavailable as e:
            raise SystemExit(f"[host] Could not enroll with admin console at {args.admin_url}: {e}")
        args.report_token = entry["report_token"]
        _SUPPORT["admin_url"], _SUPPORT["report_token"] = args.admin_url, args.report_token
        print(f"[host] Enrolled with admin console as '{device_id}' (group: {entry.get('group')})")

        # Phase 11: report our own MAC alongside the version so the console can
        # Wake-on-LAN this machine later without anyone looking the MAC up by hand.
        own_mac = admin_client.get_own_mac_address()

        if not policy_state.refresh_once(args.admin_url, args.report_token,
                                          client_version=updater.CURRENT_VERSION,
                                          mac_address=own_mac):
            print("[host] Starting on default (permissive) policy; will keep retrying in the background.")
        threading.Thread(target=policy_state.run_refresh_loop,
                          args=(args.admin_url, args.report_token, args.policy_refresh_seconds,
                                updater.CURRENT_VERSION, own_mac),
                          daemon=True, name="policy-refresh").start()

        # --- Phase 10: self-update, on the same cadence as policy refresh ---
        if not args.no_update_check:
            threading.Thread(target=update_check_loop,
                              args=(args.admin_url, args.report_token, policy_state,
                                    args.policy_refresh_seconds),
                              daemon=True, name="update-check").start()

    session_obj = session_manager.MultiUserSessionManager()
    board = whiteboard_mod.WhiteboardState()

    # Shared across every viewer in the session (one clipboard, one file
    # transfer session, one bitrate controller, one active-monitor tracker) -
    # these are per-session state, not per-viewer state.
    clipboard = clipboard_sync.ClipboardSync(None, threading.Lock(), save_dir=args.download_dir)
    file_session = file_transfer.FileTransferSession(None, threading.Lock(), download_dir=args.download_dir)
    active_monitor = monitors.ActiveMonitor(index=1)
    monitor_host = monitors.MonitorHost(active_monitor, lambda msg_type, payload: None)
    bitrate = adaptive.AdaptiveBitrateController(args.quality, args.fps, enabled=not args.no_adaptive)

    threading.Thread(target=clipboard.watch_loop, daemon=True).start()
    threading.Thread(target=console.console_loop, args=(file_session,), daemon=True).start()

    video_thread = threading.Thread(target=broadcast_video,
                                     args=(session_obj, active_monitor, bitrate),
                                     daemon=True)
    video_thread.start()

    if relays:
        args.relay_token = resolve_relay_token(args)
        print(f"[host] Device ID: {args.id}  (viewers connect with this ID via "
              f"{', '.join(f'{h}:{p}' for h, p in relays)})")
        try:
            relay_accept_loop(args, relays, tls_ctx, config, session_obj, board, clipboard,
                              file_session, bitrate, monitor_host, policy_state, branding)
        except KeyboardInterrupt:
            print(t("[host] Shutting down..."))
            session_obj.shutdown()
        return

    video_server = get_raw_channel_direct(args.video_port, "video")
    input_server = get_raw_channel_direct(args.input_port, "input")
    control_server = get_raw_channel_direct(args.control_port, "control")
    audio_server = get_raw_channel_direct(args.audio_port, "audio")
    # The other three listeners are drained by the pairer's own threads, so one viewer's setup can
    # wait for its channels (or give up on them) without holding up anyone else - see direct_accept.
    pairer = direct_accept.ChannelPairer({"input": input_server, "control": control_server,
                                          "audio": audio_server})

    try:
        while True:
            try:
                raw_video, video_addr = accept_connection_direct(video_server, "video")
            except KeyboardInterrupt:
                print(t("[host] Shutting down..."))
                session_obj.shutdown()
                break
            except Exception as e:
                print(f"[host] Error accepting connection: {e}")
                time.sleep(0.1)
                continue

            # Each viewer's remaining channels are paired on its own thread; the loop goes straight
            # back to accepting, so a viewer that stalls or leaves can't hold up the next one.
            threading.Thread(target=serve_direct_viewer,
                              args=(raw_video, video_addr, pairer, tls_ctx, config, session_obj,
                                    board, clipboard, file_session, bitrate, monitor_host,
                                    policy_state, branding, args),
                              daemon=True, name=f"setup-{video_addr}").start()

    finally:
        pairer.close()
        video_server.close()
        input_server.close()
        control_server.close()
        audio_server.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 12 host (relay failover + feedback + localization) on top of Phase 11: multi-user + chat/whiteboard/print/voice "
                                                  "+ admin policy + branding + self-update "
                                                  "+ Wake-on-LAN reporting + one-time automation connections")
    parser.add_argument("--video-port", type=int, default=5000)
    parser.add_argument("--input-port", type=int, default=5001)
    parser.add_argument("--control-port", type=int, default=5002)
    parser.add_argument("--audio-port", type=int, default=5003)
    parser.add_argument("--quality", type=int, default=60, help="starting JPEG quality 1-95")
    parser.add_argument("--fps", type=int, default=15, help="starting frames per second")
    parser.add_argument("--no-adaptive", action="store_true",
                         help="disable adaptive bitrate; keep --quality/--fps fixed")
    parser.add_argument("--relay", help="relay server(s) as host:port. Phase 12: a comma-separated list "
                                         "(a:6000,b:6000) registers on all of them for failover; "
                                         "without --relay the host listens directly as before")
    parser.add_argument("--relay-ca", help="CA or certificate file to trust for tls://host:port relays (a private "
                                            "CA or a self-signed relay certificate); default: the system trust "
                                            "store, or $REMOTEBRIDGE_RELAY_CA")
    parser.add_argument("--relay-token", help="Phase 13: token for a relay that requires authentication "
                                               "(see server/relay_token.py). Usually unnecessary: with "
                                               "--admin-url the host fetches its own from the console. "
                                               "Also read from $REMOTEBRIDGE_RELAY_TOKEN")
    parser.add_argument("--id", help="device ID to register on the relay (viewers connect with it)")
    parser.add_argument("--lang", help="Phase 12: interface language code for this host's messages, e.g. es")
    parser.add_argument("--admin-url", help="Phase 9: base URL of an admin console (admin/server.py), "
                                             "e.g. http://admin.example.org:8443 - omit to run with no "
                                             "central policy, identical to Phase 8. Phase 10: if omitted, "
                                             "also checked in --deploy-config")
    parser.add_argument("--device-id", help="Phase 9: device_id to enroll as with the admin console; "
                                             "defaults to --id if not given, then to --deploy-config (Phase 10)")
    parser.add_argument("--enrollment-key", help="Phase 9: org enrollment key, needed only the first "
                                                  "time this device_id enrolls with a given console "
                                                  "(Phase 10: also checked in --deploy-config)")
    parser.add_argument("--enrollment-file", default=admin_client.ENROLLMENT_FILE,
                         help="Phase 9: where the report_token from enrollment is cached locally")
    parser.add_argument("--policy-refresh-seconds", type=int, default=admin_client.POLICY_REFRESH_SECONDS,
                         help="Phase 9: how often to re-fetch policy from the admin console")
    parser.add_argument("--deploy-config", default=admin_client.DEPLOY_CONFIG_FILE,
                         help="Phase 10: JSON file (see deploy/deploy_config.template.json) providing "
                              "--admin-url/--device-id/--enrollment-key defaults for a scripted install; "
                              "any of those given explicitly on the command line still wins")
    parser.add_argument("--no-update-check", action="store_true",
                         help="Phase 10: never check the admin console for a newer release, "
                              "regardless of group policy")
    parser.add_argument("--config", default=auth.CONFIG_PATH,
                         help="path to host_config.json")
    parser.add_argument("--session-log", default=session_log.LOG_PATH,
                         help="path to append session log entries to")
    parser.add_argument("--download-dir", default=".",
                         help="where files pushed/pulled by the peer are saved, "
                              "and where files requested for printing must live")
    args = parser.parse_args()
    if args.relay_ca:
        relay_client.set_ca_file(args.relay_ca)

    run(args)
