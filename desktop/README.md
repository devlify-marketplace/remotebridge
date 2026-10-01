# Phase 4 — Performance & Reliability Pass

This is the Phase 4 deliverable from the roadmap:

> Stable enough for daily driver use on desktop.

Building on Phase 3 (control channel: clipboard + files), Phase 4 adds:

- **Adaptive bitrate/frame rate** — the viewer reports what it's
  actually receiving (frames/sec, kbps) roughly once a second over the
  control channel; the host nudges JPEG quality and fps up or down in
  response instead of holding fixed `--quality`/`--fps` all session.
  Quality drops before fps on the way down, and fps recovers before
  quality on the way up.
- **Auto-reconnect** — a network drop no longer kills either process.
  The **host** goes back to waiting for the next connection instead of
  exiting after one session (pass `--single-session` for the old
  behavior). The **viewer** retries the connection with a growing
  backoff (capped by `--max-backoff`, default 30s), resetting to a
  1-second wait as soon as a session connects successfully again (pass
  `--no-auto-reconnect` for the old exit-on-drop behavior). Pressing
  `q` in the viewer still quits for good — reconnect is only for
  *unintended* drops.
- **Multi-monitor support** — `monitors` lists the host's displays and
  which one is active; `monitor <n>` switches capture to another one,
  taking effect on the very next frame.
- **Session recording** — `record <path.mp4>` starts writing the
  viewer's decoded video stream to a local file; `record stop` ends it.

## What's included (new/changed in Phase 4)
- `adaptive.py` — `AdaptiveBitrateController` (host) and
  `StatsReporter` (viewer)
- `monitors.py` — monitor enumeration, `ActiveMonitor` (host state),
  `MonitorHost`/`MonitorClient` (control-channel RPC)
- `recorder.py` — `SessionRecorder`, wrapping a `cv2.VideoWriter`
- `protocol.py` — adds `MSG_STATS_REPORT` and the
  `MSG_MONITOR_LIST_REQUEST`/`_RESPONSE`/`MSG_MONITOR_SWITCH` trio
- `control_loop.py` — routes the new message types to `adaptive`/
  `monitor_handler` when the caller passes them (both optional, so
  existing callers are unaffected)
- `console.py` — adds `monitors`/`monitor <n>`/`record <path>`/
  `record stop` commands (viewer-only; host's console prints a clear
  "not available here" if you try them there)
- `host.py` — `run()` is now a loop around `run_one_session()`; video
  capture reads quality/fps from an `AdaptiveBitrateController` and
  the active monitor from an `ActiveMonitor`, both mutated by the
  control channel while `video_loop` runs
- `viewer.py` — `run()` is now a reconnect loop around
  `run_one_session()`; `video_display_loop` reports stats after every
  frame and feeds the (optional) recorder

## Setup (on both machines)
```bash
pip install -r requirements.txt
```
Clipboard text sync still needs a backend for `pyperclip` — on Linux
that means `xclip` or `xsel` installed. Recording uses OpenCV's own
`cv2.VideoWriter` (already a dependency from Phase 0), writing H.264
if your OpenCV build has it, otherwise falling back to whatever codec
`mp4v` resolves to locally.

On the **host** machine only, generate a certificate once (if you
haven't already from Phase 1):
```bash
./generate_cert.sh
```

## Configuring the host's access control (Phase 2, still in effect)
```bash
python3 configure_host.py set-password        # prompts, stores a salted hash
python3 configure_host.py enable-2fa           # optional, prints a TOTP secret/URI
python3 configure_host.py whitelist-add my-laptop   # optional, repeatable
python3 configure_host.py show                 # see current settings
```

## Running it

**Host** — now a long-running process by default; it keeps waiting for
the next viewer after each session ends:
```bash
python3 host.py
# or, to go back to exiting after one session:
python3 host.py --single-session
# or, to fix quality/fps instead of letting them adapt:
python3 host.py --quality 70 --fps 20 --no-adaptive
```

**Viewer** — reconnects automatically on a network drop by default:
```bash
python3 viewer.py --host <HOST_LAN_IP> --viewer-id my-laptop
# or, to exit on a drop instead of retrying:
python3 viewer.py --host <HOST_LAN_IP> --viewer-id my-laptop --no-auto-reconnect
```

Relay mode works the same way as earlier phases — the host
re-registers on the relay between sessions, and the viewer's reconnect
attempts re-run the same `CONNECT` handshake.

Once connected, try the new console commands alongside the existing
file/clipboard ones:
```
(files) > monitors
(files) > monitor 2
(files) > record session.mp4
(files) > record stop
```

## Known limitations (by design, for this phase)
- **Adaptation is reactive, not predictive.** It only responds to what
  the viewer already fell behind on in the last ~1 second window —
  fine for steady congestion, sluggish for sudden spikes.
- **Recording is viewer-side and un-synced with audio** — there's no
  audio channel yet in this app, so recordings are silent video only.
- **Reconnect re-authenticates from scratch each time** — it does not
  resume a session's state (e.g. an in-flight file transfer's `.part`
  file still resumes correctly by content, but a `send`/`get` that was
  actively transferring when the drop happened has to be re-issued
  manually after reconnecting).
- **Monitor switching is capture-only** — mouse/keyboard input
  coordinates are still normalized against the *first* monitor's
  resolution (`input_loop`'s `screen_w`/`screen_h`), so clicking after
  switching to a differently-sized monitor can be off until a later
  phase teaches `input_loop` to track the active monitor too.
- Whatever earlier phases already didn't cover (certificate pinning,
  rate-limiting failed auth attempts, real drag-and-drop) still
  doesn't — unrelated to this phase's scope.

## What to test
1. Start a session, then unplug/disable networking on either machine
   briefly — confirm the other side prints a "disconnected" message
   and (viewer) starts retrying, or (host) goes back to listening,
   instead of the process exiting.
2. Throttle the network (e.g. `tc` on Linux, or just a busy Wi-Fi) and
   watch the host's console print roughly track lower quality/fps
   under load, recovering once the throttle lifts.
3. On a host with more than one monitor, run `monitors`, then
   `monitor 2` (or whichever index isn't already active) and confirm
   the video window switches to that display.
4. Run `record out.mp4`, move the mouse/type for a few seconds, then
   `record stop`, and confirm `out.mp4` plays back what was on screen.
5. Confirm `q` in the viewer still exits cleanly (no reconnect attempt
   after an intentional quit).

## Next step
Phase 5 moves to mobile: native iOS/Android controller apps that can
find, connect to, and fully control a desktop host, building on this
same protocol.

## Certificate pinning (viewer)

Hosts use self-signed certificates, so the viewer pins them (trust on first use, like SSH):

- The **host prints its certificate fingerprint** (SHA-256) at startup. Give it to the people who will connect.
- **First connection**: the viewer shows the fingerprint and asks whether to trust it (in a script with no terminal it
  trusts and remembers, and says so). Better: pass `--pin <fingerprint>` and the connection is refused unless the
  host presents exactly that certificate.
- **Later connections** must present the same certificate. A different one is **refused** (exit code 3) with both
  fingerprints shown - it means either someone is intercepting the connection or the host's certificate was
  regenerated. After confirming the new fingerprint with the host's owner, reconnect with `--replace-pin`.
- All four channels of a connection must present the same certificate.
- Pins live in `~/.remotebridge/known_hosts.json` (`--known-hosts` or `$REMOTEBRIDGE_KNOWN_HOSTS` to move it),
  keyed by `host:video-port`, or by `device:<id>` when going through a relay (the relay is only a pipe).
  If that file is unreadable the viewer refuses to connect rather than treat every host as new.
