# Phase 7: Multi-User Foundation — Implementation

This directory contains the complete Phase 7 implementation for multi-user remote desktop sessions.

## Files

### Core Implementation

| File | Purpose |
|------|---------|
| **session_manager.py** | Multi-user session coordinator; tracks viewers, enforces access control |
| **protocol_p7.py** | Extended protocol for Phase 7 (view mode, session info, permissions) |
| **host_p7.py** | Phase 7 host: accepts multiple concurrent viewers, broadcasts video, routes input |
| **viewer_p7.py** | Phase 7 viewer: supports view-only mode, displays session info, queries permissions |

### Documentation

| File | Purpose |
|------|---------|
| **PHASE_7_GUIDE.md** | Comprehensive Phase 7 architecture, design decisions, testing scenarios |
| **PHASE_7_README.md** | This file: quick start and file overview |

---

## Quick Start

### Prerequisites

Same as Phase 4/6, plus no new dependencies:
- Python 3.7+
- `mss` (screen capture)
- `pynput` (input control)
- `Pillow` (image encode/decode)
- `cv2` (video display, viewer only)
- OpenSSL certificate (run `./generate_cert.sh` from Phase 4)

### Running Phase 7

#### Host: Accept Multiple Viewers

```bash
python3 host_p7.py \
  --video-port 5000 \
  --input-port 5001 \
  --control-port 5002 \
  --quality 60 \
  --fps 15
```

Output:
```
[host] Your LAN IP is likely: 192.168.1.100
[host] [video] listening on port 5000 for multi-user connections...
[host] [input] listening on port 5001 for multi-user connections...
[host] [control] listening on port 5002 for multi-user connections...
```

#### Viewer A: Connect as Control (Input Authority)

```bash
python3 viewer_p7.py 192.168.1.100:5000 --id alice --password mypassword
```

Output:
```
[viewer] TLS handshake complete on video channel
[viewer] TLS handshake complete on input channel
[viewer] TLS handshake complete on control channel
[viewer] Connection approved. Mode: control
[viewer] Connected as control viewer 'alice'

[session] Current viewers:
  - alice (control, 0.1s, 192.168.1.100:54321)
```

#### Viewer B: Connect as View-Only

```bash
python3 viewer_p7.py 192.168.1.100:5000 --id bob --view-only
```

Output:
```
[viewer] Connection approved. Mode: view-only
[viewer] Connected as view-only viewer 'bob'

[session] Current viewers:
  - alice (control, 2.3s, 192.168.1.100:54321)
  - bob (view-only, 0.1s, 192.168.1.100:54322)
```

In alice's terminal, you'll see:
```
[session] bob (view-only) joined from 192.168.1.100:54322
```

#### Viewer C: Try to Connect as Control (Auto-Downgrades)

```bash
python3 viewer_p7.py 192.168.1.100:5000 --id charlie
```

Output:
```
[viewer] Connection approved. Mode: view-only
[viewer] Connected as view-only viewer 'charlie'
```

Host terminal shows:
```
[host] Control already taken by alice; downgrading 'charlie' to view-only mode
[host] [session] Control viewer 'alice' joined (or view-only 'charlie' joined)
```

---

## Key Phase 7 Features

### 1. Multi-User Sessions

- **Control viewer:** One viewer with input authority
- **View-only viewers:** Unlimited viewers with video-only access
- **Concurrent threads:** Each viewer's input/control handled in separate threads (no blocking)

### 2. View-Only Mode

Join without input rights:
```bash
python3 viewer_p7.py HOST:PORT --view-only
```

Or auto-downgrade if control is already taken:
```bash
python3 viewer_p7.py HOST:PORT  # tries control, downgrades if needed
```

### 3. Session Awareness

All viewers receive:
- **Initial list:** Who's already in the session when they join
- **Join/leave events:** Real-time notifications as viewers connect/disconnect
- **Permission queries:** Can ask "am I allowed to send input?"

### 4. Protocol Extensions (Phase 7)

New message types:
- `MSG_SESSION_INFO` (0x60): List of current viewers
- `MSG_VIEWER_JOINED` (0x61): Someone joined
- `MSG_VIEWER_LEFT` (0x62): Someone left
- `MSG_PERMISSIONS_QUERY` (0x63): Query input permission
- `MSG_PERMISSIONS_RESPONSE` (0x64): Permission answer

Extended auth:
- `view_mode` field in MSG_AUTH_REQUEST/RESPONSE (0=control, 1=view-only)

### 5. Backward Compatibility

Old Phase 4/6 viewers and hosts still work:
- Old viewers don't receive session info (but still connect)
- Old hosts don't support multi-user (but Phase 7 viewers don't break them)
- Protocol is forward/backward compatible

---

## Architecture Overview

### Session Manager

```python
# Central coordinator
session = MultiUserSessionManager()

# Add viewer (control or view-only)
success, reason = session.add_viewer(viewer)

# Check permissions
if session.can_send_input(viewer):
    # Process input from this viewer
    ...

# Broadcast video to all recipients
for viewer in session.get_video_recipients():
    proto.send_message(viewer.video_conn, proto.MSG_VIDEO_FRAME, frame)

# Remove on disconnect
session.remove_viewer(viewer)
```

### Host Threads

- **Main:** Accepts incoming viewer connections
- **Video broadcast:** Captures and sends frames to all viewers (background)
- **Per-viewer input:** Receives input from control viewer only
- **Per-viewer control:** Handles clipboard, files, stats, permissions

### Viewer Session Object

```python
@dataclass
class ViewerSession:
    viewer_id: str          # "alice"
    address: str            # "192.168.1.5:54321"
    video_conn: socket      # receives video frames
    input_conn: socket      # sends input (or None if view-only)
    control_conn: socket    # control channel (clipboard, files, etc.)
    is_control: bool        # True = can send input, False = view-only
    connected_at: float     # timestamp
```

---

## Configuration & Customization

### Host Options

```bash
python3 host_p7.py \
  --video-port 5000           # Port for video frames (default 5000)
  --input-port 5001           # Port for input events (default 5001)
  --control-port 5002         # Port for control channel (default 5002)
  --quality 60                # JPEG quality 1-95 (default 60)
  --fps 15                    # Frames per second (default 15)
  --no-adaptive               # Disable adaptive bitrate (fixed quality/fps)
  --config host_config.json   # Auth config (passwords, 2FA, whitelist)
  --session-log sessions.log  # Where to log sessions (default "sessions.log")
  --download-dir .            # Where to save pulled files (default current dir)
```

### Viewer Options

```bash
python3 viewer_p7.py \
  192.168.1.100:5000          # host:video_port (required)
  --input-port 5001           # Port for input channel (default 5001)
  --control-port 5002         # Port for control channel (default 5002)
  --id alice                  # Viewer ID for logging (default "viewer-1")
  --password secret           # Password for unattended access
  --view-only                 # Join as view-only (no input)
```

---

## Testing Scenarios

### Scenario 1: Multiple View-Only Viewers

```bash
# Terminal 1: Host
python3 host_p7.py

# Terminal 2: Control viewer
python3 viewer_p7.py localhost:5000 --id alice

# Terminal 3, 4, 5, ... : View-only viewers
python3 viewer_p7.py localhost:5000 --id bob --view-only
python3 viewer_p7.py localhost:5000 --id charlie --view-only
python3 viewer_p7.py localhost:5000 --id diana --view-only
```

All viewers see alice's screen; only alice can control the mouse/keyboard.

### Scenario 2: Control Handoff

```bash
# Terminal 1: Host
python3 host_p7.py

# Terminal 2: alice (control)
python3 viewer_p7.py localhost:5000 --id alice

# Terminal 3: bob (view-only)
python3 viewer_p7.py localhost:5000 --id bob --view-only

# In Terminal 2: Press Ctrl+C to disconnect alice

# In Terminal 3: bob now receives session info showing he's alone
# If bob wants control, he can query permissions (automatically done on disconnect)
```

### Scenario 3: Automatic Downgrade

```bash
# Terminal 1: Host
python3 host_p7.py

# Terminal 2: alice (control)
python3 viewer_p7.py localhost:5000 --id alice

# Terminal 3: bob (tries control, gets downgraded)
python3 viewer_p7.py localhost:5000 --id bob
# Output: "Downgrading 'bob' to view-only mode"
```

Host output shows: `[host] Control already taken by alice; downgrading 'bob' to view-only mode`

---

## Session Log

Extended from Phase 4 to show multi-user events:

```
[2024-01-15 10:30:45.123] attempt; viewer_id=alice; address=192.168.1.5:54321; decision=console-prompt; approved=yes
[2024-01-15 10:30:46.234] start; viewer_id=alice; address=192.168.1.5:54321
[2024-01-15 10:30:52.345] attempt; viewer_id=bob; address=192.168.1.10:54322; decision=password; approved=yes
[2024-01-15 10:30:53.456] start; viewer_id=bob; address=192.168.1.10:54322
[2024-01-15 10:31:30.567] end; viewer_id=alice; address=192.168.1.5:54321; duration_seconds=44
[2024-01-15 10:31:35.678] end; viewer_id=bob; address=192.168.1.10:54322; duration_seconds=42
```

---

## Limitations & Deferred Work

### Phase 7 Scope (Intentionally Deferred)

- **Relay server multi-user support:** Relay still assumes 1 host per viewer; routing deferred to Phase 9
- **Per-viewer file/clipboard permissions:** All viewers can access all files; fine-grained ACLs come in Phase 9
- **Session recording with multiple viewers:** Multi-participant recording is Phase 8 work
- **Mobile support:** Phase 5/6 apps need updates to work with Phase 7 multi-user; deferred
- **Fine-grained access control:** View-only is binary; partial permissions (e.g., "control but no files") deferred

### Known Issues

1. **Input still serialized:**
   - Only one viewer can send input at a time (intentional, prevents conflicts)
   - Not a limitation for the "support" use case (1 support, N watchers)

2. **Bitrate adapts conservatively:**
   - If viewer A is on slow link, quality drops for all viewers
   - Per-viewer bitrate adaptation deferred to Phase 10+

3. **No viewer presence indicator in UI:**
   - Terminal shows session info; a real GUI would show faces/names alongside video
   - Deferred to GUI overhaul (Phase 10+)

---

## Protocol Changes Summary

### Backward Compatibility

Old Phase 4/6 viewers:
- Still work; just don't receive session info
- Auto-downgrade to view-only if control already taken (with log message)

Old Phase 4/6 hosts:
- Don't support multi-user (still use `run_one_session()` loop)
- Phase 7 viewers can still connect (just one at a time)

### New Message Types (0x60-0x64)

All JSON on control channel:
- `MSG_SESSION_INFO`: `{viewers: [{viewer_id, view_mode, address, duration}]}`
- `MSG_VIEWER_JOINED`: `{viewer_id, view_mode, address}`
- `MSG_VIEWER_LEFT`: `{viewer_id}`
- `MSG_PERMISSIONS_QUERY`: (empty)
- `MSG_PERMISSIONS_RESPONSE`: `{can_send_input, reason}`

### Extended Auth Format

Old (Phase 4):
```json
{"viewer_id": "alice", "password": "secret", "totp_code": ""}
```

New (Phase 7):
```json
{"viewer_id": "alice", "view_mode": 0, "password": "secret", "totp_code": ""}
```

`view_mode`: 0 = control, 1 = view-only

---

## Performance Notes

### Scalability

- **Per-viewer video:** O(viewers) socket sends per frame
  - 10 viewers × 15 FPS × 100KB/frame = 15 Mbps (1 capture + distribution)
  - Acceptable for LAN; may optimize with frame pooling for scale

- **Input:** O(1) since only one viewer sends
  - No conflict detection needed; serial input is correct

- **Control channel:** O(viewers) per broadcast (session info, join/leave)
  - Small messages; acceptable load

### Thread Overhead

- Main thread: Accept connections
- Video broadcast thread: Capture + send
- Per-viewer: 2 threads (input, control)
- 10 viewers = ~12 threads total (Python GIL limits true parallelism but I/O blocking is fine)

---

## Integration with Phase 6

Phase 7 **extends** Phase 6 (mobile host support) but does not replace it:

- Phase 6 mobile host: Can receive input from one remote viewer
- Phase 7 host: Can receive input from multiple viewers (but only one with control)
- Mobile + Phase 7: Same multi-user support as desktop (on roadmap for Phase 8+)

---

## Next Steps (Phase 8+)

Phase 8 builds on Phase 7 to add collaboration tools:
- **In-session voice/text chat:** Relies on multi-user foundation
- **Whiteboard/annotations:** One overlay, all viewers see it
- **Remote printing:** Print from host to any printer viewer specifies

Phase 9 adds admin/policy:
- **Admin console:** See all active sessions
- **Group policies:** Enforced settings
- **Usage reporting:** Who connected, when, for how long

---

## File Structure

```
phase-7-implementation/
├── session_manager.py       # Multi-user session coordinator
├── protocol_p7.py            # Extended protocol (view mode, session info)
├── host_p7.py                # Multi-user host application
├── viewer_p7.py              # Multi-user aware viewer
├── PHASE_7_GUIDE.md          # Comprehensive design doc
├── PHASE_7_README.md         # This file
└── tests/                    # (Optional) Test scripts
    ├── test_multi_viewer.sh  # Start 3+ viewers simultaneously
    ├── test_downgrade.sh     # Test auto-downgrade to view-only
    └── test_permissions.sh   # Test permission query on disconnect
```

---

## Troubleshooting

### "Control already taken by alice"

This is normal. A viewer tried to connect as control but another viewer already has it. The connecting viewer is automatically downgraded to view-only.

**Fix:** If you want them to have control, disconnect the current control viewer first.

### Input not working for view-only viewer

This is correct. View-only viewers cannot send input by design. To test input, join as control (without `--view-only` flag).

### Old viewer connecting to Phase 7 host

Old Phase 4/6 viewers:
- Still work but don't see session info (no MSG_SESSION_INFO support)
- Auto-downgrade to view-only if control taken

**Fix:** Use Phase 7 viewer to see full session awareness.

### Sessions don't persist after all viewers disconnect

This is intentional for Phase 7. After all viewers leave, the host waits for the next viewer (same session object but reset).

To track history across sessions, check `sessions.log` for who connected when.

---

## Summary

Phase 7 delivers the **multi-user foundation**:

✓ Multiple concurrent viewers  
✓ Control vs. view-only mode  
✓ Session info broadcast  
✓ Permission queries  
✓ Backward compatible with Phase 4-6  
✓ Ready for Phase 8 collaboration tools  

See `PHASE_7_GUIDE.md` for detailed architecture and design decisions.
