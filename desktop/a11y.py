"""
Phase 12 - accessibility helpers for the desktop viewer/host.

What this covers, and what it cannot:

  * The remote screen is pixels. Like every remote-desktop tool, this app has
    no access to the remote machine's accessibility tree, so a screen reader
    cannot read *the remote desktop's contents* through the video window.
    (Use the remote machine's own screen reader, or the OS's, and the audio
    channel if voice is on.) Saying otherwise would be wrong.
  * Everything the APP itself says - who joined, policy denials, chat, print
    results, connection state, errors - is plain text lines on the terminal,
    which screen readers read. announce() gives those a stable "[status]"
    shape; with --speak it also speaks them through the OS speech engine for
    people who aren't watching the terminal (they're watching, or operating,
    the remote window).
  * High contrast applies to what the app DRAWS over the video: the
    whiteboard strokes and the mode banner.
  * The whiteboard is mouse-driven by nature (you draw with a pointer).
    Keyboard-only users can do everything else: chat, print, feedback, mic
    mute (m), quit (ESC).
"""

import queue
import shutil
import subprocess
import sys
import threading

# Colors chosen to stay distinguishable from each other and from most screen
# content when drawn with a black outline: (name, BGR).
HIGH_CONTRAST_PALETTE = [("yellow", (0, 255, 255)), ("white", (255, 255, 255)),
                         ("cyan", (255, 255, 0)), ("magenta", (255, 0, 255))]
HIGH_CONTRAST_MIN_STROKE = 5


def high_contrast_color(stroke_color_hex: str) -> tuple:
    """Maps any stroke color to one of the palette colors, stably (same input,
    same output, so one person's stroke keeps one color)."""
    return HIGH_CONTRAST_PALETTE[sum(stroke_color_hex.encode()) % len(HIGH_CONTRAST_PALETTE)][1]


def find_speech_command():
    """Returns an argv prefix that speaks its next argument, or None.
    Linux: spd-say / espeak-ng / espeak. macOS: say. Windows: PowerShell SAPI."""
    if sys.platform == "darwin" and shutil.which("say"):
        return ["say"]
    if sys.platform.startswith("win"):
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if ps:
            return [ps, "-NoProfile", "-Command",
                    "Add-Type -AssemblyName System.Speech; "
                    "(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak($args[0])"]
    for cmd in ("spd-say", "espeak-ng", "espeak"):
        if shutil.which(cmd):
            return [cmd]
    return None


class Announcer:
    """announce(text) prints "[status] text" (always) and, if speak=True and an
    engine exists, speaks it on a background thread (never blocks the video
    loop; bounded queue, so a burst of events drops the oldest spoken ones
    rather than talking for a minute after the moment has passed)."""

    def __init__(self, speak: bool = False, command=None, printer=print):
        self._print = printer
        self._cmd = command if command is not None else (find_speech_command() if speak else None)
        self.speaking = bool(speak and self._cmd)
        self.speak_requested_but_unavailable = bool(speak and not self._cmd)
        self._q = queue.Queue(maxsize=8)
        if self.speaking:
            threading.Thread(target=self._worker, daemon=True, name="speech").start()

    def announce(self, text: str) -> None:
        self._print(f"[status] {text}")
        if self.speaking:
            try:
                self._q.put_nowait(text)
            except queue.Full:
                try:
                    self._q.get_nowait()
                    self._q.put_nowait(text)
                except (queue.Empty, queue.Full):
                    pass

    def _worker(self) -> None:
        while True:
            text = self._q.get()
            try:
                subprocess.run(self._cmd + [text], timeout=30,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                pass
