# mobile/controller — Phase 5: Mobile Controller App

A Flutter app (single Dart codebase for iOS + Android — the "shared
core SDK/engine where possible" the roadmap asks for) that finds,
connects to, and fully controls a desktop host from
`desktop/host.py`. It speaks the exact same wire protocol as
`desktop/viewer.py` — see `lib/net/protocol.dart`, a line-for-line
Dart port of `desktop/protocol.py` — so it works against any Phase 4
host, direct-on-LAN or through `server/relay.py` by device ID.

## What's here, mapped to the roadmap's Phase 5 checklist

- **Native iOS and Android apps, shared core** — one Flutter/Dart
  codebase (`lib/`); no per-platform UI code needed for the features
  below.
- **Touch input translation: tap-to-click, drag mode vs. direct-touch
  mode** — `lib/services/gesture_engine.dart`. Direct-touch maps a
  finger's screen position straight to the remote cursor (tap =
  click). Trackpad/drag mode instead moves the cursor by touch
  *delta*, like a laptop trackpad, so precise pointing doesn't depend
  on phone-screen-to-remote-screen size matching. Toggle is the
  touchpad/mouse icon in the app bar.
- **On-screen keyboard with modifier/function keys** —
  `lib/widgets/onscreen_keyboard.dart`: a text field for normal typing
  (each character becomes a key press+release immediately) plus a
  pinned row of toggleable modifiers (Ctrl/Alt/Shift/Cmd) and a
  collapsible Esc/F1–F12/Tab/Delete row.
- **Gesture shortcuts** — two-finger drag scrolls, two-finger
  pinch zooms the local view, three-finger tap sends a right-click.
  All in `gesture_engine.dart`.
- **Orientation lock / auto-rotate to match remote screen** —
  `lib/services/orientation_service.dart`, driven by the monitor
  dimensions the host already reports (`MSG_MONITOR_LIST_RESPONSE`,
  Phase 4). Lock toggle in the app bar.
- **Low-data mode for cellular connections** — toggle in the app bar.
  Rather than inventing a new wire message, it under-reports measured
  fps/kbps in the existing `MSG_STATS_REPORT` the viewer already sends
  once a second (Phase 4's adaptive-bitrate feedback), so the host's
  existing `AdaptiveBitrateController` backs off quality/fps on its
  own — no host-side change needed.
- **Biometric app lock (Face ID / fingerprint)** —
  `lib/services/biometric_gate.dart` via the `local_auth` plugin,
  gating app launch. Toggle in the device list's app bar; off by
  default so a fresh install isn't locked out with nothing configured.
- **Session picker for small screens (favorites, recents, search)** —
  `lib/screens/device_list_screen.dart` + `lib/models/device.dart`
  (persisted via `shared_preferences`).

## Deliberately deferred

Kept out to keep this phase's scope matched to the roadmap line item
(mobile *controller*, not mobile *host* — that's Phase 6) and to the
project's existing pattern of calling out what's still rough rather
than silently skipping it (see `desktop/README.md`'s own "Not yet
built" note):

- **Push notifications for incoming connection requests** — needs a
  backend push service (APNs/FCM) that doesn't exist yet anywhere in
  this repo; the relay is a dumb TCP pipe today.
- **Background reconnect on foreground** — the desktop viewer's
  reconnect-with-backoff logic isn't yet ported to `session_client.dart`;
  right now a dropped connection just ends the session.
- **Saved/remembered unattended passwords** — the connect screen
  intentionally does *not* persist the password/2FA fields alongside a
  favorite, to avoid quietly storing a plaintext secret in
  `shared_preferences`. A real "remember me" would need
  `flutter_secure_storage` (Keychain/Keystore-backed) gated behind the
  biometric lock, not shared_preferences.
- **Split-screen/multi-window and external keyboard/mouse passthrough
  on tablets** — no tablet-specific layout yet; it'll run, just without
  the DeX-style refinements.
- **Wi-Fi/cellular handoff without dropping the session** — inherits
  whatever the OS/socket layer does today; nothing explicit here.
- **Swipe-for-Alt+Tab** (mentioned as an example gesture in
  `docs/features.md`, not in the roadmap's Phase 5 bullet list) — the
  three gestures the roadmap actually asks for (scroll/pinch/right-
  click) are implemented; a swipe shortcut would slot into
  `GestureEngine._handleSingleFingerMove` alongside them.
- **Host certificate pinning** — `connectTls`/`connectRelayTls` trust
  whatever certificate the host presents, matching the desktop
  viewer's current (documented) pre-hardening trust model, not a
  mobile-specific gap.

## Running it

This container doesn't have the Flutter SDK, so none of this has been
built or `flutter analyze`'d — it's been written and reviewed by hand
against the existing desktop protocol/auth code instead. To actually
run it:

```
cd mobile/controller
flutter create .        # fills in the platform scaffolding
                         # (android/, ios/) around the lib/ and
                         # pubspec.yaml already here
flutter pub get
```

Then merge `android/app/src/main/AndroidManifest.snippet.xml` into the
generated `AndroidManifest.xml`, and
`ios/Runner/Info.snippet.plist` into the generated `Info.plist` (both
explain what each entry is for). `flutter run` from there.

Point it at a running `desktop/host.py` the same way `viewer.py` would
connect — same ports, same relay, same `--id`/password/2FA.
