"""
Phase 2 - session logging.

Appends one JSON object per line to a log file (sessions.log by
default) so a host operator can answer "who connected, when, and for
how long" after the fact. Newline-delimited JSON rather than a real
database, matching the file-based, no-server-required style of the
rest of Phase 0-2.

Event shapes:
    {"event": "attempt", "timestamp": ..., "viewer_id": ..., "address": ...,
     "decision": ..., "reason": ...}
    {"event": "start", "timestamp": ..., "viewer_id": ..., "address": ...}
    {"event": "end", "timestamp": ..., "viewer_id": ..., "address": ...,
     "duration_seconds": ...}
"""

import json
import time
from datetime import datetime, timezone

LOG_PATH = "sessions.log"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(event: str, path: str = LOG_PATH, **fields) -> None:
    entry = {"event": event, "timestamp": _now_iso(), **fields}
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


class SessionTimer:
    """Tracks a session's wall-clock duration from construction to elapsed()."""

    def __init__(self):
        self._start = time.monotonic()

    def elapsed(self) -> float:
        return round(time.monotonic() - self._start, 1)
