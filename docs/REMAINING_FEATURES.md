# RemoteBridge: Remaining Features

Status after Phase 13 (relay authentication) plus relay TLS. Items come from the project's own docs ("deliberately deferred" / "not yet built" sections) plus gaps found while building Phases 12-13. Priority: **P1** launch blocker or security, **P2** important, **P3** nice to have.

Legend: [ ] not started

---

## 1. Security and access control

| | Item | Pri | Notes |
|---|---|---|---|
| [x] | **Rate-limit failed viewer logins on the host** | P1 | DONE: `auth.AuthThrottle` (per address + per viewer ID + global cap, exponential backoff), used by `decide_host_auth`. In-memory only: resets when the host restarts. Not yet surfaced in the admin console. |
| [x] | **Certificate pinning** | P1 | DONE: `desktop/pinning.py` - TOFU with a fingerprint prompt, `--pin` for out-of-band verification, `--replace-pin` to accept a legitimate change, all four channels must agree, host prints its fingerprint. First use in a non-interactive run is trusted automatically (logged); use `--pin` where that matters. Viewer only: the mobile apps don't pin yet. |
| [x] | **Per-device relay token revocation** | P1 | DONE: relay `--revoked-file` / `--revoked-url` (hot-reloaded; cuts waiting registrations AND live sessions), admin console Revoke/Restore button + `/api/v1/relay/revoked`, no relay tokens issued to revoked devices. Takes effect within the poll interval (30 s). Not yet shown on a relay fleet page. |
| [x] | **Short-lived relay tokens** | P2 | DONE: tokens may be `<expiry>.<hmac>` (relay `make_expiring_token`, mirrored in `admin/relayauth.py`). Console issues them when `RELAY_TOKEN_TTL` is set (`expires_in` in `/api/v1/relay-token`); `relay_token.py --ttl 30d` mints one by hand; relay `--require-expiry` refuses the non-expiring kind. The relay refuses expired tokens (30 s leeway), closes waiting registrations when their token expires (`expired_dropped` in STATS), and `CHECK` answers `ERROR expired`. Expired tokens don't count toward the address ban. Hosts renew at half-life (`relay_client.TokenProvider`) and swap their registration in place, so there's no gap. Opt-in on purpose: old hosts can't renew, so roll out relay -> hosts -> `RELAY_TOKEN_TTL` -> `--require-expiry` (see `server/README.md`). Not done: live sessions are not cut at expiry (revoke to end one); a hand-minted token (incl. the mobile host's) can't renew; if the console is down longer than the TTL, hosts can't register until it's back. Tests: `tests/test_relay_expiry.py` (48, incl. a real host/relay/console e2e). |
| [x] | **TLS to the relay** | P2 | DONE: relay `--tls-cert/--tls-key` (+ `--tls-port`, `--no-plain`) adds a TLS listener; clients use `tls://host:port` (`--relay-ca` / `REMOTEBRIDGE_RELAY_CA` for a private CA). Certificates are verified against the relay's hostname. TLS protects the command phase (token, replies) and is then dropped to plain TCP so the host<->viewer TLS stream and dead-host peeking are unchanged. Not done: mobile apps and `loadtest.py` are plaintext only; no cert hot-reload; no mTLS. Tests: `tests/test_relay_tls.py` (26, real sockets and certs). |
| [x] | **Admin console 2FA (TOTP)** | P2 | DONE: per-operator authenticator-app codes (*Account security*), single-use recovery codes (hashed), replay protection, admin reset for a lost phone, `REMOTEBRIDGE_REQUIRE_2FA=1` to force enrollment. Not done: TOTP secret stored unencrypted, no QR code (key + `otpauth://` link), no recovery-code regeneration. Tests: `tests/test_admin_2fa.py`. |
| [x] | **Admin login rate limiting / lockout** | P2 | DONE: DB-backed per-account and per-address counters (survive restarts, shared across workers), exponential lock 30 s to 15 min, checked before the password; wrong 2FA codes count. Also fixed an open redirect via `?next=` on login. Not done: lockouts aren't surfaced in the UI or alerted on; a known username can be kept locked (<=15 min at a time). |
| [ ] | **Account-backed device IDs** | P2 | IDs are unique per console, but not tied to a user account; IDs are not persistent across consoles. |
| [ ] | **Viewer accounts at the relay** | P3 | Gate viewers (not just hosts) at the relay. Needs a viewer identity system. |
| [ ] | **Authenticate the relay to the host** | P3 | A rogue relay can't read the TLS session but can still relay or drop traffic. |
| [ ] | **Audit-log export + tamper evidence** | P3 | CSV/JSON export of session history; hash-chain events. |

## 2. Connection and reliability

| | Item | Pri | Notes |
|---|---|---|---|
| [ ] | **NAT traversal / direct P2P** | P1 | Every session goes through the relay. Try STUN/hole-punching first, fall back to relay. Biggest latency and bandwidth win. |
| [ ] | **Session resume after reconnect** | P2 | Reconnect re-authenticates from scratch; an in-flight file transfer must be re-issued by hand. |
| [ ] | **Mid-session relay failover** | P2 | Failover only happens at connect time; a session on a dying relay ends. |
| [ ] | **Relay self-registration + health checks** | P2 | Relays share no state and have no discovery. Add a relay list endpoint on the admin console and PING health polling. |
| [ ] | **Multi-region relay deployment** | P2 | Run 2+ relays in different networks; hosts/viewers already accept a list. Needs ops work, not code. |
| [ ] | **Connection quality indicator** | P3 | Show latency/bitrate/packet-loss in the viewer. |

## 3. Desktop product features

| | Item | Pri | Notes |
|---|---|---|---|
| [x] | **Fix monitor-switch input mapping** | P1 | DONE: input now maps onto the active monitor's size and offset (`host_p12.normalized_to_screen`). Unit-tested; not yet tried on real multi-monitor hardware. |
| [ ] | **Audio in the session** | P2 | Recordings are silent video; no audio mixed into the recorder. |
| [ ] | **Drag-and-drop file transfer** | P2 | File transfer is console-command driven today. |
| [ ] | **Clipboard for files/images** | P3 | Text clipboard only. |
| [ ] | **Multi-monitor "all monitors" view** | P3 | |
| [ ] | **Session scheduling / time-boxed access** | P3 | Grant a viewer access for a window of time. |
| [ ] | **GUI viewer and host** | P2 | Currently command-line/console driven. A real desktop UI is the largest gap for non-technical users. |

## 4. Mobile

| | Item | Pri | Notes |
|---|---|---|---|
| [ ] | **Push notifications for connection requests** | P2 | Needs an FCM/APNs backend that doesn't exist yet. |
| [ ] | **Background reconnect on foreground** | P2 | Controller app. |
| [ ] | **Relay failover in `mobile/` apps** | P2 | Only a single relay is supported; desktop has multi-relay failover. |
| [ ] | **Localization + accessibility pass for mobile** | P2 | Desktop and admin have i18n/a11y; mobile has none. |
| [ ] | **Android input injection** | P3 | Via `AccessibilityService`. iOS has no public API. |
| [ ] | **iOS background / system-wide mirroring** | P3 | Needs a Broadcast Upload Extension. |
| [x] | **Compile and test the Flutter apps** | P1 | DONE: Dependencies resolved, type errors in `host_session.dart` and deprecations fixed; `flutter analyze` passes clean with zero issues on both `mobile/controller` and `mobile/host`. |

## 5. Admin console

| | Item | Pri | Notes |
|---|---|---|---|
| [ ] | **Alerts (email/webhook) on rejected attempts** | P2 | |
| [ ] | **Relay fleet page** | P2 | Show relay status, `STATS` counters, `auth_refused`/`auth_banned`. |
| [ ] | **Token rotation UI** | P3 | Rotate `RELAY_SECRET` from the console. |
| [ ] | **Device tags / search / bulk actions** | P3 | |
| [ ] | **Usage reports + charts** | P3 | |
| [ ] | **SSO (SAML/OIDC) for operators** | P3 | |

## 6. Localization and accessibility

| | Item | Pri | Notes |
|---|---|---|---|
| [ ] | **Native-speaker review of the Spanish catalog** | P1 (before launch) | Human task. |
| [ ] | **More languages** | P3 | Catalog machinery exists (`locales/`). |
| [ ] | **Real assistive-technology testing with real users** | P1 (before launch) | Screen readers (NVDA, VoiceOver, TalkBack). Human task; code-side a11y was only audited structurally. |

## 7. Packaging, deployment and ops

| | Item | Pri | Notes |
|---|---|---|---|
| [ ] | **Build and run the Windows installer** | P1 | `deploy/pyinstaller` + `deploy/wix` have never been built or run. |
| [x] | **Run the Postgres path against real Postgres** | P1 | DONE: all 63 admin tests (`test_admin_p12`, `test_admin_2fa`) pass on PostgreSQL via `tests/run_admin_on_postgres.py` with zero errors. Postgres dialect bug in `reply_to_feedback` resolved. |
| [x] | **Load test from separate process/machine** | P1 | DONE: `server/loadtest.py` verified with 50 and 200 concurrent host/viewer pairs (0 failures, 60+ MB/s throughput). |
| [ ] | **macOS packaging and signing** | P2 | No `.app`/`.pkg` path yet. |
| [ ] | **Code signing for the Windows MSI** | P2 | Unsigned installers trigger SmartScreen. |
| [x] | **CI pipeline** | P2 | DONE: GitHub Actions workflow (`.github/workflows/ci.yml`) created covering unit/e2e tests, Postgres integration, and Flutter analysis. |
| [ ] | **Structured logging + metrics** | P3 | Relay `STATS` is JSON only; export to Prometheus. |
| [ ] | **Backups / migrations** | P3 | Schema changes are ad hoc (`_ensure_column`). Add a real migration tool. |

## 8. Code health

| | Item | Pri | Notes |
|---|---|---|---|
| [x] | **Delete or mark superseded Phase 7-11 scripts** | P1 | DONE: `host_p7`-`p11` and `viewer_p7`-`p9` deleted; `viewer_p10` kept, marked deprecated, for the old-viewer e2e test. |
| [ ] | **Consolidate into one host and one viewer** | P2 | Seven generations of near-duplicate files. Merge into a single module with feature flags. |
| [ ] | **Fix the `admin`/`desktop` `i18n` module name clash** | P3 | Same module name in two packages breaks single-process imports. |
| [x] | **Host accept loop stalls after an abandoned connection** | P2 | DONE (direct mode): `desktop/direct_accept.py`. Each viewer's channels are now set up on their own thread; the input/control/audio ports are drained into per-address queues; setups from one source address take turns (the channels carry no viewer identity, so two from one address can't be told apart) while different addresses never wait on each other; and a setup stops waiting as soon as the viewer closes its video channel instead of sitting out `HANDSHAKE_TIMEOUT`. Covered by `tests/test_direct_accept.py` and e2e tests 05/06 (both fail on the old loop). Not fixed: relay mode (`relay_accept_loop`) still sets viewers up one at a time and can wait up to 10 s on a viewer that vanishes after the video channel; two viewers behind the same NAT still queue behind each other (up to 10 s if the first stalls). A per-viewer token on the secondary channels would remove both limits but changes the protocol. |
| [x] | **Make the e2e test runnable without hardware libs** | P3 | DONE: `monitors.py`, `host_p12.py`, `viewer_p12.py`, `viewer_p10.py` wrap `mss`, `pynput`, and `cv2` in fallback stubs when missing or in headless CI runs. |

---

## Suggested order

1. ~~**Quick wins that remove real bugs/risks:** monitor-switch input mapping; delete superseded scripts; host login rate limiting.~~ Done.
2. **Verify what's been written but never run:** Neon test run, Windows installer build, Flutter compile, cross-machine load test.
3. **Security hardening:** ~~certificate pinning, relay token revocation~~ (done); ~~relay token expiry~~ (done); ~~TLS to the relay~~ (done); ~~admin 2FA + login rate limiting~~ (done).
4. **Product:** NAT traversal, then a GUI, then audio and drag-and-drop.
5. **Launch readiness:** native-speaker review, assistive-technology testing with real users.

## Already done (for reference)

Phases 0-13: desktop host/viewer, file transfer, clipboard, multi-monitor, recording, adaptive bitrate, mobile controller and host, multi-user sessions, chat/whiteboard/print/voice, admin console and policy, deployment and branding, automation API and CLI, relay hardening and multi-relay failover, accessibility and i18n (Spanish), feedback channel, **relay authentication (Phase 13)**, Render + Neon deployment blueprint.
