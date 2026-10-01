"""
Phase 3 - clipboard sync.

Watches the local clipboard for changes and sends them over the
control channel; applies incoming clipboard messages from the peer.
Runs as a background thread on both host and viewer - the control
channel is symmetric, so this class behaves identically on both ends.

Text sync uses `pyperclip` (needs a backend available on the OS - on
Linux that means xclip or xsel installed; Windows/macOS work out of
the box).

Image sync is best-effort and asymmetric, by necessity:
  - Reading an image off the local clipboard uses Pillow's
    `ImageGrab.grabclipboard()`, which only works on Windows and macOS.
  - There is no reliable cross-platform way to *write* an image onto
    the OS clipboard from Python. So an incoming image is saved to
    disk instead of silently failing to apply - the console prints
    where it went. This is called out in README.md as a known
    limitation for this phase, not a bug.
"""

import io
import os
import threading
import time

import protocol as proto


class ClipboardSync:
    def __init__(self, conn, send_lock: threading.Lock, poll_interval: float = 1.0, save_dir: str = "."):
        self.conn = conn
        self.send_lock = send_lock
        self.poll_interval = poll_interval
        self.save_dir = save_dir
        self._last_sent_text = None
        self._last_seen_text = None   # what the peer just sent us - don't echo it back
        self._last_sent_image = None
        self._stop = threading.Event()

    def _send(self, msg_type: int, payload: bytes) -> None:
        with self.send_lock:
            proto.send_message(self.conn, msg_type, payload)

    # --- outgoing: watch the local clipboard, send changes -------------
    def watch_loop(self) -> None:
        try:
            import pyperclip
        except ImportError:
            print("[clipboard] 'pyperclip' not installed - clipboard sync disabled (pip install pyperclip).")
            return

        while not self._stop.is_set():
            try:
                text = pyperclip.paste()
            except Exception:
                text = None

            if text and text != self._last_sent_text and text != self._last_seen_text:
                self._last_sent_text = text
                try:
                    self._send(proto.MSG_CLIPBOARD_TEXT, proto.pack_clipboard_text(text))
                    print("[clipboard] sent local clipboard text to peer")
                except (BrokenPipeError, ConnectionError, OSError):
                    return

            self._try_send_image()
            self._stop.wait(self.poll_interval)

    def _try_send_image(self) -> None:
        try:
            from PIL import ImageGrab, Image as PILImage
        except ImportError:
            return
        try:
            img = ImageGrab.grabclipboard()
        except Exception:
            return  # unsupported on this platform (e.g. most Linux setups)
        if not isinstance(img, PILImage.Image):
            return

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        if data == self._last_sent_image:
            return
        self._last_sent_image = data
        try:
            self._send(proto.MSG_CLIPBOARD_IMAGE, proto.pack_clipboard_image(data))
            print("[clipboard] sent local clipboard image to peer")
        except (BrokenPipeError, ConnectionError, OSError):
            pass

    # --- incoming: apply what the peer sends ---------------------------
    def handle_text(self, payload: bytes) -> None:
        text = proto.unpack_clipboard_text(payload)
        self._last_seen_text = text
        try:
            import pyperclip
            pyperclip.copy(text)
            print("[clipboard] applied peer's clipboard text")
        except ImportError:
            print("[clipboard] received peer clipboard text (pyperclip not installed, showing instead):")
            print(text)

    def handle_image(self, payload: bytes) -> None:
        data = proto.unpack_clipboard_image(payload)
        path = os.path.join(self.save_dir, f"clipboard_{int(time.time())}.png")
        with open(path, "wb") as f:
            f.write(data)
        print(f"[clipboard] received peer clipboard image, saved to {path}")
        print("[clipboard] (setting images on the OS clipboard isn't supported cross-platform yet - open the file manually)")

    def stop(self) -> None:
        self._stop.set()
