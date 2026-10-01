"""
Phase 4 - multi-monitor support.

The host enumerates its own monitors with mss; the viewer requests the
list and can ask to switch which one is being captured. Both sides
talk over the control channel using the request/response pattern
already established by file_transfer.py's directory listing.

mss numbers monitors starting at 1 (index 0 is the virtual bounding
box of all of them combined) - that numbering is kept as-is so it
lines up with what host.py's own mss.monitors list uses for capture.
"""

import threading

import protocol as proto


def list_monitors() -> list:
    try:
        import mss
        with mss.mss() as sct:
            return [
                {"index": i, "width": m["width"], "height": m["height"],
                 "left": m["left"], "top": m["top"]}
                for i, m in enumerate(sct.monitors) if i > 0
            ]
    except Exception:
        return [{"index": 1, "width": 1920, "height": 1080, "left": 0, "top": 0}]


class ActiveMonitor:
    """Thread-safe holder for which monitor index the host's video_loop
    should currently be capturing. video_loop reads it once per frame;
    MonitorHost writes it when a switch request arrives."""

    def __init__(self, index: int = 1):
        self._lock = threading.Lock()
        self._index = index

    def get(self) -> int:
        with self._lock:
            return self._index

    def set(self, index: int) -> bool:
        valid_indices = {m["index"] for m in list_monitors()}
        if index not in valid_indices:
            return False
        with self._lock:
            self._index = index
        return True


class MonitorHost:
    """Host-side control-channel handler: answers list requests and
    applies switch requests to the shared ActiveMonitor."""

    def __init__(self, active_monitor: ActiveMonitor, send_fn):
        self.active_monitor = active_monitor
        self._send = send_fn

    def dispatch(self, msg_type: int, payload: bytes) -> bool:
        if msg_type == proto.MSG_MONITOR_LIST_REQUEST:
            monitors = list_monitors()
            self._send(proto.MSG_MONITOR_LIST_RESPONSE,
                        proto.pack_monitor_list_response(monitors, self.active_monitor.get()))
        elif msg_type == proto.MSG_MONITOR_SWITCH:
            req = proto.unpack_monitor_switch(payload)
            ok = self.active_monitor.set(req.get("index", -1))
            tag = "ok" if ok else "unknown monitor index, ignored"
            print(f"[host] monitor switch to {req.get('index')}: {tag}")
        else:
            return False
        return True


class MonitorClient:
    """Viewer-side: request the host's monitor list (blocking, with a
    timeout, like file_transfer's request_listing), or fire off a
    switch request. Its dispatch() only needs to catch the list
    response - the reader loop calls it for every control message."""

    def __init__(self, send_fn):
        self._send = send_fn
        self._pending_reply = None
        self._event = threading.Event()

    def dispatch(self, msg_type: int, payload: bytes) -> bool:
        if msg_type == proto.MSG_MONITOR_LIST_RESPONSE:
            self._pending_reply = proto.unpack_monitor_list_response(payload)
            self._event.set()
            return True
        return False

    def request_list(self, timeout: float = 10.0):
        self._pending_reply = None
        self._event.clear()
        self._send(proto.MSG_MONITOR_LIST_REQUEST, proto.pack_monitor_list_request())
        if self._event.wait(timeout):
            return self._pending_reply
        return None

    def switch(self, index: int) -> None:
        self._send(proto.MSG_MONITOR_SWITCH, proto.pack_monitor_switch(index))
