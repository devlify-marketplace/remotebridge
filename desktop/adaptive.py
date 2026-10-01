"""
Phase 4 - adaptive bitrate/frame rate.

The viewer measures what it actually received (StatsReporter, used on
the viewer side) and sends a MSG_STATS_REPORT over the control channel
roughly once a second. AdaptiveBitrateController (used on the host
side) compares that against the current target fps and nudges quality
and fps up or down, one step at a time.

Quality drops before fps on the way down, and fps rises before quality
on the way up: a slightly choppier but sharp image is usually more
useful than a smooth blurry one, and recovering the reverse way avoids
overshooting frame rate before the link has actually recovered.
"""

import threading
import time

QUALITY_MIN, QUALITY_MAX = 20, 90
FPS_MIN, FPS_MAX = 5, 30
QUALITY_STEP = 10
FPS_STEP = 2
GOOD_STREAK_TO_RAISE = 5   # consecutive good reports before trying to raise
KEEPING_UP_RATIO = 0.85    # measured_fps / target_fps at or above this = "keeping up"
REPORT_INTERVAL = 1.0      # seconds, viewer side


class AdaptiveBitrateController:
    """Thread-safe target quality/fps for the host's video_loop, adjusted
    by incoming viewer stats reports. video_loop calls get_settings()
    once per frame; control_loop calls record_report() as reports
    arrive on a different thread."""

    def __init__(self, start_quality: int, start_fps: int, enabled: bool = True):
        self._lock = threading.Lock()
        self.enabled = enabled
        self._quality = max(QUALITY_MIN, min(QUALITY_MAX, start_quality))
        self._fps = max(FPS_MIN, min(FPS_MAX, start_fps)) if start_fps > 0 else FPS_MAX
        self._good_streak = 0

    def get_settings(self):
        with self._lock:
            return self._quality, self._fps

    def record_report(self, measured_fps: float, measured_kbps: float) -> None:
        if not self.enabled:
            return
        with self._lock:
            target_fps = self._fps
            keeping_up = target_fps <= 0 or measured_fps >= target_fps * KEEPING_UP_RATIO
            if keeping_up:
                self._good_streak += 1
                if self._good_streak >= GOOD_STREAK_TO_RAISE:
                    self._good_streak = 0
                    if self._fps < FPS_MAX:
                        self._fps = min(FPS_MAX, self._fps + FPS_STEP)
                    elif self._quality < QUALITY_MAX:
                        self._quality = min(QUALITY_MAX, self._quality + QUALITY_STEP)
            else:
                self._good_streak = 0
                if self._quality > QUALITY_MIN:
                    self._quality = max(QUALITY_MIN, self._quality - QUALITY_STEP)
                elif self._fps > FPS_MIN:
                    self._fps = max(FPS_MIN, self._fps - FPS_STEP)


class StatsReporter:
    """Lives on the viewer. Call note_frame() as each video frame
    arrives, and maybe_report() after it (e.g. once per frame) - it
    only actually sends once REPORT_INTERVAL has elapsed, then resets
    its counters for the next window."""

    def __init__(self):
        self._count = 0
        self._bytes = 0
        self._window_start = time.monotonic()

    def note_frame(self, frame_bytes: int) -> None:
        self._count += 1
        self._bytes += frame_bytes

    def maybe_report(self, send_fn) -> None:
        now = time.monotonic()
        elapsed = now - self._window_start
        if elapsed < REPORT_INTERVAL:
            return
        measured_fps = self._count / elapsed
        measured_kbps = (self._bytes * 8 / 1000) / elapsed
        self._count = 0
        self._bytes = 0
        self._window_start = now
        try:
            send_fn(measured_fps, measured_kbps)
        except Exception:
            pass  # control channel may be mid-reconnect; skip this one report
