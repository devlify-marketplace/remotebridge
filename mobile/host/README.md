# mobile/host — Phase 6: Mobile as Host

A Flutter app that lets a phone itself be remoted into — the mirror
image of `mobile/controller` (Phase 5), which lets a phone control
something else. It speaks the same wire protocol as `desktop/host.py`
— see `lib/net/protocol.dart`, the host-role half of the Dart port of
`desktop/protocol.py` — so an unmodified `desktop/viewer.py` or the
Phase 5 controller app can connect to it directly on a LAN or through
`server/relay.py` by device ID, without either of them needing to know
the far end is a phone rather than a desktop.

## What's here, mapped to the roadmap's Phase 6 checklist

- **Screen capture permission flow (iOS ReplayKit / Android
  MediaProjection)** — `lib/services/screen_capture_service.dart` is
  the Dart side of a platform channel; the actual capture is native
  code, since neither MediaProjection nor ReplayKit has a Flutter API.
  Android: `android/.../MainActivity.kt` (owns the
  `startActivityForResult` permission dialog) +
  `ScreenCaptureService.kt` (the foreground service that keeps
  capturing while backgrounded) + `ScreenCaptureBridge.kt`. iOS:
  `ios/Runner/ScreenCaptureBridge.swift`, registered from
  `AppDelegate.swift` per `ios/Runner/AppDelegate.snippet.swift`.
- **Screen mirroring to a remote viewer** — `lib/net/host_session.dart`
  streams the JPEG frames `screen_capture_service.dart` emits as
  `MSG_VIDEO_FRAME`s on the video channel, after running the same
  auth handshake `desktop/host.py` does (ported to
  `lib/models/host_config.dart`). `lib/screens/host_setup_screen.dart`
  picks direct-LAN ports or a relay device ID and configures access
  control; `lib/screens/host_status_screen.dart` runs the session and
  shows connection state, quality/battery readout, and log lines
  while it's live; `lib/widgets/session_confirmation_dialog.dart` is
  the accept/reject prompt shown when no valid unattended password
  was presented — the mobile counterpart of `auth.py`'s blocking
  console `input()`.
- **Remote file browser scoped to app-accessible storage** —
  `lib/services/host_file_browser.dart`. Same list/send/pull/chunk/
  complete control-channel messages `desktop/file_transfer.py`
  handles, but every path is resolved *inside* the app's documents
  directory and anything that would escape it (`../..`, an absolute
  path) is rejected — a sandboxing requirement here, not just a
  convenience default like on desktop.
- **Battery-aware throttling** — `lib/services/battery_guard.dart`
  tracks battery level/charging state and caps quality/fps by tier
  (normal / low / critical / charging-no-cap), combined with
  `lib/services/adaptive_bitrate.dart`'s network-side controller
  (Phase 4, ported unchanged) via a plain `min()` — whichever cap is
  stricter wins, so a low battery throttles even over a great
  connection.
- **TLS + session logging carried over from Phase 2/4** —
  `lib/services/cert_manager.dart` generates and caches a self-signed
  cert/key pair the first time the app hosts (no `openssl` on a
  phone, so `package:basic_utils` does it at runtime instead of
  shelling out to `generate_cert.sh`). `lib/services/session_log.dart`
  appends the same newline-delimited-JSON `sessions.log` format
  `desktop/session_log.py` writes, and `host_status_screen.dart` reads
  it back for the "recent connections" list.

## Deliberately deferred

Kept out to keep this phase's scope matched to the roadmap's Phase 6
line items, and to the project's existing pattern of calling out
what's still rough rather than silently skipping it (see
`mobile/controller/README.md`'s own section of the same name):

- **Input injection into the phone** (mouse/keyboard/touch control
  *of* the phone by a remote viewer) — not in the Phase 6 checklist,
  which only asks for the phone to be watched, not controlled. The
  input channel still exists and is drained (`host_session.dart`'s
  `inputDrainSub`) so an unmodified desktop viewer or the Phase 5
  controller app can open it without erroring on a missing
  port/registration — the bytes just aren't acted on. Wiring this up
  later would mean Android's `AccessibilityService` (there's no public
  API to synthesize arbitrary touch/key events otherwise) and, on
  iOS, nothing at all — Apple has no public API for a third-party app
  to inject system-wide input, full stop.
- **iOS background / system-wide screen mirroring** —
  `RPScreenRecorder.startCapture` (what `ScreenCaptureBridge.swift`
  actually calls) only captures this app's own foreground content. A
  Phase 6-complete "mirror whatever's on the phone's screen, including
  other apps, even backgrounded" needs a separate Broadcast Upload
  Extension target with its own App Group entitlement — a distinct
  Xcode target this repo doesn't generate, not something addable by
  editing a plist. See `ios/Runner/Info.snippet.plist`'s note. Android
  doesn't have this limitation — `MediaProjection` captures the whole
  device display from a foreground service regardless of which app is
  frontmost.
- **TOTP / two-factor and viewer-ID whitelisting** — `auth.py`'s full
  decision logic also covers a TOTP code and an allowed-ID list;
  `host_config.dart`'s port only carries over the password +
  prompt/timeout half (see that file's docstring). Not a protocol
  gap — `protocol.dart`'s `unpackAuthRequest` already carries
  `totp_code` — just not wired into `HostSession._decide()` or
  surfaced in `host_setup_screen.dart` yet.
- **Applying incoming clipboard text to the OS clipboard** —
  `host_session.dart`'s control-channel listener recognizes
  `MSG_CLIPBOARD_TEXT` but doesn't call `Clipboard.setData` on it yet;
  doing so needs `package:flutter/services.dart`, a UI-layer import
  the current `net/` layer deliberately stays free of. Left as a
  follow-up: wire a callback from `HostSession` that
  `host_status_screen.dart` implements.
- **Stopping mid-wait** — `HostSession.requestStop()` only breaks the
  outer per-session loop between connections; if the app is currently
  blocked awaiting the *next* viewer (socket accept / relay
  registration), leaving `host_status_screen.dart` early won't
  interrupt that wait. The next incoming connection (or app restart)
  is what actually stops it. Not fixed this phase — see that screen's
  file-level doc comment.
- **Host certificate pinning** — same documented pre-hardening trust
  model as the desktop viewer and the Phase 5 controller app: nothing
  on the viewer side pins this host's cert fingerprint yet, so TLS
  here defeats casual eavesdropping but not a MITM on the very first
  connection.

## Running it

This container doesn't have the Flutter SDK, so none of this has been
built or `flutter analyze`'d — it's been written and reviewed by hand
against the existing desktop protocol/auth code, the same way
`mobile/controller` was. To actually run it:

```
cd mobile/host
flutter create .        # fills in the platform scaffolding
                         # (android/, ios/) around the lib/ and
                         # pubspec.yaml already here — this will also
                         # regenerate MainActivity.kt/AppDelegate.swift
                         # from a template, so re-apply the versions
                         # already in android/.../MainActivity.kt and
                         # the ScreenCaptureService/Bridge files rather
                         # than letting flutter create overwrite them
flutter pub get
```

Then merge `android/app/src/main/AndroidManifest.snippet.xml` into the
generated `AndroidManifest.xml`, and `ios/Runner/Info.snippet.plist` +
`ios/Runner/AppDelegate.snippet.swift` into the generated
`Info.plist`/`AppDelegate.swift` (each snippet explains what its
entries are for). `flutter run` from there.

Point a `desktop/viewer.py` or the Phase 5 controller app at whatever
this app's setup screen shows — same ports for direct/LAN, same
relay host/port and device ID for relay mode.
