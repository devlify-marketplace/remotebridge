"""
Phase 3 - file transfer and remote directory browsing.

Runs over the control channel (a third TCP connection, alongside video
and input), symmetric in both directions: either side can list the
other's current directory, push a file to them, or ask them to push a
file to us.

There's no OS-level drag-and-drop here - this app has no native window
to drop files onto (the viewer's display is an OpenCV video window,
not a full GUI toolkit; see viewer.py's docstring). The two-pane file
manager for this phase is `console.py`'s interactive commands: your
local listing (`lls`) and the peer's listing (`ls`), with `send`/`get`
to move files between them. A real drag-and-drop GUI needs a proper
GUI toolkit, which isn't part of the terminal + OpenCV stack until a
later phase.

Resume/retry: an interrupted incoming transfer leaves a `<name>.part`
file on disk containing whatever arrived. Sending the same file again
(same filename, same destination) has the receiver report how many
bytes of the `.part` file it already has; the sender seeks to that
offset before resuming, instead of restarting from byte zero.
"""

import os
import secrets
import threading

import protocol as proto

CHUNK_SIZE = 65536


def _new_transfer_id() -> int:
    return secrets.randbits(32)


class FileTransferSession:
    """
    One instance per control-channel connection. `dispatch()` is meant
    to be called from the channel's single reader loop (see
    control_loop.py) for every message that isn't a clipboard message.
    Sending (push/pull) is driven separately by console commands.
    """

    def __init__(self, conn, send_lock: threading.Lock, download_dir: str = "."):
        self.conn = conn
        self.send_lock = send_lock
        self.download_dir = download_dir
        self.cwd = os.getcwd()

        self._incoming = {}          # transfer_id -> receiving-file state
        self._pending_list_reply = None
        self._list_event = threading.Event()
        self._accept_waiters = {}    # transfer_id -> (Event, result dict)
        self._lock = threading.Lock()

    def _send(self, msg_type: int, payload: bytes) -> None:
        with self.send_lock:
            proto.send_message(self.conn, msg_type, payload)

    # --- dispatch incoming control-channel messages ---------------------
    def dispatch(self, msg_type: int, payload: bytes) -> bool:
        """Returns True if this message type belongs to file transfer
        and was handled, False otherwise (caller tries other handlers)."""
        if msg_type == proto.MSG_FILE_LIST_REQUEST:
            self._handle_list_request(payload)
        elif msg_type == proto.MSG_FILE_LIST_RESPONSE:
            self._pending_list_reply = proto.unpack_file_list_response(payload)
            self._list_event.set()
        elif msg_type == proto.MSG_FILE_SEND_REQUEST:
            self._handle_send_request(payload)
        elif msg_type == proto.MSG_FILE_SEND_ACCEPT:
            info = proto.unpack_file_send_accept(payload)
            waiter = self._accept_waiters.get(info["transfer_id"])
            if waiter:
                ev, result = waiter
                result.update(info)
                ev.set()
        elif msg_type == proto.MSG_FILE_CHUNK:
            self._handle_chunk(payload)
        elif msg_type == proto.MSG_FILE_COMPLETE:
            self._handle_complete(payload)
        elif msg_type == proto.MSG_FILE_PULL_REQUEST:
            self._handle_pull_request(payload)
        else:
            return False
        return True

    # --- directory listing -----------------------------------------------
    def request_listing(self, remote_dir: str = ".", timeout: float = 10.0):
        self._pending_list_reply = None
        self._list_event.clear()
        self._send(proto.MSG_FILE_LIST_REQUEST, proto.pack_file_list_request(remote_dir))
        if self._list_event.wait(timeout):
            return self._pending_list_reply
        return None

    def _handle_list_request(self, payload: bytes) -> None:
        req = proto.unpack_file_list_request(payload)
        target = req.get("dir") or self.cwd
        entries = []
        try:
            with os.scandir(target) as it:
                for entry in it:
                    try:
                        entries.append({
                            "name": entry.name,
                            "is_dir": entry.is_dir(),
                            "size": entry.stat().st_size if entry.is_file() else 0,
                        })
                    except OSError:
                        continue
        except OSError as exc:
            target = f"{target} (error: {exc})"
        self._send(proto.MSG_FILE_LIST_RESPONSE, proto.pack_file_list_response(target, entries))

    def list_local(self, local_dir: str = None):
        target = local_dir or self.cwd
        try:
            with os.scandir(target) as it:
                entries = [
                    {"name": e.name, "is_dir": e.is_dir(), "size": e.stat().st_size if e.is_file() else 0}
                    for e in it
                ]
            return target, entries
        except OSError as exc:
            return f"{target} (error: {exc})", []

    # --- sending (pushing) a local file to the peer -----------------------
    def send_file(self, local_path: str, remote_name: str = None) -> None:
        if not os.path.isfile(local_path):
            print(f"[files] no such local file: {local_path}")
            return

        remote_name = remote_name or os.path.basename(local_path)
        size = os.path.getsize(local_path)
        transfer_id = _new_transfer_id()

        ev = threading.Event()
        result = {}
        self._accept_waiters[transfer_id] = (ev, result)
        self._send(proto.MSG_FILE_SEND_REQUEST, proto.pack_file_send_request(transfer_id, remote_name, size))
        print(f"[files] offered '{remote_name}' ({size} bytes) to peer, waiting for accept...")

        got_reply = ev.wait(30)
        self._accept_waiters.pop(transfer_id, None)
        if not got_reply:
            print("[files] peer did not respond to the transfer offer - giving up.")
            return
        if not result.get("accepted"):
            print(f"[files] peer declined: {result.get('reason', 'no reason given')}")
            return

        resume_offset = result.get("resume_offset", 0)
        try:
            with open(local_path, "rb") as f:
                if resume_offset:
                    f.seek(resume_offset)
                    print(f"[files] resuming '{remote_name}' from byte {resume_offset}")
                sent = resume_offset
                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    self._send(proto.MSG_FILE_CHUNK, proto.pack_file_chunk(transfer_id, sent, chunk))
                    sent += len(chunk)
            self._send(proto.MSG_FILE_COMPLETE, proto.pack_file_complete(transfer_id, True, "sender finished"))
            print(f"[files] sent '{remote_name}' ({sent} bytes total)")
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            print(f"[files] transfer of '{remote_name}' interrupted: {exc}. "
                  f"Run 'send {local_path}' again to resume.")

    # --- receiving a file the peer pushes to us ---------------------------
    def _handle_send_request(self, payload: bytes) -> None:
        req = proto.unpack_file_send_request(payload)
        transfer_id, filename, size = req["transfer_id"], req["filename"], req["size"]
        safe_name = os.path.basename(filename)  # never trust a path from the peer
        dest_path = os.path.join(self.download_dir, safe_name)
        part_path = dest_path + ".part"

        resume_offset = 0
        if os.path.exists(part_path):
            existing = os.path.getsize(part_path)
            if existing <= size:
                resume_offset = existing
            # else: stale/corrupt partial bigger than the real file - restart from 0

        try:
            fh = open(part_path, "ab" if resume_offset else "wb")
        except OSError as exc:
            self._send(proto.MSG_FILE_SEND_ACCEPT,
                       proto.pack_file_send_accept(transfer_id, False, 0, f"cannot write file: {exc}"))
            return

        with self._lock:
            self._incoming[transfer_id] = {
                "fh": fh, "dest_path": dest_path, "part_path": part_path,
                "size": size, "received": resume_offset, "name": safe_name,
            }

        note = f", resuming from byte {resume_offset}" if resume_offset else ""
        print(f"[files] incoming '{safe_name}' ({size} bytes) from peer{note} - accepting automatically")
        self._send(proto.MSG_FILE_SEND_ACCEPT,
                   proto.pack_file_send_accept(transfer_id, True, resume_offset, ""))

    def _handle_chunk(self, payload: bytes) -> None:
        transfer_id, offset, data = proto.unpack_file_chunk(payload)
        with self._lock:
            state = self._incoming.get(transfer_id)
        if not state:
            return  # chunk for a transfer we don't know about (e.g. after a restart) - drop it
        state["fh"].write(data)
        state["received"] = offset + len(data)

    def _handle_complete(self, payload: bytes) -> None:
        info = proto.unpack_file_complete(payload)
        transfer_id = info["transfer_id"]
        with self._lock:
            state = self._incoming.pop(transfer_id, None)
        if not state:
            return
        state["fh"].close()
        if info.get("ok") and state["received"] >= state["size"]:
            os.replace(state["part_path"], state["dest_path"])
            print(f"[files] received '{state['name']}' complete -> {state['dest_path']}")
        else:
            print(f"[files] '{state['name']}' ended incomplete "
                  f"({state['received']}/{state['size']} bytes) - kept as {state['part_path']}. "
                  "Ask the peer to send it again to resume.")

    # --- asking the peer to push a file to us ("get") ----------------------
    def request_file(self, remote_path: str) -> None:
        self._send(proto.MSG_FILE_PULL_REQUEST, proto.pack_file_pull_request(remote_path))
        print(f"[files] asked peer to send '{remote_path}' - it will arrive automatically if they have it.")

    def _handle_pull_request(self, payload: bytes) -> None:
        req = proto.unpack_file_pull_request(payload)
        path = req.get("path", "")
        # Runs send_file in its own thread so the control-channel reader
        # loop isn't blocked waiting for the accept round-trip and the
        # transfer itself.
        threading.Thread(target=self.send_file, args=(path,), daemon=True).start()
