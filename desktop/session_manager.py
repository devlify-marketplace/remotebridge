"""
Phase 7 - Multi-user session manager.
Phase 8 - Adds the optional per-viewer audio channel and mute state used
by voice chat (audio_conn/mic_muted below); everything else is unchanged
from Phase 7.

Replaces the single-viewer-per-session model with concurrent multi-user
support. Tracks connected viewers (control vs. view-only), routes video
frames/input to appropriate recipients, and enforces access control.

A view-only viewer receives video frames but cannot send input; they're
marked as such during authentication and filtered out of input_loop
broadcast but included in video_loop broadcast.
"""

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


@dataclass
class ViewerSession:
    """Represents one connected viewer in a multi-user session."""
    viewer_id: str
    address: str
    video_conn: object  # socket (TLS-wrapped)
    input_conn: object  # socket (TLS-wrapped) or None if view-only
    control_conn: object  # socket (TLS-wrapped)
    is_control: bool  # True = input control, False = view-only
    connected_at: float = field(default_factory=time.time)
    audio_conn: object = None  # socket (TLS-wrapped), or None if voice isn't in use
    mic_muted: bool = False  # Phase 8: viewer-reported mute state (UI hint only)
    
    def __hash__(self):
        return id(self)
    
    def __eq__(self, other):
        return id(self) == id(other)


class MultiUserSessionManager:
    """
    Manages concurrent viewer connections to a single host.
    
    Key responsibilities:
    - Track active viewers (control vs. view-only)
    - Coordinate video frame distribution to all viewers
    - Route input from any control viewer to the host
    - Route control messages (clipboard, files, monitors) to appropriate viewers
    - Enforce per-viewer access control (one control viewer, many view-only)
    - Handle disconnections and cleanup
    """
    
    def __init__(self):
        self.viewers: Dict[str, ViewerSession] = {}  # keyed by (address, viewer_id) unique tuple
        self.viewers_lock = threading.Lock()
        
        self.control_viewer: Optional[ViewerSession] = None  # the one viewer with input control
        self.control_lock = threading.Lock()
        
        self.active = True
        self.session_start_time = time.time()
    
    def add_viewer(self, viewer: ViewerSession) -> Tuple[bool, str]:
        """
        Adds a viewer to the session. Returns (success, reason).
        
        If is_control=True:
          - If no control viewer exists, this viewer becomes the control viewer.
          - If a control viewer already exists, reject (only one control allowed).
        If is_control=False:
          - Always allowed (unlimited view-only viewers).
        """
        with self.control_lock:
            if viewer.is_control:
                if self.control_viewer is not None:
                    return False, f"Control already taken by {self.control_viewer.viewer_id}"
                self.control_viewer = viewer
            
            # Add to all-viewers list
            viewer_key = f"{viewer.address}_{viewer.viewer_id}"
            with self.viewers_lock:
                if viewer_key in self.viewers:
                    return False, "Viewer already in session"
                self.viewers[viewer_key] = viewer
            
            if viewer.is_control:
                print(f"[session] Control viewer '{viewer.viewer_id}' from {viewer.address} joined")
            else:
                print(f"[session] View-only viewer '{viewer.viewer_id}' from {viewer.address} joined")
            
            return True, "OK"
    
    def remove_viewer(self, viewer: ViewerSession) -> None:
        """Removes a viewer from the session (e.g., on disconnect)."""
        viewer_key = f"{viewer.address}_{viewer.viewer_id}"
        
        with self.control_lock:
            if self.control_viewer == viewer:
                self.control_viewer = None
                print(f"[session] Control viewer '{viewer.viewer_id}' disconnected")
            
            with self.viewers_lock:
                if viewer_key in self.viewers:
                    del self.viewers[viewer_key]
                    if not viewer.is_control:
                        print(f"[session] View-only viewer '{viewer.viewer_id}' disconnected")
    
    def get_all_viewers(self) -> List[ViewerSession]:
        """Returns a snapshot of all connected viewers."""
        with self.viewers_lock:
            return list(self.viewers.values())
    
    def get_control_viewer(self) -> Optional[ViewerSession]:
        """Returns the current control viewer, or None."""
        with self.control_lock:
            return self.control_viewer
    
    def get_viewer_by_id(self, viewer_id: str) -> Optional[ViewerSession]:
        """Finds a viewer by viewer_id (for broadcast routing)."""
        with self.viewers_lock:
            for v in self.viewers.values():
                if v.viewer_id == viewer_id:
                    return v
        return None
    
    def list_viewers(self) -> List[Dict]:
        """Returns a list of viewer info for broadcast or display."""
        viewers = self.get_all_viewers()
        result = []
        control = self.get_control_viewer()
        
        for v in viewers:
            result.append({
                "viewer_id": v.viewer_id,
                "address": v.address,
                "mode": "control" if v.is_control else "view-only",
                "connected_duration": time.time() - v.connected_at,
                "is_active": (control == v) if v.is_control else True,
            })
        return result
    
    def is_control_viewer(self, viewer: ViewerSession) -> bool:
        """Check if a viewer is the current control viewer."""
        with self.control_lock:
            return self.control_viewer == viewer
    
    def can_send_input(self, viewer: ViewerSession) -> bool:
        """Returns True if this viewer is allowed to send input."""
        return viewer.is_control and self.is_control_viewer(viewer)
    
    def get_video_recipients(self) -> List[ViewerSession]:
        """All viewers receive video (both control and view-only)."""
        return self.get_all_viewers()
    
    def get_input_sender(self) -> Optional[ViewerSession]:
        """The control viewer, or None."""
        return self.get_control_viewer()
    
    def get_audio_recipients(self, exclude: Optional["ViewerSession"] = None) -> List["ViewerSession"]:
        """Phase 8: every viewer with an active audio channel, except `exclude`
        (typically the frame's own sender — no point echoing audio back)."""
        return [v for v in self.get_all_viewers()
                if v.audio_conn is not None and v is not exclude]
    
    def shutdown(self) -> None:
        """Marks the session as inactive (stops accepting new viewers)."""
        self.active = False
    
    def is_active(self) -> bool:
        """Whether this session is still accepting new viewers."""
        return self.active
    
    def session_duration(self) -> float:
        """Elapsed time since session start."""
        return time.time() - self.session_start_time
