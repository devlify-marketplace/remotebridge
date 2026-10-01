"""
Phase 8 - Whiteboard state.

The host doesn't render the whiteboard itself; it just needs to remember
what's been drawn so far so a viewer who joins mid-session (or the
control viewer whose window was closed and reopened) can be brought up
to date with one snapshot instead of replaying every stroke ever sent.

Thread-safe: strokes arrive on whichever viewer's control-channel thread
sent them, and are read by whichever thread is handling a new viewer's
join.
"""

import threading
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class Stroke:
    stroke_id: str
    viewer_id: str
    color: str
    width: int
    points: List[List[float]] = field(default_factory=list)
    active: bool = True  # False once an "end" event has been received


class WhiteboardState:
    """Tracks all strokes drawn in the current session."""

    def __init__(self, max_strokes: int = 500):
        self._strokes: Dict[str, Stroke] = {}
        self._order: List[str] = []  # insertion order, oldest first
        self._lock = threading.Lock()
        self._max_strokes = max_strokes

    def start_stroke(self, stroke_id: str, viewer_id: str, color: str, width: int,
                      x: float, y: float) -> None:
        with self._lock:
            self._strokes[stroke_id] = Stroke(
                stroke_id=stroke_id, viewer_id=viewer_id, color=color, width=width,
                points=[[x, y]],
            )
            self._order.append(stroke_id)
            self._evict_if_needed()

    def add_point(self, stroke_id: str, x: float, y: float) -> None:
        with self._lock:
            stroke = self._strokes.get(stroke_id)
            if stroke is not None and stroke.active:
                stroke.points.append([x, y])

    def end_stroke(self, stroke_id: str) -> None:
        with self._lock:
            stroke = self._strokes.get(stroke_id)
            if stroke is not None:
                stroke.active = False

    def clear(self) -> None:
        with self._lock:
            self._strokes.clear()
            self._order.clear()

    def snapshot(self) -> List[dict]:
        """A plain-dict copy of every stroke, in draw order, for protocol_p8.pack_whiteboard_state."""
        with self._lock:
            return [
                {
                    "stroke_id": s.stroke_id,
                    "viewer_id": s.viewer_id,
                    "color": s.color,
                    "width": s.width,
                    "points": [list(p) for p in s.points],
                }
                for sid in self._order
                for s in [self._strokes[sid]]
            ]

    def _evict_if_needed(self) -> None:
        """Caller already holds the lock. Drop the oldest strokes once
        the board gets too large, so a long support session doesn't grow
        the join-snapshot without bound."""
        while len(self._order) > self._max_strokes:
            oldest = self._order.pop(0)
            self._strokes.pop(oldest, None)
