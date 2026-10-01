# Phase 8: Collaboration & Support Tools — Implementation

Builds directly on Phase 7 (multi-user sessions). Nothing about Phase 7's
video/input/clipboard/file/monitor handling changes — this phase adds a
new message family for each of the roadmap's four items and wires it
into the same control-loop pattern Phase 7 established.

## Files

| File | Purpose |
|------|---------|
| **protocol_p8.py** | New message types: chat, whiteboard strokes/clear/state, print request/response, voice frame/state |
| **whiteboard.py** | Host-side whiteboard state (so late-joining viewers get a snapshot instead of full replay) |
| **print_service.py** | Validates and executes a print job on the host (`lp`/`lpr` on Linux/macOS, default-printer shell-out on Windows), with path-traversal protection |
| **audio_chat.py** | Mic capture / speaker playback via `sounddevice`, degrades gracefully if unavailable |
| **session_manager.py** | Phase 7's manager plus an `audio_conn` field and `get_audio_recipients()` |
| **host_p8.py** | Host: relays chat/whiteboard/voice, executes print requests, sends whiteboard snapshot to new joiners |
| **viewer_p8.py** | Viewer: chat prompt, whiteboard drawing overlay on the video window, `/print` command, voice send/receive |

## What each roadmap item became

- **In-session text chat** — rides the existing control channel. Type at
  the `>` prompt in the viewer's terminal; the host relays it to every
  other connected viewer (control and view-only alike).
- **In-session voice chat** — a new dedicated audio channel (`--audio-port`,
  default 5003), symmetric with the video/input/control ports from
  earlier phases. Mono PCM16, no compression yet — that's the natural
  next step if bandwidth becomes an issue. The host relays each viewer's
  frames to everyone else; it doesn't mix or decode audio itself.
  Press `m` in the video window to mute/unmute.
- **Whiteboard/annotation overlay** — hold **Space** over the video window
  and drag to draw; strokes are sent point-by-point over the control
  channel and rendered on every viewer's copy of the video, in normalized
  coordinates so they land in the right place regardless of window size.
  `c` clears the board for everyone. The host keeps a running snapshot
  (capped at 500 strokes) so a viewer who joins mid-session isn't missing
  context.
- **Remote printing** — reuses Phase 3's file transfer: push a file to the
  host first, then `/print <filename> [printer]` at the chat prompt. The
  host only ever prints files inside its configured `--download-dir`
  (`resolve_print_path` rejects path separators, `..`, and absolute
  paths) and, by design, only lets the current *control* viewer print —
  a view-only participant can watch and chat but shouldn't be able to
  spend the host's paper.

## Running it

```bash
# Host
python3 host_p8.py --video-port 5000 --input-port 5001 \
                    --control-port 5002 --audio-port 5003

# Viewer (control)
python3 viewer_p8.py 192.168.1.100:5000 --id alice --password mypassword

# Viewer (view-only, no mic)
python3 viewer_p8.py 192.168.1.100:5000 --id bob --view-only --no-voice
```

New dependency: `pip install sounddevice numpy` for voice (everything
else — chat, whiteboard, print — has no new dependencies beyond what
Phase 7 already required).

## Notes / follow-ups for a later pass

- Voice is uncompressed PCM16 and unmixed on the host — fine for a
  handful of participants on a decent link; an Opus codec would help at
  scale, and per-speaker volume/mixing controls could move client-side.
- Windows printing shells out to the file's registered "print" verb,
  which covers the common default-printer case but not per-copy counts
  or explicit printer selection the way the CUPS (`lp`/`lpr`) path does
  on Linux/macOS.
- Whiteboard persistence is in-memory only and reset when the host
  process restarts; saving/exporting the board is a natural Phase 9+
  addition alongside the admin console's session history.
