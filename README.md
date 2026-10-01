# RemoteBridge

An AnyDesk-style remote access tool, built in phases. See
[`docs/roadmap.md`](docs/roadmap.md) for the full phase-by-phase plan
and [`docs/features.md`](docs/features.md) for the full feature spec
(including mobile).

## Project structure
```
remotebridge/
├── docs/
│   ├── features.md        # Full feature spec (desktop + mobile)
│   ├── roadmap.md         # Phased build plan
│   └── phases/            # Phase-by-phase implementation write-ups (7+)
│       ├── PHASE_7_README.md
│       ├── PHASE_8_README.md
│       ├── PHASE_9_README.md
│       ├── PHASE_10_README.md
│       ├── PHASE_11_README.md
│       └── PHASE_12_README.md
├── desktop/               # Phase 0-4 base, extended through Phase 10
│   ├── protocol.py         # shared wire protocol (video/input/auth/files/clipboard/stats/monitors)
│   ├── protocol_p7.py      # + multi-user session info / view-only / permissions
│   ├── protocol_p8.py      # + chat / whiteboard / print / voice
│   ├── protocol_p9.py      # + policy-denied notice
│   ├── protocol_p10.py     # + branding notice
│   ├── host.py, viewer.py          # Phase 0-4 single-viewer host/viewer
│   ├── viewer_p10.py               # DEPRECATED - kept only for the old-viewer compatibility e2e test
│   ├── host_p12.py, viewer_p12.py  # Phase 12: USE THESE - multi-relay failover, feedback, i18n, a11y,
│   │                               #   and fixes for 3 connection bugs found in the removed p7-p11 scripts (see PHASE_12_README.md)
│   ├── relay_client.py, i18n.py, a11y.py, support.py, feedback_cli.py, protocol_p12.py, locales/
│   ├── session_manager.py  # multi-user viewer tracking (Phase 7, +audio Phase 8)
│   ├── whiteboard.py, print_service.py, audio_chat.py   # Phase 8 collaboration tools
│   ├── admin_client.py     # Phase 9: enrollment / policy fetch / session reporting
│   │                       # Phase 10: + branding / release fetch / deploy_config
│   ├── updater.py          # Phase 10: version compare, download, verify, apply
│   ├── auth.py             # access control: password/2FA/whitelist/prompt
│   │                       # Phase 11: + decide_host_auth (fixes a real deadlock, see PHASE_11_README.md)
│   ├── configure_host.py  # CLI to manage host_config.json
│   ├── session_log.py     # appends to sessions.log
│   ├── clipboard_sync.py  # clipboard watch/apply
│   ├── file_transfer.py   # file listing/push/pull with resume
│   ├── console.py         # interactive file-manager + monitor/record commands
│   ├── control_loop.py    # control-channel dispatch loop
│   ├── adaptive.py        # adaptive bitrate/fps controller + viewer stats reporter
│   ├── monitors.py        # monitor enumeration/switching over the control channel
│   ├── recorder.py        # session recording to a local video file
│   ├── generate_cert.sh   # creates the host's TLS certificate
│   ├── requirements.txt
│   └── README.md          # Phase 4 walkthrough (later phases: docs/phases/)
├── server/                # Phase 1+: relay/signaling server
│   ├── relay.py            # ID-based relay (built, Phase 1)
│   └── README.md
├── admin/                 # Phase 9: admin console & policy; +deployment/branding (10); +automation (11)
│   ├── server.py, store.py, policy.py, wol.py
│   ├── templates/, static/
│   └── README.md
├── deploy/                # Phase 10: scripted/MSI deployment
│   ├── linux/install.sh    # venv + systemd unit (built and run in this env)
│   ├── pyinstaller/, wix/  # Windows exe + MSI (written, not run - see deploy/README.md)
│   ├── deploy_config.template.json
│   └── README.md
├── cli/                   # Phase 11: remotebridge.py - scripted wake/connect/pull-data (stdlib only)
│   ├── remotebridge.py
│   └── README.md
└── mobile/                # Phase 5-6: iOS/Android apps
    ├── controller/         # Phase 5: control a PC from a phone (built)
    ├── host/               # Phase 6: let others remote into a phone (built)
    └── README.md
```

## Hosted deployment (Render + Neon)
The admin console deploys to Render's free tier with a Neon Postgres database: see
[`docs/DEPLOY_RENDER_NEON.md`](docs/DEPLOY_RENDER_NEON.md) and [`render.yaml`](render.yaml).
The relay is raw TCP and needs a VPS/Fly.io-style host instead (`server/Dockerfile`).

## Current status
**Phase 13 (relay authentication) built** - see [`docs/phases/PHASE_13_README.md`](docs/phases/PHASE_13_README.md).
The relay can require a per-device token to register (stops device-ID squatting), the admin console
issues tokens to enrolled devices, and repeated failures are banned. Static tokens / no per-device
revocation are documented limits.

**Phase 12 (scale & polish) built** — see
[`docs/phases/PHASE_12_README.md`](docs/phases/PHASE_12_README.md). Relay
hardening, a load tester (`server/loadtest.py`) and client-side multi-relay
failover; accessibility (admin console structure + contrast + high-contrast
theme; viewer status lines, `--speak`, `--high-contrast`); localization
machinery with a Spanish catalog; and an in-app feedback channel
(console inbox, `/feedback` in the viewer, `feedback_cli.py`). **It also found
and fixed three bugs that stopped `host_p11` + `viewer_p10` from ever completing
a connection** (a TLS-handshake ordering deadlock, a wrong method name, a wrong
dict key) — use `host_p12.py` / `viewer_p12.py`. Not launch-ready yet: see the
write-up's "What is and isn't done" (no real screen-reader testing, Spanish not
native-reviewed, no cross-machine load test, unauthenticated relay).

**Phase 11 (automation & remote ops) built** — see
[`docs/phases/PHASE_11_README.md`](docs/phases/PHASE_11_README.md),
[`admin/README.md`](admin/README.md), and [`cli/README.md`](cli/README.md).
An operator API key (or the console's own login) now authorizes a
separate REST surface, `/api/v1/ops/`, for listing devices, pulling
session history, sending Wake-on-LAN, and minting a single-use
connection a host accepts with nobody approving anything at its
console — `cli/remotebridge.py` is a small scriptable client for exactly
that. **This phase also found and fixed a real bug**: every multi-user
host (Phases 7-10) deadlocked on an actual connection attempt, because
`accept_viewer` read the auth request itself and then handed the same
socket to a function that tried to read a second one that was never
coming. It only ever looked fine because the earlier phases' own tests
used fake sockets that can't block — see the Phase 11 write-up for the
full story and the fix (`auth.decide_host_auth`).

**Phase 10 (deployment & branding) built** — see
[`docs/phases/PHASE_10_README.md`](docs/phases/PHASE_10_README.md) and
[`deploy/README.md`](deploy/README.md). An org sets its display name,
support URL, and latest release (version/URL/checksum) once, on the
admin console's new Deployment page; every enrolled host picks up
branding for itself and connecting viewers, and checks/applies updates
according to its group's `auto_update` policy. `deploy/linux/install.sh`
(a venv + systemd unit) is genuinely built and run in this project's own
environment; the Windows path (`deploy/pyinstaller` + `deploy/wix`,
PyInstaller + WiX Toolset producing an MSI) is written correctly against
both tools' documented interfaces but not compiled here - see
`deploy/README.md` for exactly what that distinction means. A host with
no `--admin-url` (and no `deploy_config.json`) behaves exactly like
Phase 9.

**Phase 9 (admin console & policy) built** — see
[`docs/phases/PHASE_9_README.md`](docs/phases/PHASE_9_README.md) and
[`admin/README.md`](admin/README.md). A central console (`admin/`) now
tracks enrolled devices and their session history, and pushes down a
per-group policy — unattended access, file transfer, clipboard, chat,
whiteboard, printing, voice, viewer caps, session time limits, and an
org-wide viewer-ID allow/block list — that `host_p9.py` fetches and
actually enforces. A host with no `--admin-url` configured behaves
exactly like Phase 8.

**Phase 8 (collaboration & support tools) built** — see
[`docs/phases/PHASE_8_README.md`](docs/phases/PHASE_8_README.md).
In-session text and voice chat, a whiteboard/annotation overlay, and
remote printing, all built on Phase 7's multi-user foundation.

**Phase 7 (multi-user foundation) built** — see
[`docs/phases/PHASE_7_README.md`](docs/phases/PHASE_7_README.md). A
host can now accept several simultaneous viewers, with a view-only mode
for everyone but the one viewer holding input control.

**Phase 6 (mobile host) built** — see
[`mobile/host/README.md`](mobile/host/README.md) for what it
implements (screen mirroring, scoped file browser, battery-aware
throttling), what's deferred (input injection into the phone, iOS
background/system-wide mirroring, TOTP/whitelist), and how to
build/run it — same caveat as Phase 5 below: written and reviewed by
hand, not compiled, since this environment has no Flutter SDK.

**Phase 5 (mobile controller) built** — see
[`mobile/controller/README.md`](mobile/controller/README.md) for what
it implements, what's deferred, and how to build/run it (Flutter SDK
isn't available in the environment it was written in, so it hasn't
been compiled — written and reviewed by hand against the existing
protocol/auth code instead).

**Phase 4 complete** — see [`desktop/README.md`](desktop/README.md) to
run it. You can now:
- Connect directly on a LAN, or through the relay by device ID
- Control the host's mouse and keyboard from the viewer, not just watch
- Require a console confirmation, an unattended password, and optional
  2FA before a session starts, plus restrict which viewer IDs may even
  attempt a connection
- Read back who connected, when, and for how long from `sessions.log`
- Sync clipboard text (and, best-effort, images) between host and viewer
- Push/pull files in either direction with a resume-on-retry `.part`
  file, and browse both sides' filesystems from an interactive console
- Let quality/fps adapt automatically to what the viewer is actually
  receiving, instead of holding a fixed setting all session
- Survive a network drop — the host keeps listening for the next
  connection, and the viewer retries with backoff — instead of either
  process exiting
- List and switch between the host's monitors mid-session
- Record the viewer's video stream to a local video file
- Everything is TLS-encrypted (though the viewer doesn't yet verify the
  host's certificate against a known fingerprint — still a later
  hardening item)

Not yet built: the scale/accessibility/localization pass (Phase 12).
See `docs/roadmap.md` for the full sequence.
