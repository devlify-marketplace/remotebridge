"""
Phase 4 - Viewer application.

Adds on top of Phase 3 (control channel: clipboard + files):
  1. Adaptive bitrate feedback - once a second, reports what it
     actually received (frames/sec, kbps) to the host over the control
     channel so the host's quality/fps can adapt (see adaptive.py).
  2. Multi-monitor support - `monitors`/`monitor <n>` console commands
     to list and switch which of the host's displays is being sent
     (see monitors.py).
  3. Session recording - `record <path>`/`record stop` console
     commands write the displayed video to a local file (see
     recorder.py).
  4. Auto-reconnect - a network drop (as opposed to pressing 'q' to
     quit) no longer ends the process. It retries the connection with
     a growing backoff, up to --max-backoff seconds between attempts,
     and resets to the minimum backoff as soon as a session connects
     and authenticates successfully. Pass --no-auto-reconnect to
     restore the old exit-on-drop behavior.

Direct mode (same LAN):
    python3 viewer.py --host 192.168.1.23 --video-port 5000 --input-port 5001 --control-port 5002

Relay mode (connect by ID, works across networks):
    python3 viewer.py --relay relay.example.com:6000 --id my-device-1

Keyboard support in this phase is limited to what OpenCV's window can
capture (single keypresses via waitKey; no reliable key-up/held-key
tracking, no modifier combos). A proper GUI toolkit replaces this in a
later phase - see README.md.

Press Ctrl+C in the terminal to quit.
"""

import argparse
import io
import socket
import ssl
import threading
import time

import cv2
import numpy as np
from PIL import Image

import adaptive
import clipboard_sync
import console
import control_loop
import file_transfer
import monitors
import protocol as proto
import recorder
import relay_client

# Map a subset of ASCII key codes from cv2.waitKey to pynput-style names
# used by host.py's resolve_key(). Anything not in this map falls back
# to sending the raw single character.
_SPECIAL_KEYS = {
    13: "enter",
    27: "esc",
    8: "backspace",
    9: "tab",
    32: "space",
}


def get_raw_channel_direct(host: str, port: int, retries: int = 30, delay: float = 0.2) -> socket.socket:
    """
    Connect to host:port, retrying briefly on ConnectionRefusedError.

    The host opens its video port and its input port a moment apart
    (it accepts the video connection before it even binds the input
    port), so a fast viewer can otherwise race ahead and get refused
    connecting to the input port. A short retry makes that a non-issue
    without requiring any coordination between the two sides.

    Raises ConnectionError (not SystemExit) once retries are
    exhausted, so the caller's auto-reconnect loop can treat it the
    same as any other drop and try again later instead of the whole
    process exiting.
    """
    last_error = None
    for _ in range(retries):
        try:
            return socket.create_connection((host, port))
        except ConnectionRefusedError as exc:
            last_error = exc
            time.sleep(delay)
    raise ConnectionError(f"Could not connect to {host}:{port} after {retries} attempts: {last_error}")


def get_raw_channel_relay(relay: tuple, name: str) -> socket.socket:
    """CONNECT to `name` on one specific relay (the one the video channel
    already went through)."""
    return relay_client.viewer_connect_one(relay, name)


def make_tls_client_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # Phase 1 does not yet verify the host's certificate - it just
    # encrypts the channel against passive eavesdropping. Real
    # trust-on-first-use fingerprint verification is a later phase.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def video_display_loop(conn, window_name: str, mouse_state: dict,
                        stats: adaptive.StatsReporter, control_send, rec: recorder.SessionRecorder) -> str:
    """Returns "quit" if the user pressed 'q', "dropped" on a
    connection error (so the caller's auto-reconnect loop can retry)."""
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)

    def on_mouse(event, x, y, flags, param):
        win_w, win_h = mouse_state.get("win_size", (1, 1))
        x_norm = max(0.0, min(1.0, x / max(win_w, 1)))
        y_norm = max(0.0, min(1.0, y / max(win_h, 1)))
        input_conn = mouse_state["input_conn"]

        try:
            if event == cv2.EVENT_MOUSEMOVE:
                proto.send_message(input_conn, proto.MSG_MOUSE_MOVE, proto.pack_mouse_move(x_norm, y_norm))
            elif event == cv2.EVENT_LBUTTONDOWN:
                proto.send_message(input_conn, proto.MSG_MOUSE_CLICK, proto.pack_mouse_click(x_norm, y_norm, "left", True))
            elif event == cv2.EVENT_LBUTTONUP:
                proto.send_message(input_conn, proto.MSG_MOUSE_CLICK, proto.pack_mouse_click(x_norm, y_norm, "left", False))
            elif event == cv2.EVENT_RBUTTONDOWN:
                proto.send_message(input_conn, proto.MSG_MOUSE_CLICK, proto.pack_mouse_click(x_norm, y_norm, "right", True))
            elif event == cv2.EVENT_RBUTTONUP:
                proto.send_message(input_conn, proto.MSG_MOUSE_CLICK, proto.pack_mouse_click(x_norm, y_norm, "right", False))
            elif event == cv2.EVENT_MOUSEWHEEL:
                delta = 1.0 if flags > 0 else -1.0
                proto.send_message(input_conn, proto.MSG_MOUSE_SCROLL, proto.pack_mouse_scroll(0.0, delta))
        except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
            pass  # input channel dropped; video loop below will notice and exit too

    cv2.setMouseCallback(window_name, on_mouse)

    def report_stats(measured_fps, measured_kbps):
        control_send(proto.MSG_STATS_REPORT, proto.pack_stats_report(measured_fps, measured_kbps))

    try:
        while True:
            msg_type, payload = proto.recv_message(conn)
            if msg_type != proto.MSG_VIDEO_FRAME:
                continue

            stats.note_frame(len(payload))
            stats.maybe_report(report_stats)

            image = Image.open(io.BytesIO(payload)).convert("RGB")
            frame = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
            h, w = frame.shape[:2]
            mouse_state["win_size"] = (w, h)

            if rec.active:
                rec.write_frame(frame)

            cv2.imshow(window_name, frame)
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q") and cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) >= 1:
                # Reserve 'q' to quit, like Phase 0. Everything else
                # captured below is forwarded to the host as a keypress.
                return "quit"
            elif key != 255:
                key_name = _SPECIAL_KEYS.get(key, chr(key) if 32 <= key < 127 else None)
                if key_name:
                    input_conn = mouse_state["input_conn"]
                    try:
                        proto.send_message(input_conn, proto.MSG_KEY_EVENT, proto.pack_key_event(key_name, True))
                        proto.send_message(input_conn, proto.MSG_KEY_EVENT, proto.pack_key_event(key_name, False))
                    except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
                        pass
    except (ConnectionError, OSError, ssl.SSLError):
        print("[viewer] connection ended")
        return "dropped"
    finally:
        if rec.active:
            path = rec.stop()
            print(f"[viewer] recording stopped (connection ended), saved to {path}")
        cv2.destroyAllWindows()


def run_one_session(args, tls_ctx, relays, connection_state: dict) -> str:
    """Runs one connection attempt through to its end. Returns "quit"
    (user pressed 'q' - stop for good) or "dropped" (network-level
    problem - the caller's auto-reconnect loop may retry). Raises
    SystemExit only for a genuine, non-retryable rejection (bad
    password/2FA/whitelist) so those aren't retried forever.

    Sets connection_state["connected"] = True as soon as every channel
    is up and authenticated, so the caller can tell "never got
    anywhere, keep backing off" apart from "was working fine and then
    dropped, don't punish it with a long wait"."""
    if args.relay:
        shown = ", ".join(f"{h}:{p}" for h, p in relays)
        print(f"[viewer] Connecting to '{args.id}' via relay(s) {shown} ...")
        # Phase 12: try each relay in turn; the input/control channels then
        # stay on whichever one paired.
        raw_video, chosen_relay = relay_client.viewer_connect(relays, f"{args.id}-video")
        print(f"[viewer] paired through relay {chosen_relay[0]}:{chosen_relay[1]}")
    else:
        print(f"[viewer] Connecting to {args.host} (video:{args.video_port}) ...")
        raw_video = get_raw_channel_direct(args.host, args.video_port)

    video_conn = tls_ctx.wrap_socket(raw_video)
    print("[viewer] TLS handshake complete. Authenticating...")

    viewer_id = args.viewer_id or f"viewer-{socket.gethostname()}"
    proto.send_message(
        video_conn, proto.MSG_AUTH_REQUEST,
        proto.pack_auth_request(viewer_id, args.password or "", args.totp or ""),
    )
    msg_type, payload = proto.recv_message(video_conn)
    if msg_type != proto.MSG_AUTH_RESPONSE:
        raise SystemExit("[viewer] Unexpected message from host during authentication.")
    resp = proto.unpack_auth_response(payload)
    if not resp["approved"]:
        raise SystemExit(f"[viewer] Connection rejected by host: {resp['reason']}")
    print(f"[viewer] Connection approved: {resp['reason']}")

    if args.relay:
        raw_input = get_raw_channel_relay(chosen_relay, f"{args.id}-input")
    else:
        raw_input = get_raw_channel_direct(args.host, args.input_port)
    input_conn = tls_ctx.wrap_socket(raw_input)
    print("[viewer] Connected. Click into the window to control the host. Press 'q' to quit.")

    if args.relay:
        raw_control = get_raw_channel_relay(chosen_relay, f"{args.id}-control")
    else:
        raw_control = get_raw_channel_direct(args.host, args.control_port)
    control_conn = tls_ctx.wrap_socket(raw_control)
    print("[viewer] Control channel connected (files/clipboard/monitors).")
    connection_state["connected"] = True

    control_send_lock = threading.Lock()

    def control_send(msg_type, payload):
        with control_send_lock:
            proto.send_message(control_conn, msg_type, payload)

    clipboard = clipboard_sync.ClipboardSync(control_conn, control_send_lock, save_dir=args.download_dir)
    file_session = file_transfer.FileTransferSession(control_conn, control_send_lock, download_dir=args.download_dir)
    monitor_client = monitors.MonitorClient(control_send)
    stats = adaptive.StatsReporter()
    rec = recorder.SessionRecorder()

    threading.Thread(target=clipboard.watch_loop, daemon=True).start()
    threading.Thread(target=control_loop.control_reader_loop,
                      args=(control_conn, clipboard, file_session, None, monitor_client), daemon=True).start()

    mouse_state = {"input_conn": input_conn, "win_size": (1, 1)}
    threading.Thread(target=console.console_loop,
                      args=(file_session, monitor_client, rec, mouse_state), daemon=True).start()

    outcome = video_display_loop(video_conn, "Remote Desktop (Phase 4) - press q to quit",
                                  mouse_state, stats, control_send, rec)
    clipboard.stop()
    return outcome


def run(args) -> None:
    tls_ctx = make_tls_client_context()

    relays = []
    if args.relay:
        try:
            relays = relay_client.parse_relays(args.relay)
        except ValueError as e:
            raise SystemExit(str(e))

    min_backoff = 1.0
    backoff = min_backoff
    while True:
        connection_state = {"connected": False}
        try:
            outcome = run_one_session(args, tls_ctx, relays, connection_state)
        except (ConnectionError, OSError, ssl.SSLError) as exc:
            print(f"[viewer] connection attempt failed: {exc}")
            outcome = "dropped"

        if connection_state["connected"]:
            # It worked at least once this attempt - a fresh drop right
            # after should retry promptly, not inherit a long wait from
            # earlier failed attempts.
            backoff = min_backoff

        if outcome == "quit" or args.no_auto_reconnect:
            break

        print(f"[viewer] Reconnecting in {backoff:.0f}s... (Ctrl+C to give up)")
        time.sleep(backoff)
        backoff = min(args.max_backoff, backoff * 2)
    print("[viewer] stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 4 remote-desktop viewer")
    parser.add_argument("--host", help="Host machine's LAN IP address (direct mode)")
    parser.add_argument("--video-port", type=int, default=5000)
    parser.add_argument("--input-port", type=int, default=5001)
    parser.add_argument("--control-port", type=int, default=5002)
    parser.add_argument("--relay", help="relay server as host:port (relay mode). Phase 12: a comma-separated "
                                         "list (a:6000,b:6000) is tried in order for failover")
    parser.add_argument("--id", help="device ID to connect to (required with --relay)")
    parser.add_argument("--viewer-id", help="identity sent to the host (checked against its whitelist, if any)")
    parser.add_argument("--password", help="unattended-access password, if the host requires one")
    parser.add_argument("--totp", help="current 6-digit 2FA code, if the host has 2FA enabled")
    parser.add_argument("--download-dir", default=".",
                         help="where files pushed/pulled by the host are saved")
    parser.add_argument("--no-auto-reconnect", action="store_true",
                         help="exit on a network drop instead of retrying the connection")
    parser.add_argument("--max-backoff", type=float, default=30.0,
                         help="cap, in seconds, on the wait between reconnect attempts")
    args = parser.parse_args()

    if not args.relay and not args.host:
        parser.error("either --host (direct mode) or --relay + --id (relay mode) is required")

    run(args)
