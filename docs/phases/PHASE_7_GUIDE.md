# Phase 7: Multi-User Foundation

**Goal:** Make the host capable of serving more than one connected viewer at once — the structural prerequisite everything else in this stretch builds on.

**Deliverable:** A host can accept several simultaneous viewers, at least one of which is read-only (view-only mode).

---

## Overview

Phase 7 transforms the architecture from **one-viewer-at-a-time** to **many-viewers-concurrently**:

### Old Model (Phases 0-6)
```
Host
  ↓ (wait for connection)
  ├─ Video→ Viewer 1
  ├─ Input←─ Viewer 1
  └─ Control↔ Viewer 1
  ↓ (session ends, loop back)
  wait for next connection
```

### New Model (Phase 7+)
```
Host (multi-user session)
  ├─ Video→ Viewer A (control)
  │    ├─ Input←─ (Viewer A processes this)
  │    └─ Control↔ (Viewer A can query permissions, clipboard, files)
  │
  ├─ Video→ Viewer B (view-only)
  │    ├─ Input←─ (ignored)
  │    └─ Control↔ (can only query session info, no input allowed)
  │
  ├─ Video→ Viewer C (view-only)
  │    └─ (same as B)
  │
  ↓ (viewers join/leave during session, no interruption)
```

### Key Features

1. **Unlimited concurrent viewers**
   - Only one can have **control** (input authority)
   - Many can be **view-only** (read-only, video only)

2. **Session persistence**
   - Host no longer waits between sessions
   - Viewers can join/leave independently
   - Other viewers unaffected when one disconnects

3. **View-only mode**
   - Viewers explicitly join as view-only (no input attempt)
   - Or automatically downgrade if control already taken
   - Receive video; input/control-channel requests are rejected or ignored

4. **Session awareness**
   - All viewers get a list of current participants
   - Broadcasts when someone joins/leaves
   - Each viewer can query "am I allowed to send input?"

---

## Architecture

### Session Manager (`session_manager.py`)

Central coordinator for all connected viewers in a single session.

```python
class MultiUserSessionManager:
    viewers: Dict[str, ViewerSession]      # all connected viewers
    control_viewer: Optional[ViewerSession] # the one viewer with input authority
    
    def add_viewer(viewer) -> (bool, reason)  # add or reject
    def remove_viewer(viewer)                  # on disconnect
    def get_all_viewers() -> [ViewerSession]   # snapshot of active viewers
    def get_control_viewer() -> ViewerSession  # current input authority (or None)
    def can_send_input(viewer) -> bool         # is this viewer allowed to send input?
    def get_video_recipients() -> [ViewerSession]  # broadcast targets
    def get_input_sender() -> ViewerSession    # who can send input (or None)
```

**Thread-safe:** Uses locks to protect access from multiple viewer threads.

### Viewer Session (`session_manager.ViewerSession`)

Represents one connected viewer.

```python
@dataclass
class ViewerSession:
    viewer_id: str          # "alice", "bob", etc.
    address: str            # "192.168.1.5:54321"
    video_conn: socket      # TLS-wrapped (receives video frames)
    input_conn: socket      # TLS-wrapped (sends input events)
    control_conn: socket    # TLS-wrapped (control channel: clipboard, files, etc.)
    is_control: bool        # True = input authority, False = view-only
    connected_at: float     # timestamp
```

### Protocol Extensions (`protocol_p7.py`)

New message types for multi-user coordination:

| Msg Type | Purpose | Direction | Format |
|----------|---------|-----------|--------|
| `MSG_AUTH_REQUEST` (extended) | Include view_mode | Viewer→Host | JSON: `{viewer_id, view_mode, password, totp_code}` |
| `MSG_AUTH_RESPONSE` (extended) | Echo view_mode for confirmation | Host→Viewer | JSON: `{approved, view_mode, reason}` |
| `MSG_SESSION_INFO` (0x60) | List of current viewers | Host→Viewer | JSON: `{viewers: [{viewer_id, view_mode, address, duration}]}` |
| `MSG_VIEWER_JOINED` (0x61) | Someone joined | Host→All Viewers | JSON: `{viewer_id, view_mode, address}` |
| `MSG_VIEWER_LEFT` (0x62) | Someone left | Host→All Viewers | JSON: `{viewer_id}` |
| `MSG_PERMISSIONS_QUERY` (0x63) | "Can I send input?" | Viewer→Host | (empty) |
| `MSG_PERMISSIONS_RESPONSE` (0x64) | Yes/no answer | Host→Viewer | JSON: `{can_send_input, reason}` |

**Backward compatibility:**
- Old viewers (Phase 0-6) still work; they just don't know about view_mode (defaults to control)
- Host always responds with Phase 7 format so new features are available

---

## Host Implementation (`host_p7.py`)

### Connection Flow

1. **Accept connections** (no longer one-and-done)
   ```
   Video listener (port 5000)
     ↓ accept → Video socket
   Input listener (port 5001)
     ↓ accept → Input socket
   Control listener (port 5002)
     ↓ accept → Control socket
   ```
   Viewers must connect all three channels in order (same as Phase 0-6).

2. **Authenticate** (same as before, but now includes view_mode)
   ```
   Viewer sends: MSG_AUTH_REQUEST with viewer_id, password, view_mode
   Host checks password/2FA/whitelist
   Host responds: MSG_AUTH_RESPONSE with approved, view_mode, reason
   ```

3. **Add to session**
   ```
   If view_mode=CONTROL:
     - If no control viewer exists, assign control to this viewer
     - If control already taken, reject or auto-downgrade to view-only
   If view_mode=VIEW_ONLY:
     - Always accepted (unlimited view-only viewers)
   ```

4. **Broadcast session info**
   ```
   Send MSG_SESSION_INFO to new viewer (current participants)
   Send MSG_VIEWER_JOINED to all others (announce newcomer)
   ```

5. **Run session threads**
   - **Main thread:** `broadcast_video()` - capture and send frames to all viewers
   - **Per-viewer input thread:** Receive input from control viewer only
   - **Per-viewer control thread:** Handle clipboard, files, stats, permissions, etc.

### Key Functions

#### `broadcast_video(session, active_monitor, bitrate)`
- Captures screen once per frame interval
- Broadcasts to all video recipients (both control and view-only)
- Removes dead viewers on send failure
- Broadcasts viewer-left message

#### `handle_viewer_input(viewer, session)`
- Only runs for control viewers (view-only viewers skip input handling)
- Receives mouse/keyboard from the control viewer
- Applies input to the host OS
- Stops if viewer loses control (another viewer took it)

#### `handle_viewer_control(viewer, session, ...)`
- Handles all control-channel messages for one viewer:
  - Clipboard text/image
  - File list, send, pull, chunks
  - Stats reports (for adaptive bitrate)
  - Monitor list/switch
  - **Phase 7:** Permissions query
- Runs in its own thread per viewer (doesn't block other viewers)

#### `accept_viewer(raw_video, raw_input, raw_control, ...)`
- Wraps channels in TLS
- Performs authentication
- Creates ViewerSession and adds to session manager
- Spawns input/control threads
- Logs session start/end

---

## Viewer Implementation (`viewer_p7.py`)

### Command-Line Usage

```bash
# Join as control (tries to get input authority; auto-downgrades if taken)
python3 viewer_p7.py 192.168.1.100:5000 --id alice --password secret

# Join explicitly as view-only
python3 viewer_p7.py 192.168.1.100:5000 --id bob --view-only

# Multiple simultaneous viewers in separate processes
viewer_p7.py HOST:PORT --id alice &    # control
viewer_p7.py HOST:PORT --id bob --view-only &  # view-only
viewer_p7.py HOST:PORT --id charlie --view-only &  # view-only
```

### Session Awareness Features

1. **Display session info on connect**
   ```
   [session] Current viewers:
     - alice (control, 0.5s, 192.168.1.5:54321)
     - bob (view-only, 1.2s, 192.168.1.10:54322)
   ```

2. **Announce joins/leaves**
   ```
   [session] charlie (view-only) joined from 192.168.1.15:54323
   [session] bob left the session
   ```

3. **Query permissions after others leave**
   ```
   [session] alice left the session
   [viewer] Querying permissions...
   [session] You now have input control!
   ```

### Permission Handling

If a control viewer's connection drops or they're replaced:
- Input thread detects it
- Viewer can call `query_permissions()` to refresh
- May get permission to send input if they're the new control viewer

---

## Access Control & Security

### Permission Model

| Viewer Mode | Can Receive Video | Can Send Input | Can Push Clipboard | Can Pull Files |
|-------------|-------------------|----------------|--------------------|----------------|
| Control    | ✓ | ✓ | ✓ | ✓ |
| View-only  | ✓ | ✗ | ✗ | ✗ |

(Note: Phase 7 focuses on the multi-user structure. Fine-grained per-viewer file/clipboard permissions are Phase 9+ work.)

### Enforcing View-Only

1. **At auth time:**
   - Viewer declares view_mode in MSG_AUTH_REQUEST
   - Host honors it in MSG_AUTH_RESPONSE

2. **At message time:**
   - If viewer tries to send MSG_MOUSE_MOVE, MSG_KEY_EVENT, etc., host rejects with log entry
   - Clipboard/file operations from view-only viewers are ignored

3. **At permission query:**
   - Viewer can call MSG_PERMISSIONS_QUERY any time
   - Host responds with true/false + reason

---

## Replay & Logging

### Session Log

Extends existing `sessions.log`:

```
[timestamp] attempt; viewer_id=alice; address=192.168.1.5:54321; decision=console-prompt; approved=yes
[timestamp] start; viewer_id=alice
[timestamp] attempt; viewer_id=bob; address=192.168.1.10:54322; decision=password; approved=yes
[timestamp] start; viewer_id=bob
[timestamp] end; viewer_id=alice; duration_seconds=45
[timestamp] end; viewer_id=bob; duration_seconds=30
```

### Multiple Sessions

Once Phase 7 drops, each host session is **multi-user within a session**, but the host still supports `--single-session` to exit after the last viewer leaves (for testing).

Default behavior (without `--single-session`): after all viewers disconnect, host waits for new viewers (same session continues, but may have new participants).

---

## Migration from Phase 6

### Phase 6 Host → Phase 7 Host

**Old code assumption:**
```python
# Phase 4/6: one session = one viewer
run_one_session():
    wait_for_viewer()
    auth()
    run_video_loop()  # blocks until dropped
    cleanup()
```

**New code structure:**
```python
# Phase 7: one session = many viewers
MultiUserSessionManager session = new()
start_broadcast_video(session)  # background thread, never blocks
while True:
    wait_for_viewer()
    spawn_thread( auth_and_add_to_session() )
```

### Phase 6 Viewer → Phase 7 Viewer

**Old code assumption:**
```python
# Phase 4/6: viewer knows it's the only participant
connect()
auth()
handle_video()  # assumes full control, exclusive input
```

**New code structure:**
```python
# Phase 7: viewer is one of many
connect()
auth(view_mode=CONTROL or VIEW_ONLY)
handle_control_channel()  # monitor session info
handle_input_sender()     # may be limited if not control viewer
handle_video()
```

### Compatibility

Phase 7 is **backward compatible**:
- Old Phase 6 viewers still connect and work (they just won't get session info)
- Old Phase 6 hosts won't work with multiple viewers (still use run_one_session loop)

---

## Testing Phase 7

### Test Scenario 1: Two Simultaneous Control Attempts

```bash
# Terminal 1: Start host
python3 host_p7.py --video-port 5000 --input-port 5001 --control-port 5002

# Terminal 2: Connect as control
python3 viewer_p7.py localhost:5000 --id alice

# Terminal 3: Connect as control (should auto-downgrade to view-only)
python3 viewer_p7.py localhost:5000 --id bob
# Output: "Downgrading 'bob' to view-only mode"

# Verify in alice's terminal: sees "bob (view-only) joined"
# Verify in bob's terminal: see "view-only" mode in session info
```

### Test Scenario 2: Control Handoff on Disconnect

```bash
# Terminal 1: Start host
python3 host_p7.py --video-port 5000 --input-port 5001 --control-port 5002

# Terminal 2: alice (control)
python3 viewer_p7.py localhost:5000 --id alice
# Output: "Connected as control viewer 'alice'"

# Terminal 3: bob (view-only)
python3 viewer_p7.py localhost:5000 --id bob --view-only
# Output: "Connected as view-only viewer 'bob'"
# Output in alice's terminal: "bob (view-only) joined"
# Output in bob's terminal: "alice (control, 2.5s, localhost:XXX)"

# In Terminal 2: Ctrl+C to disconnect alice
# Output in bob's terminal: "alice left the session"
# Bob's input should now be enabled (if he queries permissions)
```

### Test Scenario 3: Three Viewers, Multiple View-Only

```bash
# Terminal 1: Start host
python3 host_p7.py

# Terminal 2: alice (control)
python3 viewer_p7.py localhost:5000 --id alice
# Output: "Connected as control viewer 'alice'"

# Terminal 3: bob (explicit view-only)
python3 viewer_p7.py localhost:5000 --id bob --view-only

# Terminal 4: charlie (tries control, gets auto-downgraded)
python3 viewer_p7.py localhost:5000 --id charlie
# Output: "Downgrading 'charlie' to view-only mode"

# Verify:
# - alice sees: "bob (view-only) joined", then "charlie (view-only) joined"
# - bob sees: "alice (control, 0s), charlie (view-only, 0s)"
# - charlie sees: "alice (control, 1s), bob (view-only, 1s)"
# - Only alice can move the mouse on the host
```

---

## Limitations & Deferred Work

### Phase 7 Scope

Phase 7 delivers the **structural foundation** for multi-user sessions but intentionally defers some features to later phases:

1. **Relay server multi-user support** (Phase 7+ item)
   - Relay currently assumes one-viewer-per-host
   - Multi-user relay routing deferred to Phase 9 (admin console)

2. **Per-viewer file/clipboard permissions** (Phase 9)
   - All viewers can currently access all files
   - Fine-grained ACLs come with the admin console

3. **Session recording with multiple viewers** (Phase 8)
   - Recording the multi-participant conversation is Phase 8 work
   - Single-viewer recording (Phase 4) still works

4. **Mobile support** (Phase 5/6 extend to Phase 7)
   - Mobile host/controller apps need updates to support multi-user
   - Deferred; Phase 7 focuses on desktop

### Known Workarounds

1. **Only one control viewer at a time**
   - This is intentional (prevents conflicting inputs)
   - Later phases might support something like "request control" or "take turns"

2. **View-only viewers can't pull files or sync clipboard**
   - This is intentional for Phase 7
   - Per-viewer capabilities come in Phase 9

3. **No built-in throttling per-viewer**
   - Host sends same video quality/fps to all viewers
   - Adaptive bitrate (Phase 4) takes the most conservative feedback
   - Per-viewer bitrate adaptation deferred to Phase 10+

---

## Next Phases

### Phase 8: Collaboration & Support Tools

Now that multi-user is working, add tools for groups:
- In-session voice and text chat (rely on multi-user foundation)
- Whiteboard/annotation overlay (one shared overlay, all viewers see it)
- Remote printing

### Phase 9: Admin Console & Policy

Once collaboration tools exist, add visibility/control:
- Central admin console to see all sessions
- Group policies for enforced settings
- Usage reporting dashboard
- Per-viewer file/clipboard ACLs

### Phase 10: Deployment & Branding

Once everything is stable at small scale:
- Scripted/MSI deployment for IT rollout
- Auto-update mechanism
- White-label options

---

## Implementation Notes

### Thread Safety

- `MultiUserSessionManager` uses locks for `viewers` dict and `control_viewer` reference
- Each viewer's connection handling runs in its own thread (no cross-viewer data races)
- Shared resources (clipboard, file session, adaptive bitrate) are thread-safe via their own locks

### Performance Considerations

1. **Video broadcast:** Copying the frame to each viewer's socket is O(viewers)
   - With 10 viewers, 15 FPS = 150 frame copies/sec
   - Acceptable for LAN; may want to optimize (frame pool, zero-copy) for scale

2. **Input serialization:** Only one viewer sends input (no conflicts)
   - Simplifies host state machine vs. if multiple viewers sent input

3. **Per-viewer threads:** Many threads (at least 2 per viewer)
   - 10 viewers = 20+ threads
   - Python's GIL limits true parallelism but threads unblock on socket I/O

### Backward Compatibility

Phase 7 maintains protocol compatibility with Phase 0-6:
- Old auth format (no view_mode) still accepted (defaults to control)
- New session info messages are sent but old viewers ignore them
- Host responds to old and new message types identically where possible

---

## Summary

Phase 7 transforms the host from a single-viewer service into a **multi-user hub**:

| Aspect | Phase 0-6 | Phase 7 |
|--------|-----------|---------|
| **Concurrent viewers** | 1 | Many |
| **Control viewers** | 1 (implied) | 1 (explicit) |
| **View-only viewers** | 0 | Unlimited |
| **Session persistence** | Ends after viewer disconnect | Continues; viewers join/leave |
| **Broadcaster** | Single-threaded per session | Background thread, always running |
| **Per-viewer handling** | Blocking (video_loop on main thread) | Non-blocking (spawned threads) |
| **Session awareness** | None | Viewers know who else is present |

This foundation enables collaboration (Phase 8), policy enforcement (Phase 9), and eventually admin consoles and enterprise features (Phase 10+).
