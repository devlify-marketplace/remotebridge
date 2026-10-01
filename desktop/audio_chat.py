"""
Phase 8 - Voice chat audio I/O.

Thin wrapper around `sounddevice` for capturing mic input and playing
back received audio. Kept deliberately simple (mono PCM16, no codec) —
that's plenty for a support/collaboration voice channel on a LAN or
decent connection, and it keeps this phase focused on the collaboration
plumbing rather than audio compression. A codec (Opus) is the obvious
follow-up if bandwidth becomes a problem.

Both capture and playback degrade gracefully if `sounddevice` isn't
installed or no audio device is available, the same way host_p7.py
degrades when `mss`/`pynput` are missing — the rest of the session
(video, input, chat, whiteboard, print) keeps working without voice.
"""

import queue
import threading

SAMPLE_RATE = 16000
CHANNELS = 1
BLOCK_SIZE = 1600  # 100ms at 16kHz — small enough to feel responsive


class AudioUnavailable(Exception):
    pass


def _import_sounddevice():
    try:
        import sounddevice as sd
        import numpy as np
        return sd, np
    except ImportError as e:
        raise AudioUnavailable(
            "Missing dependency 'sounddevice' (and numpy). "
            "Install with: pip install sounddevice numpy"
        ) from e
    except OSError as e:
        # sounddevice imports fine but its native PortAudio library isn't
        # installed on this machine (e.g. `apt install libportaudio2` on
        # Linux). Same graceful-degrade path as a missing Python package.
        raise AudioUnavailable(
            f"Audio library unavailable ({e}). On Linux try: "
            "sudo apt install libportaudio2"
        ) from e


class MicCapture:
    """Captures mic audio in the background; pull chunks with get_chunk()."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS,
                 block_size: int = BLOCK_SIZE):
        self.sd, self.np = _import_sounddevice()
        self.sample_rate = sample_rate
        self.channels = channels
        self.block_size = block_size
        self._queue: "queue.Queue[bytes]" = queue.Queue(maxsize=20)
        self._stream = None
        self.muted = False

    def _callback(self, indata, frames, time_info, status):
        if self.muted:
            return
        pcm = (indata * 32767).astype(self.np.int16).tobytes()
        try:
            self._queue.put_nowait(pcm)
        except queue.Full:
            pass  # Drop audio rather than build latency

    def start(self) -> None:
        self._stream = self.sd.InputStream(
            samplerate=self.sample_rate, channels=self.channels,
            blocksize=self.block_size, dtype="float32", callback=self._callback,
        )
        self._stream.start()

    def get_chunk(self, timeout: float = 1.0) -> bytes:
        """Blocks until the next mic chunk is ready. Raises queue.Empty on timeout."""
        return self._queue.get(timeout=timeout)

    def set_muted(self, muted: bool) -> None:
        self.muted = muted

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class AudioPlayback:
    """Plays back PCM16 chunks pushed via play_chunk(); mixes concurrent speakers."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS):
        self.sd, self.np = _import_sounddevice()
        self.sample_rate = sample_rate
        self.channels = channels
        self._queue: "queue.Queue[bytes]" = queue.Queue(maxsize=50)
        self._stream = None
        self._lock = threading.Lock()

    def _callback(self, outdata, frames, time_info, status):
        needed_bytes = frames * self.channels * 2  # int16 = 2 bytes/sample
        buf = bytearray()
        while len(buf) < needed_bytes:
            try:
                buf += self._queue.get_nowait()
            except queue.Empty:
                break
        if len(buf) < needed_bytes:
            buf += b"\x00" * (needed_bytes - len(buf))
        pcm = self.np.frombuffer(bytes(buf[:needed_bytes]), dtype=self.np.int16)
        outdata[:] = pcm.reshape(-1, self.channels).astype(self.np.float32) / 32767.0

    def start(self) -> None:
        self._stream = self.sd.OutputStream(
            samplerate=self.sample_rate, channels=self.channels,
            dtype="float32", callback=self._callback,
        )
        self._stream.start()

    def play_chunk(self, pcm_bytes: bytes) -> None:
        try:
            self._queue.put_nowait(pcm_bytes)
        except queue.Full:
            pass  # Drop rather than build latency

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
