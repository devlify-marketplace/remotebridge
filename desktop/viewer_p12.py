"""
Phase 12 - Scale & polish, viewer side. Adds to Phase 10 (viewer_p10.py is
the base; Phase 11 needed no viewer change):

  * Relay mode with failover: --relay a:6000,b:6000 --device <host device id>
    instead of a direct host:port. Relays are tried in order; the input/
    control/audio channels stay on whichever relay paired the video channel.
  * /feedback [bug|question|idea] <text> and /replies at the chat prompt file
    and read support tickets through the host (protocol_p12.py).
  * --lang: interface language (i18n.py). Host "reason" strings that have a
    catalog entry are translated too.
  * --high-contrast: whiteboard strokes and the mode banner are drawn in a
    high-contrast style (a11y.py). --speak: also speak status lines through
    the OS speech engine. Status lines are always printed as "[status] ...".

Phase 8 - Collaboration & support tools, viewer side.
Phase 9 - Recognizes MSG_POLICY_DENIED: when the host blocks something
(clipboard, file transfer, chat, whiteboard, printing, voice, or a
session time limit) because of admin policy, this viewer prints why
instead of the action just silently not happening. Nothing else about
Phase 8's behavior changes - a Phase 9 viewer talking to a Phase 8 host
simply never receives this message type.
Phase 10 - Recognizes MSG_BRANDING: right after auth, the host sends its
org's display name (and support URL), which retitles this viewer's
window from the generic "Remote Desktop" to e.g. "Acme Corp Remote -
Remote Desktop" - useful because one viewer might connect to hosts
belonging to different orgs across different sessions.

Extends the Phase 7 viewer with:
  1. Text chat            - typed at the terminal, prefixed with "chat: "
                             in the video window overlay isn't attempted;
                             chat lives in the console, matching how the
                             host's own admin console (console.py) works.
  2. Whiteboard overlay    - hold SPACE and drag with the mouse over the
                             video window to draw; 'c' clears the board
                             for everyone; strokes from other viewers are
                             drawn on top of the live video automatically.
  3. Remote printing       - "/print <filename>" at the chat prompt sends
                             a print request for a file already pushed to
                             the host via file transfer.
  4. Voice chat            - on by default if `sounddevice` is installed;
                             'm' toggles mute.

Usage:
  python3 viewer_p12.py 192.168.1.100:5000 --id alice --password mypassword
  python3 viewer_p12.py 192.168.1.100:5000 --id bob --view-only --no-voice
"""

import argparse
import io
import queue
import socket
import ssl
import sys
import threading
import time
import uuid

try:
    import cv2
except Exception:
    class DummyCv2:
        WINDOW_NORMAL = 0
        LINE_AA = 1
        FONT_HERSHEY_SIMPLEX = 0
        COLOR_RGB2BGR = 0
        EVENT_LBUTTONDOWN = 1
        EVENT_LBUTTONUP = 2
        EVENT_MOUSEMOVE = 0
        EVENT_RBUTTONDOWN = 3
        EVENT_RBUTTONUP = 4
        EVENT_MBUTTONDOWN = 5
        EVENT_MBUTTONUP = 6
        EVENT_FLAG_LBUTTON = 1

        def cvtColor(self, img, code): return img
        def line(self, img, pt1, pt2, color, thickness=1, lineType=1): pass
        def rectangle(self, img, pt1, pt2, color, thickness=1): pass
        def putText(self, img, text, org, fontFace, fontScale, color, thickness=1, lineType=1): pass
        def getTextSize(self, text, fontFace, fontScale, thickness): return ((100, 20), 5)
        def namedWindow(self, winname, flags=0): pass
        def setMouseCallback(self, winname, on_mouse): pass
        def setWindowTitle(self, winname, title): pass
        def imshow(self, winname, mat): pass
        def waitKey(self, delay=1): return -1
        def destroyWindow(self, winname): pass
    cv2 = DummyCv2()

import numpy as np
from PIL import Image

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
        def release(key): pass

import protocol as proto
import pinning
import protocol_p7 as proto_p7
import protocol_p8 as proto_p8
import protocol_p9 as proto_p9
import protocol_p10 as proto_p10
import protocol_p12 as proto_p12
import a11y
import audio_chat
import i18n
import relay_client
from i18n import t

WINDOW_NAME = "Remote Desktop"

# Phase 12 display/announcement settings, set once by run() before any thread starts.
HIGH_CONTRAST = False
ANNOUNCER = a11y.Announcer(speak=False)


def make_tls_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def connect_to_host(address: str, ports: tuple, tls_ctx: ssl.SSLContext, pinner=None) -> tuple:
    """Connects video, input, control, and (Phase 8) audio channels. With a pinner, every
    channel's certificate is checked against the pin (and against each other)."""
    host, video_port = address.rsplit(":", 1)
    video_port = int(video_port)
    input_port, control_port, audio_port = ports

    connections = {}
    try:
        raw_video = socket.create_connection((host, video_port), timeout=5)
        video_conn = pinning.wrap_and_check(tls_ctx, raw_video, host, pinner)
        print("[viewer] TLS handshake complete on video channel")
        connections["video"] = video_conn

        raw_input = socket.create_connection((host, input_port), timeout=5)
        input_conn = pinning.wrap_and_check(tls_ctx, raw_input, host, pinner)
        print("[viewer] TLS handshake complete on input channel")
        connections["input"] = input_conn

        raw_control = socket.create_connection((host, control_port), timeout=5)
        control_conn = pinning.wrap_and_check(tls_ctx, raw_control, host, pinner)
        print("[viewer] TLS handshake complete on control channel")
        connections["control"] = control_conn

        raw_audio = socket.create_connection((host, audio_port), timeout=5)
        audio_conn = pinning.wrap_and_check(tls_ctx, raw_audio, host, pinner)
        print("[viewer] TLS handshake complete on audio channel")
        connections["audio"] = audio_conn

        return video_conn, input_conn, control_conn, audio_conn

    except Exception as e:
        for conn in connections.values():
            try:
                conn.close()
            except Exception:
                pass
        raise e


def connect_via_relay(relays: list, device_id: str, tls_ctx: ssl.SSLContext, pinner=None) -> tuple:
    """Phase 12: the four channels through a relay. Tries the relays in order for
    the video channel, then pins the other three to whichever one paired. Returns
    (video, input, control, audio) TLS connections, like connect_to_host."""
    connections = {}
    try:
        raw_video, chosen = relay_client.viewer_connect(relays, f"{device_id}-video")
        print(f"[viewer] paired through relay {chosen[0]}:{chosen[1]}")
        connections["video"] = pinning.wrap_and_check(tls_ctx, raw_video, device_id, pinner)
        for label in ("input", "control", "audio"):
            raw = relay_client.viewer_connect_one(chosen, f"{device_id}-{label}")
            connections[label] = pinning.wrap_and_check(tls_ctx, raw, device_id, pinner)
        return (connections["video"], connections["input"], connections["control"], connections["audio"])
    except Exception:
        for conn in connections.values():
            try:
                conn.close()
            except Exception:
                pass
        raise


def build_pinner(args) -> "pinning.Pinner":
    """Pin under the device ID when going through a relay (the relay is just a pipe),
    otherwise under host:video-port."""
    if args.relay:
        key = pinning.key_device(args.device)
        target = args.device
    else:
        host, port = args.host.rsplit(":", 1)
        key = pinning.key_direct(host, port)
        target = args.host

    def confirm_new(_key, fingerprint) -> bool:
        if not sys.stdin or not sys.stdin.isatty():
            # Nobody to ask (scripted/automated run): trust on first use, but say so.
            print(t("[viewer] Non-interactive run: trusting and remembering {target}'s certificate "
                    "on first use ({fingerprint}). Use --pin to require a fingerprint you verified.",
                    target=target, fingerprint=fingerprint))
            return True
        print(t("[viewer] First connection to {target}. Its certificate fingerprint (SHA-256) is:",
                target=target))
        print("  " + fingerprint)
        print(t("[viewer] Compare it with the fingerprint the host printed when it started."))
        try:
            answer = input(t("[viewer] Trust this host and remember its certificate? [y/N] "))
        except EOFError:
            answer = ""
        return answer.strip().lower() in ("y", "yes")

    return pinning.Pinner(pinning.PinStore(args.known_hosts), key, expected=args.pin,
                          confirm_new=confirm_new, replace=args.replace_pin)


def perform_auth(video_conn: socket.socket, viewer_id: str, password: str,
                  view_only: bool = False) -> dict:
    view_mode = proto_p7.VIEW_MODE_VIEW_ONLY if view_only else proto_p7.VIEW_MODE_CONTROL
    auth_req = proto_p7.pack_auth_request_p7(viewer_id, view_mode, password)
    proto.send_message(video_conn, proto.MSG_AUTH_REQUEST, auth_req)

    try:
        msg_type, payload = proto.recv_message(video_conn)
        if msg_type != proto.MSG_AUTH_RESPONSE:
            raise ValueError(f"Expected AUTH_RESPONSE, got {msg_type}")
        try:
            result = proto_p7.unpack_auth_response_p7(payload)
        except Exception:
            result = proto.unpack_auth_response(payload)
            result["view_mode"] = proto_p7.VIEW_MODE_CONTROL
        return result
    except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError) as e:
        raise RuntimeError(f"Auth failed: {e}")


class WhiteboardOverlay:
    """Client-side mirror of the host's whiteboard.py state, kept just for
    drawing. Thread-safe: strokes arrive on the control-channel thread and
    are drawn on the video thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._strokes = {}  # stroke_id -> {"color","width","points"}
        self._order = []

    def load_snapshot(self, strokes: list) -> None:
        with self._lock:
            self._strokes.clear()
            self._order.clear()
            for s in strokes:
                self._strokes[s["stroke_id"]] = {
                    "color": s["color"], "width": s["width"], "points": list(s["points"]),
                }
                self._order.append(s["stroke_id"])

    def apply_stroke_event(self, event: dict) -> None:
        sid = event["stroke_id"]
        with self._lock:
            if event["action"] == proto_p8.STROKE_START:
                self._strokes[sid] = {
                    "color": event["color"], "width": event["width"],
                    "points": [[event["x"], event["y"]]],
                }
                self._order.append(sid)
            elif event["action"] == proto_p8.STROKE_POINT:
                if sid in self._strokes:
                    self._strokes[sid]["points"].append([event["x"], event["y"]])
            # "end" needs no visual change - the stroke is already fully drawn

    def clear(self) -> None:
        with self._lock:
            self._strokes.clear()
            self._order.clear()

    def draw_onto(self, frame: np.ndarray) -> np.ndarray:
        """Returns a copy of `frame` with every stroke drawn on top, scaled
        from normalized coordinates to this frame's actual pixel size."""
        h, w = frame.shape[:2]
        with self._lock:
            order = list(self._order)
            strokes = {sid: dict(self._strokes[sid]) for sid in order if sid in self._strokes}

        for sid in order:
            s = strokes.get(sid)
            if not s or len(s["points"]) < 2:
                continue
            pts = [(int(x * w), int(y * h)) for x, y in s["points"]]
            if HIGH_CONTRAST:
                # A black outline under a bright, thick stroke stays visible on both light
                # and dark remote screens; the palette keeps strokes from different people apart.
                width = max(s["width"], a11y.HIGH_CONTRAST_MIN_STROKE)
                color_bgr = a11y.high_contrast_color(s["color"])
                for i in range(1, len(pts)):
                    cv2.line(frame, pts[i - 1], pts[i], (0, 0, 0), width + 4, cv2.LINE_AA)
                for i in range(1, len(pts)):
                    cv2.line(frame, pts[i - 1], pts[i], color_bgr, width, cv2.LINE_AA)
                continue
            color_bgr = _hex_to_bgr(s["color"])
            for i in range(1, len(pts)):
                cv2.line(frame, pts[i - 1], pts[i], color_bgr, s["width"], cv2.LINE_AA)
        return frame


def _hex_to_bgr(hex_color: str) -> tuple:
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    return (b, g, r)


class ViewerState:
    """Small shared-state bag so the video/mouse callback, control thread,
    and chat thread can talk to each other without a tangle of globals."""

    def __init__(self, viewer_id: str, view_only: bool, use_voice: bool):
        self.viewer_id = viewer_id
        self.view_only = view_only
        self.use_voice = use_voice
        self.board = WhiteboardOverlay()
        self.last_frame_size = (1, 1)  # (w, h) of the most recent video frame, for mouse scaling
        self.drawing_mode = False
        self.active_stroke_id = None
        self.mic_muted = False
        self.chat_lines = []  # recent chat, for optional on-screen display
        self.chat_lock = threading.Lock()
        self.pending_branding = None  # Phase 10: set by handle_control_channel, applied
                                       # to the window title by handle_video_stream (the
                                       # thread that actually owns the OpenCV window -
                                       # HighGUI calls from another thread aren't safe)


def send_stroke(control_conn, state: ViewerState, action: str, x_norm: float, y_norm: float) -> None:
    payload = proto_p8.pack_whiteboard_stroke(state.active_stroke_id, state.viewer_id, action,
                                               x_norm, y_norm)
    try:
        proto.send_message(control_conn, proto_p8.MSG_WHITEBOARD_STROKE, payload)
    except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
        pass


def make_mouse_callback(control_conn, input_conn, state: ViewerState):
    """Routes mouse events to whiteboard drawing while SPACE is held, and to
    normal remote-control input otherwise (skipped entirely if view-only)."""
    mouse = MouseController()

    def on_mouse(event, px, py, flags, param):
        w, h = state.last_frame_size
        x_norm, y_norm = (px / w if w else 0.0, py / h if h else 0.0)

        if state.drawing_mode:
            if event == cv2.EVENT_LBUTTONDOWN:
                state.active_stroke_id = f"{state.viewer_id}-{uuid.uuid4().hex[:8]}"
                send_stroke(control_conn, state, proto_p8.STROKE_START, x_norm, y_norm)
                state.board.apply_stroke_event({
                    "stroke_id": state.active_stroke_id, "action": proto_p8.STROKE_START,
                    "x": x_norm, "y": y_norm, "color": "#ff3b30", "width": 3,
                })
            elif event == cv2.EVENT_MOUSEMOVE and flags & cv2.EVENT_FLAG_LBUTTON and state.active_stroke_id:
                send_stroke(control_conn, state, proto_p8.STROKE_POINT, x_norm, y_norm)
                state.board.apply_stroke_event({
                    "stroke_id": state.active_stroke_id, "action": proto_p8.STROKE_POINT,
                    "x": x_norm, "y": y_norm,
                })
            elif event == cv2.EVENT_LBUTTONUP and state.active_stroke_id:
                send_stroke(control_conn, state, proto_p8.STROKE_END, x_norm, y_norm)
                state.active_stroke_id = None
            return

        if state.view_only:
            return

        # Normal remote-control input (unchanged behavior from Phase 4/7)
        if event == cv2.EVENT_MOUSEMOVE:
            proto.send_message(input_conn, proto.MSG_MOUSE_MOVE,
                                proto.pack_mouse_move(x_norm, y_norm))
        elif event in (cv2.EVENT_LBUTTONDOWN, cv2.EVENT_LBUTTONUP,
                       cv2.EVENT_RBUTTONDOWN, cv2.EVENT_RBUTTONUP,
                       cv2.EVENT_MBUTTONDOWN, cv2.EVENT_MBUTTONUP):
            button = ("left" if event in (cv2.EVENT_LBUTTONDOWN, cv2.EVENT_LBUTTONUP) else
                      "right" if event in (cv2.EVENT_RBUTTONDOWN, cv2.EVENT_RBUTTONUP) else "middle")
            pressed = event in (cv2.EVENT_LBUTTONDOWN, cv2.EVENT_RBUTTONDOWN, cv2.EVENT_MBUTTONDOWN)
            proto.send_message(input_conn, proto.MSG_MOUSE_CLICK,
                                proto.pack_mouse_click(x_norm, y_norm, button, pressed))

    return on_mouse


def handle_video_stream(video_conn: socket.socket, control_conn, input_conn,
                         state: ViewerState) -> None:
    """Receives and displays video frames, with the whiteboard overlay drawn
    on top and the mouse callback wired for both control and annotation."""
    window_open = False
    mouse_callback = make_mouse_callback(control_conn, input_conn, state)

    try:
        while True:
            try:
                msg_type, payload = proto.recv_message(video_conn)
            except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
                print("[viewer] [video] disconnected")
                break

            if msg_type != proto.MSG_VIDEO_FRAME:
                continue

            try:
                img = Image.open(io.BytesIO(payload))
                frame = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
                state.last_frame_size = (frame.shape[1], frame.shape[0])
                frame = state.board.draw_onto(frame)

                if state.drawing_mode:
                    banner = t("WHITEBOARD (space=draw, c=clear, release space to control)")
                    if HIGH_CONTRAST:
                        (tw, th), base = cv2.getTextSize(banner, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
                        cv2.rectangle(frame, (0, 0), (tw + 24, th + base + 20), (0, 0, 0), -1)
                        cv2.rectangle(frame, (0, 0), (tw + 24, th + base + 20), (255, 255, 255), 2)
                        cv2.putText(frame, banner, (12, th + 10), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                                    (255, 255, 255), 2, cv2.LINE_AA)
                    else:
                        cv2.putText(frame, banner, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                                    (0, 0, 255), 2, cv2.LINE_AA)

                if not window_open:
                    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
                    cv2.setMouseCallback(WINDOW_NAME, mouse_callback)
                    window_open = True

                if state.pending_branding is not None:
                    # Phase 10: apply here, not from handle_control_channel's thread -
                    # this is the thread that owns the window, and it only just made
                    # sure the window exists (branding can arrive before the first frame).
                    cv2.setWindowTitle(WINDOW_NAME,
                                        f"{state.pending_branding['display_name']} - Remote Desktop")
                    if state.pending_branding.get("support_url"):
                        print(f"[viewer] Connected via {state.pending_branding['display_name']} "
                              f"- support: {state.pending_branding['support_url']}")
                    else:
                        print(f"[viewer] Connected via {state.pending_branding['display_name']}")
                    state.pending_branding = None

                cv2.imshow(WINDOW_NAME, frame)
                key = cv2.waitKey(1) & 0xFF
                if key == 27:  # ESC
                    break
                elif key == ord(" "):
                    state.drawing_mode = not state.drawing_mode
                elif key == ord("c") and state.drawing_mode:
                    state.board.clear()
                    try:
                        proto.send_message(control_conn, proto_p8.MSG_WHITEBOARD_CLEAR,
                                            proto_p8.pack_whiteboard_clear(state.viewer_id))
                    except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
                        pass
                elif key == ord("m"):
                    state.mic_muted = not state.mic_muted
                    print(f"[viewer] Mic {'muted' if state.mic_muted else 'unmuted'}")
                    try:
                        proto.send_message(control_conn, proto_p8.MSG_VOICE_STATE,
                                            proto_p8.pack_voice_state(state.viewer_id, not state.mic_muted))
                    except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
                        pass

            except Exception as e:
                print(f"[viewer] [video] error decoding frame: {e}")

    finally:
        if window_open:
            cv2.destroyWindow(WINDOW_NAME)


def handle_control_channel(control_conn: socket.socket, state: ViewerState) -> None:
    """Handles session info/permissions (Phase 7) plus chat, whiteboard
    sync, and print responses (Phase 8)."""
    print(f"[viewer] Connected as {'view-only' if state.view_only else 'control'} viewer '{state.viewer_id}'")

    threading.Thread(target=report_stats_loop, args=(control_conn,), daemon=True).start()

    try:
        while True:
            try:
                msg_type, payload = proto.recv_message(control_conn)
            except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
                print("[viewer] [control] disconnected")
                break

            if msg_type == proto_p7.MSG_SESSION_INFO:
                info = proto_p7.unpack_session_info(payload)
                print("\n" + t("[session] Current viewers:"))
                for v in info.get("viewers", []):
                    # Phase 12: the host's list_viewers() sends "mode" as a string, the protocol
                    # docs say "view_mode" as an int; Phase 10 only read the latter and crashed.
                    if "view_mode" in v:
                        mode_str = "control" if v["view_mode"] == proto_p7.VIEW_MODE_CONTROL else "view-only"
                    else:
                        mode_str = "control" if v.get("mode") == "control" else "view-only"
                    print(f"  - {v['viewer_id']} ({t(mode_str)}, {v.get('connected_duration', 0):.1f}s, {v['address']})")
                print()

            elif msg_type == proto_p7.MSG_VIEWER_JOINED:
                info = proto_p7.unpack_viewer_joined(payload)
                mode_str = "control" if info["view_mode"] == proto_p7.VIEW_MODE_CONTROL else "view-only"
                ANNOUNCER.announce(t("{viewer} ({mode}) joined from {address}",
                                     viewer=info['viewer_id'], mode=t(mode_str), address=info['address']))

            elif msg_type == proto_p7.MSG_VIEWER_LEFT:
                info = proto_p7.unpack_viewer_left(payload)
                ANNOUNCER.announce(t("{viewer} left the session", viewer=info['viewer_id']))
                if not state.view_only:
                    query_permissions(control_conn)

            elif msg_type == proto_p7.MSG_PERMISSIONS_RESPONSE:
                perms = proto_p7.unpack_permissions_response(payload)
                if not perms["can_send_input"]:
                    ANNOUNCER.announce(t("Warning: lost input control ({reason})", reason=t(perms['reason'])))

            # --- Phase 8 ---
            elif msg_type == proto_p8.MSG_CHAT_TEXT:
                msg = proto_p8.unpack_chat_message(payload)
                line = f"{msg['sender_id']}: {msg['text']}"
                with state.chat_lock:
                    state.chat_lines.append(line)
                print(f"\n[chat] {line}\n> ", end="", flush=True)

            elif msg_type == proto_p8.MSG_WHITEBOARD_STATE:
                snap = proto_p8.unpack_whiteboard_state(payload)
                state.board.load_snapshot(snap.get("strokes", []))

            elif msg_type == proto_p8.MSG_WHITEBOARD_STROKE:
                event = proto_p8.unpack_whiteboard_stroke(payload)
                state.board.apply_stroke_event(event)

            elif msg_type == proto_p8.MSG_WHITEBOARD_CLEAR:
                state.board.clear()

            elif msg_type == proto_p8.MSG_PRINT_RESPONSE:
                resp = proto_p8.unpack_print_response(payload)
                ANNOUNCER.announce(t("Print succeeded: {message}", message=resp['message']) if resp["success"]
                                   else t("Print failed: {message}", message=resp['message']))

            elif msg_type == proto_p8.MSG_VOICE_STATE:
                vs = proto_p8.unpack_voice_state(payload)
                print(f"\n[voice] {vs['viewer_id']} {'unmuted' if vs['mic_on'] else 'muted'}\n> ",
                      end="", flush=True)

            # --- Phase 9 ---
            elif msg_type == proto_p9.MSG_POLICY_DENIED:
                denial = proto_p9.unpack_policy_denied(payload)
                ANNOUNCER.announce(t(denial['reason']))

            # --- Phase 10 ---
            elif msg_type == proto_p10.MSG_BRANDING:
                # Handed to the video thread to apply (see handle_video_stream) -
                # this thread doesn't own the OpenCV window.
                state.pending_branding = proto_p10.unpack_branding(payload)

            # --- Phase 12 ---
            elif msg_type == proto_p12.MSG_FEEDBACK_RESULT:
                show_feedback_result(proto_p12.unpack_feedback_result(payload))

    except Exception as e:
        print(f"[viewer] [control] error: {e}")


def show_feedback_result(res: dict) -> None:
    if not res.get("ok"):
        ANNOUNCER.announce(t("Feedback failed: {error}", error=t(res.get("error", "unknown error"))))
    elif "items" in res:
        items = res["items"]
        if not items:
            ANNOUNCER.announce(t("You haven't sent any feedback yet."))
        for item in items:
            print(f"#{item['id']} [{t(item['status'])}] {item['message'][:80]}")
            if item.get("reply"):
                print("   " + t("Reply: {text}", text=item["reply"]))
    else:
        ANNOUNCER.announce(t("Thanks - your feedback was sent (ticket #{id}).", id=res.get("ticket_id")))


def report_stats_loop(control_conn: socket.socket) -> None:
    while True:
        try:
            time.sleep(1.0)
            proto.send_message(control_conn, proto.MSG_STATS_REPORT,
                                proto.pack_stats_report(15.0, 800.0))
        except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
            break
        except Exception as e:
            print(f"[viewer] [stats] error: {e}")


def query_permissions(control_conn: socket.socket) -> None:
    try:
        proto.send_message(control_conn, proto_p7.MSG_PERMISSIONS_QUERY,
                            proto_p7.pack_permissions_query())
    except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
        pass


# --- Phase 8: voice chat threads ------------------------------------------

def handle_voice_send(audio_conn, state: ViewerState) -> None:
    try:
        mic = audio_chat.MicCapture()
        mic.start()
    except audio_chat.AudioUnavailable as e:
        print(f"[viewer] [voice] mic unavailable, voice send disabled: {e}")
        return

    try:
        while True:
            try:
                pcm = mic.get_chunk(timeout=1.0)
            except queue.Empty:
                continue
            if state.mic_muted:
                continue
            try:
                proto.send_message(audio_conn, proto_p8.MSG_VOICE_FRAME,
                                    proto_p8.pack_voice_frame(pcm, mic.sample_rate, mic.channels))
            except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
                break
    finally:
        mic.stop()


def handle_voice_receive(audio_conn) -> None:
    try:
        playback = audio_chat.AudioPlayback()
        playback.start()
    except audio_chat.AudioUnavailable as e:
        print(f"[viewer] [voice] playback unavailable, voice receive disabled: {e}")
        return

    try:
        while True:
            try:
                msg_type, payload = proto.recv_message(audio_conn)
            except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
                break
            if msg_type == proto_p8.MSG_VOICE_FRAME:
                _, _, pcm = proto_p8.unpack_voice_frame(payload)
                playback.play_chunk(pcm)
    finally:
        playback.stop()


# --- Phase 8: chat / print prompt -----------------------------------------

def chat_input_loop(control_conn, state: ViewerState) -> None:
    """Reads lines from stdin. A bare line is sent as chat; "/print <file>"
    (optionally followed by a printer name) requests a remote print of a
    file already pushed to the host's download directory."""
    print(t("[viewer] Type to chat. \"/print <filename> [printer]\" prints a file already on the host. "
            "\"/feedback [bug|question|idea] <text>\" contacts your admins; \"/replies\" shows their answers. "
            "Ctrl+C to quit typing (video window keeps running)."))
    while True:
        try:
            line = input("> ")
        except (EOFError, KeyboardInterrupt):
            break
        if not line.strip():
            continue

        if line.startswith("/feedback") or line.strip() == "/replies":
            try:
                if line.strip() == "/replies":
                    proto.send_message(control_conn, proto_p12.MSG_FEEDBACK, proto_p12.pack_feedback_list())
                else:
                    words = line[len("/feedback"):].strip().split(maxsplit=1)
                    category = "other"
                    if words and words[0] in ("bug", "question", "idea"):
                        category, words = words[0], words[1:]
                    text = words[0] if words else ""
                    if not text:
                        print(t("Usage: /feedback [bug|question|idea] <text>"))
                        continue
                    proto.send_message(control_conn, proto_p12.MSG_FEEDBACK,
                                        proto_p12.pack_feedback_submit(category, text))
            except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
                print(t("[viewer] Could not send feedback (disconnected)"))
            continue

        if line.startswith("/print "):
            parts = line[len("/print "):].strip().split(maxsplit=1)
            filename = parts[0] if parts else ""
            printer = parts[1] if len(parts) > 1 else ""
            if not filename:
                print("[viewer] Usage: /print <filename> [printer]")
                continue
            try:
                proto.send_message(control_conn, proto_p8.MSG_PRINT_REQUEST,
                                    proto_p8.pack_print_request(filename, printer))
            except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
                print("[viewer] Could not send print request (disconnected)")
            continue

        try:
            proto.send_message(control_conn, proto_p8.MSG_CHAT_TEXT,
                                proto_p8.pack_chat_message(state.viewer_id, line))
        except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
            print("[viewer] Could not send chat message (disconnected)")
            break


def run(args) -> None:
    global HIGH_CONTRAST, ANNOUNCER
    if args.lang:
        i18n.set_language(args.lang)
    HIGH_CONTRAST = args.high_contrast
    ANNOUNCER = a11y.Announcer(speak=args.speak)
    if ANNOUNCER.speak_requested_but_unavailable:
        print(t("[viewer] --speak was requested but no speech engine was found "
                "(install espeak-ng or speech-dispatcher on Linux); status lines are still printed."))
    tls_ctx = make_tls_context()

    if args.relay and not args.device:
        print(t("[viewer] --relay needs --device (the host's device ID)"))
        return
    try:
        pinner = build_pinner(args)
    except ValueError:
        print(t("[viewer] --pin must be a SHA-256 fingerprint (64 hex digits, colons optional)"))
        return 2

    try:
        if args.relay:
            video_conn, input_conn, control_conn, audio_conn = connect_via_relay(
                relay_client.parse_relays(args.relay), args.device, tls_ctx, pinner)
        else:
            video_conn, input_conn, control_conn, audio_conn = connect_to_host(
                args.host, (args.input_port, args.control_port, args.audio_port), tls_ctx, pinner
            )
    except pinning.PinMismatch as e:
        ANNOUNCER.announce(t("WARNING: the certificate for {target} has changed!\n"
                             "Expected: {expected}\nReceived: {actual}\n"
                             "Someone may be intercepting this connection, or the host's certificate was "
                             "regenerated. The connection was refused.\n"
                             "If you have verified the new fingerprint with the host's owner, reconnect "
                             "with --replace-pin.",
                             target=e.key, expected=e.expected, actual=e.actual))
        return 3
    except pinning.PinRejected as e:
        ANNOUNCER.announce(t("[viewer] Not trusting {target}: connection cancelled.", target=e.key))
        return 3
    except pinning.PinStoreError as e:
        ANNOUNCER.announce(t("[viewer] Could not read the saved certificate list {path}: {error}. "
                             "Fix or remove that file to continue.", path=e.path, error=e.cause))
        return 2
    except Exception as e:
        ANNOUNCER.announce(t("Failed to connect: {error}", error=e))
        return

    try:
        result = perform_auth(video_conn, args.id, args.password, args.view_only)
    except Exception as e:
        ANNOUNCER.announce(t("Authentication failed: {error}", error=e))
        for c in (video_conn, input_conn, control_conn, audio_conn):
            c.close()
        return

    if not result.get("approved"):
        ANNOUNCER.announce(t("Connection rejected: {reason}", reason=t(result.get('reason') or "")))
        for c in (video_conn, input_conn, control_conn, audio_conn):
            c.close()
        return

    view_mode = result.get("view_mode", proto_p7.VIEW_MODE_CONTROL)
    is_view_only = (view_mode == proto_p7.VIEW_MODE_VIEW_ONLY)
    ANNOUNCER.announce(t("Connection approved. Mode: {mode}",
                         mode=t("view-only") if is_view_only else t("control")))

    state = ViewerState(args.id, is_view_only, use_voice=not args.no_voice)

    threading.Thread(target=handle_control_channel, args=(control_conn, state),
                      daemon=True, name="control").start()

    threading.Thread(target=chat_input_loop, args=(control_conn, state),
                      daemon=True, name="chat").start()

    if state.use_voice:
        threading.Thread(target=handle_voice_send, args=(audio_conn, state),
                          daemon=True, name="voice-send").start()
        threading.Thread(target=handle_voice_receive, args=(audio_conn,),
                          daemon=True, name="voice-recv").start()

    try:
        handle_video_stream(video_conn, control_conn, input_conn, state)
    except KeyboardInterrupt:
        print("[viewer] Interrupted")
    finally:
        for c in (video_conn, input_conn, control_conn, audio_conn):
            try:
                c.close()
            except Exception:
                pass
        ANNOUNCER.announce(t("Disconnected"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 12 multi-user viewer: chat/whiteboard/print/voice, policy/branding notices, relay failover, feedback, localization, accessibility options")
    parser.add_argument("host", nargs="?", help="host:port, e.g., 192.168.1.100:5000 (omit when using --relay)")
    parser.add_argument("--input-port", type=int, default=5001)
    parser.add_argument("--control-port", type=int, default=5002)
    parser.add_argument("--audio-port", type=int, default=5003)
    parser.add_argument("--id", default="viewer-1", help="viewer ID for logging")
    parser.add_argument("--password", default="", help="password for unattended access")
    parser.add_argument("--view-only", action="store_true", help="join as view-only (no input control)")
    parser.add_argument("--no-voice", action="store_true", help="disable voice chat")
    parser.add_argument("--relay", help="Phase 12: relay(s) as host:port, comma-separated, tried in order "
                                         "(use with --device instead of a host:port)")
    parser.add_argument("--relay-ca", help="CA or certificate file to trust for tls://host:port relays (a private "
                                            "CA or a self-signed relay certificate); default: the system trust "
                                            "store, or $REMOTEBRIDGE_RELAY_CA")
    parser.add_argument("--device", help="Phase 12: the host's device ID when connecting via --relay")
    parser.add_argument("--lang", help="Phase 12: interface language code, e.g. es")
    parser.add_argument("--high-contrast", action="store_true",
                        help="Phase 12: draw whiteboard strokes and the mode banner in a high-contrast style")
    parser.add_argument("--pin", help="SHA-256 fingerprint of the host's certificate, verified out of band "
                                      "(the host prints it at startup). The connection is refused if the "
                                      "host presents anything else")
    parser.add_argument("--replace-pin", action="store_true",
                        help="accept a host's NEW certificate and replace the remembered one "
                             "(only after verifying the change with the host's owner)")
    parser.add_argument("--known-hosts", default=None,
                        help="where remembered certificate fingerprints are kept "
                             "(default ~/.remotebridge/known_hosts.json or $REMOTEBRIDGE_KNOWN_HOSTS)")
    parser.add_argument("--speak", action="store_true",
                        help="Phase 12: also speak status lines via the OS speech engine")
    args = parser.parse_args()
    if args.relay_ca:
        relay_client.set_ca_file(args.relay_ca)
    if not args.relay and not args.host:
        parser.error("either a host:port or --relay + --device is required")

    sys.exit(run(args) or 0)
