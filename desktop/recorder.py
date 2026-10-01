"""
Phase 4 - session recording to a video file.

Runs on the viewer, where frames already arrive decoded as BGR numpy
arrays for the OpenCV display window (see viewer.py's
video_display_loop) - recording just means also writing each displayed
frame to a cv2.VideoWriter. Started/stopped with the console's
`record <path>` / `record stop` commands (see console.py).
"""

import threading

import cv2

DEFAULT_FOURCC = "mp4v"
DEFAULT_RECORD_FPS = 15.0


class SessionRecorder:
    def __init__(self):
        self._lock = threading.Lock()
        self._writer = None
        self._path = None
        self._frame_size = None

    @property
    def active(self) -> bool:
        with self._lock:
            return self._writer is not None

    def start(self, path: str, frame_size: tuple, fps: float = DEFAULT_RECORD_FPS) -> str:
        """Returns "" on success, or an error message."""
        with self._lock:
            if self._writer is not None:
                return f"already recording to {self._path}"
            fourcc = cv2.VideoWriter_fourcc(*DEFAULT_FOURCC)
            writer = cv2.VideoWriter(path, fourcc, fps, frame_size)
            if not writer.isOpened():
                return f"could not open '{path}' for writing (check the extension, e.g. .mp4)"
            self._writer = writer
            self._path = path
            self._frame_size = frame_size
            return ""

    def write_frame(self, frame) -> None:
        with self._lock:
            if self._writer is None:
                return
            h, w = frame.shape[:2]
            if (w, h) != self._frame_size:
                frame = cv2.resize(frame, self._frame_size)
            self._writer.write(frame)

    def stop(self):
        """Returns the path that was being recorded to, or None if
        nothing was recording."""
        with self._lock:
            path = self._path
            if self._writer is not None:
                self._writer.release()
            self._writer = None
            self._path = None
            self._frame_size = None
            return path
