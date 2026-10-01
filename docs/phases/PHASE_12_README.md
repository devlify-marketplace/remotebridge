# Phase 12: Scale & Polish — Implementation

Roadmap goal: *handle growth and rough edges before a wider public launch.*
Four items — relay load-testing and failover, accessibility, localization, an
in-app support channel — plus **three real bugs** found by finally running the
real host and viewer against each other (first, because it blocks everything
else). Read "What is and isn't done" before calling this launch-ready: it isn't
yet, and the list of why is short and specific.

## Use `host_p12.py` / `viewer_p12.py` from now on

`host_p11.py` + `viewer_p10.py` **cannot complete a connection** (bug 1 below),
so every launcher (`deploy/linux/install.sh`, the PyInstaller spec, the WiX
build, `cli/remotebridge.py`) now points at the p12 entry points. The wire protocol
is unchanged apart from two new control messages an older peer ignores: a
Phase 10 viewer connects to a Phase 12 host fine (tested), and a Phase 12
viewer connecting to an older host works except `/feedback`, which reports no
answer. `host_p7`–`host_p11` and `viewer_p7`–`viewer_p9` have since been **deleted** (they
still contained the bugs below); `viewer_p10.py` remains, marked deprecated, only
because the e2e test uses it to prove old viewers still connect.

## Bugs found and fixed (all three were in Phases 7–11)

Found because Phase 12's end-to-end test launches the real `host`/`viewer`
processes through real relays instead of calling functions with fakes. Each was
reproduced against the **unmodified Phase 11 zip** before being fixed.

1. **Every multi-user connection died with "The handshake operation timed
   out" — direct or relayed.** The viewer handshakes TLS on each channel as it
   opens it (video, input, control, audio) and only sends its auth request once
   all four are up. The host did the opposite: it accepted all four raw sockets
   first (handshaking none), then handshook video, waited for auth, and handshook
   the other three only *after* auth. Each side waited on the other. Fixed in
   `host_p12.handshake_channels`: complete TLS on all four channels in the
   viewer's order, then authenticate. Bounded by a 10 s timeout, including the
   secondary accepts, so a client that opens video and stalls can't wedge the
   host's accept loop. (Phase 11's fix of the auth double-read was real, but this
   second deadlock sat right behind it.)
2. **Host: the control handler died on the first stats report** —
   `bitrate.report_stats()` doesn't exist; the method is `record_report()`. The
   exception killed that viewer's whole control thread, so chat, print,
   clipboard and file transfer stopped after a few seconds. Fixed in `host_p12`.
3. **Viewer: control thread died on the first session-info message** —
   the host sends `"mode": "control"`, the viewer read `["view_mode"]` (an int
   the docs describe but the host never sends) → `KeyError`. `viewer_p12` accepts
   both shapes.

A fourth, smaller one in the admin console: **no POST form had CSRF
protection**, and `/logout` was a GET. Both fixed (see Accessibility/Admin).

## 1. Relay load testing and failover

### Load testing — `server/loadtest.py`
Opens N concurrent simulated host/viewer pairs (REGISTER/CONNECT, then rounds of
"frame" viewer→host and "input ack" host→viewer), reporting sessions
established, connect latency, round-trip latency, throughput, and — for a relay
it launches itself — the relay process's threads, RSS and CPU.

```bash
python3 server/loadtest.py --sessions 2000 --ramp 5 --rounds 30 --think-ms 250 --frame-kb 1
python3 server/loadtest.py --relay-host relay.example.com --relay-port 6000 --sessions 300
python3 server/loadtest.py --relay-script path/to/other_relay.py --sessions 1000    # compare builds
```

Measured on one 4 GB, 4-core sandbox with generator and relay sharing loopback
(so it measures the relay's *software* limits, not your network, not TLS, and
competes with the generator for CPU):

| Scenario | Phase 1 relay | Phase 12 relay |
|---|---|---|
| 3000 sessions, steady light traffic | **136 of 3000 failed** (`not_found`: CONNECT beat REGISTER) | 3000/3000 established, 0 failures |
| Threads for ~2000 live sessions | ~6,100 (3 per session) | ~4,000 (2 per session) |
| 1000 sessions, 32 KB frames, no pause | 138 MB/s, 101 MB RSS | 165 MB/s, 267 MB RSS |
| Connect p50 in a 1000-session synchronized burst | 329 ms | 406 ms |
| 9000 sessions over 6 s ramp (Phase 12 only) | — | 9000/9000, 0 failures, peak 8,244 threads, 633 MB RSS |

Read that honestly: the gains are **correctness** (no lost connections; dead
hosts reaped) and **fewer threads**; raw forwarding speed is about the same, the
new relay uses **more memory** (bigger buffers) and is **slightly slower to
connect** in a synchronized burst on this box (not profiled). Real capacity
depends on your hardware, frame bitrates (a screen stream is far heavier than the
1 KB/250 ms test), and TLS (end-to-end, so the relay never pays for it). The
thread-per-connection design will want replacing by an async relay somewhere past
~10k sessions; I didn't build that.

### What changed in `server/relay.py` (wire protocol unchanged, plus `PING`/`STATS`)
Dead registrations are reaped (and never paired — the old relay would hand a
viewer a corpse); duplicate REGISTER replaces and closes the old one instead of
leaking it; **CONNECT waits (default 3 s) for the name to register** (removes the
race that lost connections above, and the multi-channel input/control/audio race);
slow-loris and oversize command protection; `--max-waiting` / `--max-sessions`
limits that refuse cleanly; TCP keepalive; optional `--idle-timeout`; graceful
SIGTERM; `PING`→`PONG` for load-balancer health checks; `STATS` (JSON counters,
loopback only unless `--stats-open`).

### Redundancy — client-side, deliberately
Relays share no state, so there's nothing to synchronize and no relay is
special: run several and give clients the list (`desktop/relay_client.py`).

- **Host** `--relay a:6000,b:6000 --id office-pc` registers on **all** relays,
  takes whichever one a viewer reaches first, withdraws the others, and
  re-registers with backoff on any relay that drops it or comes back from a
  restart.
- **Viewer** `--relay a:6000,b:6000 --device office-pc` tries relays in order
  (a dead one, or one the host isn't registered on, just moves on); all four
  channels stay on the relay that paired the first.
- `host_p12.py` now actually **uses** `--relay` (Phases 7–11 accepted the flag and
  ignored it; only the single-session `host.py` used a relay). `host.py` and
  `viewer.py` got the same multi-relay support.

**Limits:** failover is at *connect* time. A session already running through a
relay that then dies ends (the viewer's existing auto-reconnect picks a live relay
next time); it is not migrated. The relay has **no authentication**: anyone who
can reach it can `REGISTER` a device's name and displace the real host (end-to-end
TLS + auth still protect the content, but it's a cheap denial of service). Per-ID
registration secrets or allow-listing the relay to your network is the launch fix.

## 2. Accessibility

**Admin console** (web): skip link; single `<main>` landmark; `lang` attribute
follows the chosen language; every form control has a programmatic label (the
per-row inputs say *which device*); table captions and `scope="col"`; nav exposes
`aria-current`; flash messages use `role="alert"` / `role="status"`; decorative
bars are `aria-hidden`; the group `<select>` no longer **submits on change**
(arrowing through options used to change a device's group); visible focus rings;
`prefers-reduced-motion`; a forced-colors block; and **high contrast**, chosen
from the sidebar (works without JavaScript) or applied automatically for
`prefers-contrast: more`. The default palette failed WCAG AA for error text
(4.0:1) and was corrected to 6.0:1; the high-contrast theme is ≥ 10:1 (AAA). The
test suite computes the ratios from the real CSS.

**Desktop viewer:** status lines (joins/leaves, policy denials, print results,
connection state, errors) print as `[status] …` and, with `--speak`, are also
spoken through the OS engine (`say`, PowerShell SAPI, `spd-say`/`espeak`); a
bounded queue drops stale speech rather than lagging. `--high-contrast` draws
whiteboard strokes in a bright palette over a black outline and the mode banner
on a black plate.

**What this cannot do:** the remote screen is pixels, so — like every remote
desktop tool — a screen reader cannot read *the remote machine's contents*
through the video window; use the remote machine's own screen reader plus voice
chat. The whiteboard is pointer-driven. **Nothing here was tested with a real
screen reader (NVDA, JAWS, VoiceOver, Orca) or by disabled users** — the checks
are structural (labels, landmarks, roles, contrast maths). Do that before
claiming "accessible". The Flutter apps in `mobile/` were not touched.

## 3. Localization

English-text-as-key catalogs (`desktop/locales/<code>.json`,
`admin/locales/<code>.json`); a missing or placeholder-mismatched translation
falls back to English instead of crashing a live session. Admin: per-request
language from a cookie, else `Accept-Language` (q-values honoured), picker in the
sidebar. Desktop: `--lang`, `REMOTEBRIDGE_LANG`, else the OS locale;
`es_MX` falls back to `es`. Host "reason" strings sent over the wire (e.g.
"incorrect unattended-access password") are translated **at the viewer**, so the
protocol stays language-neutral. Templates escape interpolated values (a device
named `<b>x</b>` renders as text, tested).

**Shipped: Spanish.** Adding a language is one JSON file. The pseudo-language
`en-XA` (set `lang=en-XA`) accents and pads every translated string: the admin
test renders every page in it and fails if any visible text bypasses translation.

**Not done / caveats:**
- The Spanish was written by me, **not reviewed by a native speaker** — `_meta.reviewed`
  is `false`. Get it reviewed (and more languages added) before launch.
- Desktop coverage is the strings in `host_p12`, `viewer_p12`, `feedback_cli`
  (55). The older modules' prints (file console, clipboard, recorder, `auth.py`…)
  are still English. No plural-rule engine beyond the one singular/plural pair in
  the admin UI; no RTL layout.
- The viewer's OpenCV banner uses a Hershey font that draws **ASCII only**: a
  language needing accents/non-Latin script cannot render the whiteboard banner
  there (the Spanish one is written without accents for this reason). A real fix
  means drawing text with PIL/Qt.
- Dates/numbers aren't locale-formatted.

## 4. In-app support / feedback

- **Console:** new *Feedback* inbox (open/resolved/all, unread count in the nav),
  ticket page, admin-only reply (+ mark resolved), auditors read-only.
- **API:** `POST /api/v1/feedback` (device token; 4000-char cap; 20/hour/device;
  unknown category → `other`), `GET /api/v1/feedback` (a device sees **only its own**
  tickets, never internal fields like who replied), `GET /api/v1/ops/feedback`
  (operator key).
- **Host operator:** `python3 desktop/feedback_cli.py send "…" --category bug` /
  `replies`.
- **Viewer:** at the chat prompt, `/feedback [bug|question|idea] <text>` and
  `/replies`. The viewer has no console credential, so it asks **its host**
  (`MSG_FEEDBACK`, `protocol_p12.py`), which files the ticket under its own
  enrollment stamped with the viewer's ID and lists back only that ID's tickets.
  Tested: a second viewer on the same host cannot see the first's ticket. The viewer
  ID is self-declared at auth, so this is a convenience filter, not a boundary
  against someone impersonating another ID.

## Also in the admin console
CSRF tokens on every browser POST form (none before; the JSON API is exempt by
design — it uses bearer tokens), `/logout` is POST, `set-language`/`set-theme`
only redirect to same-site paths (backslash tricks rejected).

## Running the tests
```bash
python3 tests/test_relay.py      # 17: relay + failover over real loopback sockets
python3 tests/test_i18n.py       # 12: catalogs, fallbacks, pseudo-locale, coverage
python3 tests/test_admin_p12.py  # 35: CSRF, feedback API, localization, a11y, contrast, XSS
python3 tests/test_p12_e2e.py    # 3: real host_p12/viewer_p12 processes via 2 relays (one killed) and the
                                 #    real admin console; needs Xvfb + openssl (skipped without)
```
All 67 pass here. The end-to-end test is the one that caught bugs 1–3; it also
covers direct mode, an unmodified Phase 10 viewer, and a rejected password
(translated).

## What is and isn't done (before a public launch)
Done and verified: relay hardening + load numbers + client failover; admin-console
accessibility structure and contrast; Spanish + the i18n machinery; the feedback
channel end to end; the three connection bugs.

Still needed — none of it could be done from here:
1. Real assistive-technology testing with real users (see above).
2. Native-speaker review of the Spanish, and any further languages.
3. Load test **from a separate machine against the deployed relay**, with real TLS
   video bitrates; relay registration authentication; run ≥ 2 relays in different
   networks/regions and put `PING` health checks on them.
4. Everything Phase 10 already flagged: the MSI/PyInstaller/WiX outputs have never
   been built or run (`deploy/README.md`).
5. `mobile/` apps: no relay failover, localization, or accessibility pass.
6. The Phase 7–11 scripts still carry bugs 1–3; delete them or keep them clearly
   marked as superseded.
