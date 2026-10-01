"""
Phase 4 - Host application.

Adds on top of Phase 3 (control channel: clipboard + files):
  1. Adaptive bitrate/frame rate - the viewer reports what it's
     actually receiving (see adaptive.py) and video_loop's quality/fps
     track an AdaptiveBitrateController instead of staying fixed at
     the CLI's --quality/--fps for the whole session.
  2. Multi-monitor support - the viewer can list and switch which of
     the host's monitors is being captured (see monitors.py). The
     active monitor is read fresh by video_loop every frame, so a
     switch takes effect on the very next capture.
  3. Auto-reconnect - by default the host doesn't exit after one
     viewer session. It goes back to listening (or re-registers on the
     relay) for the next connection, whether the previous one ended in
     a clean disconnect, a network drop, or a rejected auth attempt.
     Pass --single-session to restore the old one-and-done behavior.

Direct mode (same LAN):
    python3 host.py --video-port 5000 --input-port 5001 --control-port 5002

Relay mode (works across networks, connect by ID instead of IP):
    python3 host.py --relay relay.example.com:6000 --id my-device-1
    (registers "my-device-1-video", "-input", and "-control" on the relay)
"""

import argparse
import io
import socket
import ssl
import threading
import time

from PIL import Image

import adaptive
import auth
import clipboard_sync
import console
import control_loop
import file_transfer
import monitors
import protocol as proto
import relay_client
import session_log

CERT_FILE = "host_cert.pem"
KEY_FILE = "host_key.pem"


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
    return ctx


def get_raw_channel_direct(port: int, label: str) -> socket.socket:
    """Direct mode: listen and accept one incoming connection."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(1)
    print(f"[host] [{label}] listening on port {port}, waiting for viewer...")
    conn, addr = server.accept()
    print(f"[host] [{label}] viewer connected from {addr}")
    server.close()
    return conn


def get_raw_channel_relay(relay: tuple, name: str, label: str) -> socket.socket:
    """Relay mode: register `name` on one specific relay (the one the video
    channel already paired through - a session never straddles relays)."""
    conn = relay_client.host_register_on(relay, name)
    print(f"[host] [{label}] registered as '{name}' on relay {relay[0]}:{relay[1]}, waiting...")
    return conn


def capture_frame(sct, monitor, quality: int) -> bytes:
    raw = sct.grab(monitor)
    img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def video_loop(conn, active_monitor: monitors.ActiveMonitor, bitrate: adaptive.AdaptiveBitrateController) -> str:
    """Returns "dropped" when the viewer disconnects (network drop or
    clean close - the two channels look the same from here); raises
    SystemExit only for a genuinely fatal setup problem."""
    try:
        import mss
    except ImportError:
        raise SystemExit("Missing dependency 'mss'. Install with: pip install mss")

    with mss.mss() as sct:
        try:
            while True:
                start = time.time()
                quality, fps = bitrate.get_settings()
                monitor = sct.monitors[active_monitor.get()]
                frame = capture_frame(sct, monitor, quality)
                proto.send_message(conn, proto.MSG_VIDEO_FRAME, frame)
                interval = 1.0 / fps if fps > 0 else 0
                elapsed = time.time() - start
                if interval > elapsed:
                    time.sleep(interval - elapsed)
        except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
            print("[host] [video] viewer disconnected")
            return "dropped"


def input_loop(conn) -> None:
    from pynput.mouse import Controller as MouseController, Button
    from pynput.keyboard import Controller as KeyboardController, Key

    mouse = MouseController()
    keyboard = KeyboardController()

    # Host's own screen resolution, used to convert normalized
    # coordinates from the viewer back into real pixel coordinates.
    try:
        import mss
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            screen_w, screen_h = monitor["width"], monitor["height"]
    except ImportError:
        screen_w, screen_h = 1920, 1080  # fallback guess

    button_map = {"left": Button.left, "right": Button.right, "middle": Button.middle}

    def resolve_key(name: str):
        if len(name) == 1:
            return name
        return getattr(Key, name, None)

    try:
        while True:
            msg_type, payload = proto.recv_message(conn)

            if msg_type == proto.MSG_MOUSE_MOVE:
                x_norm, y_norm = proto.unpack_mouse_move(payload)
                mouse.position = (x_norm * screen_w, y_norm * screen_h)

            elif msg_type == proto.MSG_MOUSE_CLICK:
                x_norm, y_norm, button, pressed = proto.unpack_mouse_click(payload)
                mouse.position = (x_norm * screen_w, y_norm * screen_h)
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

    except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
        print("[host] [input] viewer disconnected")


def run_one_session(args, tls_ctx, config, relays) -> None:
    """Accepts (or waits for, on the relay) exactly one viewer session,
    runs it to completion, and returns. Never raises for a dropped
    connection or a rejected auth attempt - only for fatal setup
    problems (missing cert, missing dependency) - so the caller's loop
    can always go back to waiting for the next viewer."""
    if args.relay:
        print(f"[host] Device ID: {args.id}  (viewer connects with this ID via the relay)")
        # Phase 12: registers on every configured relay and takes whichever a
        # viewer reaches first; the rest are withdrawn.
        raw_video, chosen_relay = relay_client.host_wait_paired(
            relays, f"{args.id}-video", log=lambda m: print(f"[host] [video] {m}"))
        peer_addr = f"{chosen_relay[0]}:{chosen_relay[1]}"
    else:
        print(f"[host] Your LAN IP is likely: {get_lan_ip()}")
        raw_video = get_raw_channel_direct(args.video_port, "video")
        peer_addr = raw_video.getpeername()

    video_conn = tls_ctx.wrap_socket(raw_video, server_side=True)
    print("[host] TLS handshake complete on video channel. Waiting for authentication...")

    result = auth.perform_host_auth(video_conn, config, peer_addr)
    session_log.log_event(
        "attempt", path=args.session_log,
        viewer_id=result["viewer_id"], address=str(peer_addr),
        decision=result["decision"], reason=result["reason"],
    )

    try:
        proto.send_message(video_conn, proto.MSG_AUTH_RESPONSE,
                            proto.pack_auth_response(result["approved"], result["reason"]))
    except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError):
        print("[host] Viewer disconnected during authentication.")
        return

    if not result["approved"]:
        print(f"[host] Connection rejected ({result['decision']}): {result['reason']}")
        video_conn.close()
        return

    print(f"[host] Connection approved ({result['decision']}) for viewer '{result['viewer_id']}'.")

    if args.relay:
        raw_input = get_raw_channel_relay(chosen_relay, f"{args.id}-input", "input")
    else:
        raw_input = get_raw_channel_direct(args.input_port, "input")
    input_conn = tls_ctx.wrap_socket(raw_input, server_side=True)
    print("[host] TLS handshake complete on input channel. Session starting.")

    if args.relay:
        raw_control = get_raw_channel_relay(chosen_relay, f"{args.id}-control", "control")
    else:
        raw_control = get_raw_channel_direct(args.control_port, "control")
    control_conn = tls_ctx.wrap_socket(raw_control, server_side=True)
    print("[host] TLS handshake complete on control channel (files/clipboard/monitors).")

    control_send_lock = threading.Lock()

    def control_send(msg_type, payload):
        with control_send_lock:
            proto.send_message(control_conn, msg_type, payload)

    clipboard = clipboard_sync.ClipboardSync(control_conn, control_send_lock, save_dir=args.download_dir)
    file_session = file_transfer.FileTransferSession(control_conn, control_send_lock, download_dir=args.download_dir)
    active_monitor = monitors.ActiveMonitor(index=1)
    monitor_host = monitors.MonitorHost(active_monitor, control_send)
    bitrate = adaptive.AdaptiveBitrateController(args.quality, args.fps, enabled=not args.no_adaptive)

    threading.Thread(target=clipboard.watch_loop, daemon=True).start()
    threading.Thread(target=control_loop.control_reader_loop,
                      args=(control_conn, clipboard, file_session, bitrate, monitor_host), daemon=True).start()
    threading.Thread(target=console.console_loop, args=(file_session,), daemon=True).start()

    timer = session_log.SessionTimer()
    session_log.log_event("start", path=args.session_log,
                           viewer_id=result["viewer_id"], address=str(peer_addr))

    t_input = threading.Thread(target=input_loop, args=(input_conn,), daemon=True)
    t_input.start()

    video_loop(video_conn, active_monitor, bitrate)  # runs on main thread until dropped

    clipboard.stop()
    session_log.log_event("end", path=args.session_log,
                           viewer_id=result["viewer_id"], address=str(peer_addr),
                           duration_seconds=timer.elapsed())


def run(args) -> None:
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

    while True:
        run_one_session(args, tls_ctx, config, relays)
        if args.single_session:
            break
        print("[host] Session ended - waiting for the next connection "
              "(pass --single-session to exit instead, or Ctrl+C to stop the host).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 4 remote-desktop host")
    parser.add_argument("--video-port", type=int, default=5000)
    parser.add_argument("--input-port", type=int, default=5001)
    parser.add_argument("--control-port", type=int, default=5002)
    parser.add_argument("--quality", type=int, default=60, help="starting JPEG quality 1-95")
    parser.add_argument("--fps", type=int, default=15, help="starting frames per second")
    parser.add_argument("--no-adaptive", action="store_true",
                         help="disable adaptive bitrate; keep --quality/--fps fixed all session")
    parser.add_argument("--single-session", action="store_true",
                         help="exit after one viewer session instead of waiting for the next one")
    parser.add_argument("--relay", help="relay server as host:port, e.g. relay.example.com:6000. Phase 12: a "
                                         "comma-separated list (a:6000,b:6000) registers on all of them for failover")
    parser.add_argument("--id", help="device ID to register on the relay (required with --relay)")
    parser.add_argument("--config", default=auth.CONFIG_PATH,
                         help="path to host_config.json (see configure_host.py)")
    parser.add_argument("--session-log", default=session_log.LOG_PATH,
                         help="path to append session log entries to")
    parser.add_argument("--download-dir", default=".",
                         help="where files pushed/pulled by the peer are saved")
    args = parser.parse_args()

    run(args)
