"""
Phase 3-4 - control-channel reader loop.

Reads one message at a time from the control channel and routes it to
whichever handler owns that message type - clipboard sync, file
transfer, or (Phase 4) adaptive-bitrate stats reports and multi-monitor
list/switch requests. Runs as its own thread; other threads (the
clipboard watcher, the console's send_file/request_file calls, the
viewer's per-frame stats reporting) write to the same connection
through the shared send_lock those objects already take.

`adaptive` and `monitor_handler` are optional so the same loop keeps
working unchanged for callers that don't pass them:
  - host.py passes `adaptive` (an AdaptiveBitrateController) to absorb
    MSG_STATS_REPORT, and `monitor_handler` as a MonitorHost.
  - viewer.py passes `monitor_handler` as a MonitorClient (it never
    receives stats reports, so `adaptive` stays None there).
"""

import ssl

import protocol as proto


def control_reader_loop(conn, clipboard, file_session, adaptive=None, monitor_handler=None) -> None:
    try:
        while True:
            msg_type, payload = proto.recv_message(conn)
            if msg_type == proto.MSG_CLIPBOARD_TEXT:
                clipboard.handle_text(payload)
            elif msg_type == proto.MSG_CLIPBOARD_IMAGE:
                clipboard.handle_image(payload)
            elif adaptive is not None and msg_type == proto.MSG_STATS_REPORT:
                adaptive.record_report(*proto.unpack_stats_report(payload))
            elif monitor_handler is not None and monitor_handler.dispatch(msg_type, payload):
                pass
            elif not file_session.dispatch(msg_type, payload):
                print(f"[control] ignoring unrecognized message type 0x{msg_type:02x}")
    except (BrokenPipeError, ConnectionResetError, ConnectionError, ssl.SSLError, OSError):
        print("[control] control channel closed.")
