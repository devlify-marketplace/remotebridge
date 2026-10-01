# admin/ — Admin Console & Policy Server (Phase 9, +Deployment Phase 10, +Automation Phase 11)

## What's built
- `store.py` — SQLite persistence: operator accounts, groups (policy
  bundles), enrolled devices (+ `current_version` Phase 10, +`mac_address`/
  `last_seen_address` Phase 11), session history, org-wide branding/release
  settings (Phase 10), API keys, and one-time pending connections (Phase 11).
  No ORM; plain `sqlite3` with `Row` objects. `init_db` migrates an existing
  database from an earlier phase in place rather than requiring a fresh one.
- `wol.py` — builds and sends a Wake-on-LAN magic packet (Phase 11).
- `policy.py` — turns a group's DB row into the JSON policy dict a
  host actually enforces, `auto_update` (Phase 10) included.
- `server.py` — the Flask app: a session-cookie-authenticated web
  console (dashboard, devices, groups & policy, session history,
  operator accounts + API keys (Phase 11), deployment - branding +
  release, Phase 10) plus a JSON API under `/api/v1/` for hosts to
  enroll, fetch policy (now also reporting their own version and MAC,
  Phase 10/11), fetch branding (public, no token - useful pre-enrollment
  too) and the latest release (bearer), report session events, and
  (Phase 11) check a one-time pre-authorization. A separate
  `/api/v1/ops/` (API-key or logged-in-session authenticated, Phase 11)
  is for scripts/other systems: list devices, pull a device's sessions,
  wake it, or issue a one-time connection.
- `templates/`, `static/style.css` — the console's own pages; no
  external JS framework, no CDN dependency beyond nothing at all (the
  dashboard's "busiest devices" bars are plain CSS, not a charting
  library).

Run it with:
```bash
pip install -r requirements.txt
python3 server.py --port 8443
```
First run prompts once for the first operator account and prints a
one-time **org enrollment key** — save it, it's what a host's first
`--admin-url` run needs (see `../desktop/admin_client.py` and
`host_p10.py`). After that first enrollment, a host holds its own
`report_token` and never needs the enrollment key again.

## How a host connects to this
A host started with `--admin-url`, `--device-id`, and (on its first
run only) `--enrollment-key` will:
1. Enroll once (`POST /api/v1/enroll`), caching the returned
   `report_token` locally (`admin_enrollment.json` by default).
2. Fetch its group's effective policy at startup and again every
   5 minutes by default (`GET /api/v1/policy`), so a policy change
   here reaches a running host without restarting it. Since Phase 10,
   this same request reports the host's own build version, which is
   what populates the Devices page's Version column.
3. Report every connection attempt/start/end (`POST /api/v1/events`),
   best-effort and non-blocking - an unreachable console never stalls
   or breaks an actual remote session, it just means that session
   won't show up here until the console is back.
4. (Phase 10) Fetch org branding (`GET /api/v1/branding`, no token
   needed) to show in its own banners and to send to connecting
   viewers, and check the published release (`GET /api/v1/release`)
   against its own version - applying it automatically only if that
   host's group has the `auto_update` policy on.
5. (Phase 11) Report its own MAC address on that same policy fetch
   (`?mac_address=`), so Wake-on-LAN and the Devices page have
   something to show without anyone typing it in by hand. A host that
   receives a one-time `auto-` viewer_id checks it in real time via
   `GET /api/v1/preauth/<viewer_id>` rather than waiting for its next
   periodic policy refresh - see `PHASE_11_README.md`.

A host with no `--admin-url` never calls any of this and behaves
exactly like Phase 8. A scripted/MSI install (see `../deploy/`) can
skip typing any of these flags at all by bundling a `deploy_config.json`
next to the host - the Deployment page below generates that file's
contents pre-filled for copy-paste.

## Deployment page (Phase 10)
`/deployment` (operators with the `admin` role) is where an org sets:
- **Branding** - display name and support URL, shown in this console's
  own UI and sent to every viewer that connects to one of this org's
  hosts (`MSG_BRANDING`).
- **Latest release** - version, download URL, and SHA-256 checksum for
  whatever install package `../deploy/` produced. Publishing here is
  what a host with `auto_update` on is comparing itself against; it
  does nothing on its own to hosts whose group has `auto_update` off,
  beyond letting them print that an update exists.
- A ready-to-copy `deploy_config.json` snippet, pre-filled with this
  console's own URL and org enrollment key, for whichever of
  `../deploy/linux/install.sh` or `../deploy/wix/build.ps1` is building
  the next round of installs.

## Automation: REST API + CLI (Phase 11)
`/api/v1/ops/` is a separate, operator-scoped surface from everything
above - authenticated by an API key (create one on the Operators page;
shown once, only its hash is kept) or a logged-in browser session, not
a device's own `report_token`. It's what `../cli/remotebridge.py` talks to,
and what backs the Devices page's own Wake button:
- `GET devices` - list, with each one's group/version/MAC/last seen
- `GET devices/<id>/sessions` - that device's session history
- `POST devices/<id>/wake` - send it a Wake-on-LAN magic packet
  (needs a MAC on file; needs the console to share a broadcast domain
  with the device, or a router forwarding directed broadcasts - see
  `../cli/README.md`)
- `POST devices/<id>/connect` - mint a single-use `auto-<8 hex>` viewer
  ID (5-minute default lifetime) that the target host will accept
  without anyone approving anything at its console - the real-time
  check in step 5 above. Returns the device's last-seen address as a
  best-effort connection hint, not a guarantee.

## Sign-in protection: lockout and two-factor (TOTP)

**Lockout.** Failed sign-ins are counted per account name (whether or not it exists) and per client
address, in the database, so the count survives restarts and is shared by every gunicorn worker. The
5th failure in a row starts a 30 s lock that doubles with each further failure, up to 15 min; the
password is not even checked while locked (HTTP 429 with `Retry-After`). A full sign-in clears the
account's counter; the address's counter ages out after an hour. Wrong 2FA codes count too. Behind a
proxy, set `REMOTEBRIDGE_BEHIND_PROXY=1` or every visitor shares the proxy's address. Trade-off: anyone
who knows an operator's username can keep that account locked for up to 15 min at a time.

**2FA.** Each operator can turn on an authenticator-app code under *Account security* (any TOTP app).
After the password, the console asks for the 6-digit code (or a recovery code); until then nothing is
signed in. A code can be used once (replays are refused). Turning it on shows 8 single-use recovery
codes, once; only their hashes are stored. Turning it off needs the password and a current code. An
admin can reset another operator's 2FA from *Operators* (lost phone and codes). Set
`REMOTEBRIDGE_REQUIRE_2FA=1` to force every operator to set it up before reaching anything else.

Not done: the TOTP secret is stored in the database unencrypted (as the host's is); no QR code (the
key is shown for manual entry, plus an `otpauth://` link); recovery codes can't be regenerated
without turning 2FA off and on; no email/webhook alert on lockouts (they go to the log).

## Not yet built (later phases)
- A device-level policy override on top of its group (right now, one
  device → one group → one policy; no per-device exceptions), and
  likewise one org-wide branding/release/Wake-on-LAN-broadcast-target
  rather than per-group ones
- Real TLS termination / rate limiting in front of this - Flask's
  built-in server is fine for a LAN or behind your own reverse proxy,
  not for exposing directly to the public internet
- Tying `viewer_id` to real accounts/SSO - it's still just the
  self-declared string it's always been (Phase 2); this console adds
  visibility and policy around it, not an identity system for it
- A true Windows-service wrapper / SCM integration for the installed
  host (see `../deploy/README.md`) - what Phase 10 ships is a launchable
  app (Start Menu shortcut on Windows, a systemd unit on Linux), and the
  Linux side already runs headlessly fine; Windows' equivalent today is
  "point Task Scheduler at the exe yourself," not something the MSI sets
  up automatically yet
- A headless capture client for `connect --launch` - it still opens
  `viewer_p10.py`'s normal video window (see `../cli/README.md`)

See `../docs/roadmap.md` (Phases 9-11) for scope, and
`../docs/phases/PHASE_9_README.md` / `PHASE_10_README.md` /
`PHASE_11_README.md` for what changed in `desktop/` to make a host obey
and report what this console says - Phase 11's write-up in particular
covers a real auth deadlock bug in Phases 7-10 that got fixed alongside
this phase's own additions.


## Phase 12 additions
- **Feedback inbox** (`/feedback`): tickets from hosts and their viewers; admins reply/resolve,
  auditors read. API: `POST/GET /api/v1/feedback` (device token), `GET /api/v1/ops/feedback` (API key).
- **Accessibility + localization:** language picker and high-contrast toggle in the sidebar;
  catalogs in `locales/`. **CSRF tokens** on every browser POST form; `/logout` is now POST.
See `../docs/phases/PHASE_12_README.md`.

## Revoking a device's relay access

Devices page -> **Revoke** (admins only). The console stops issuing that device a relay token
(`GET /api/v1/relay-token` returns 403) and lists it at `GET /api/v1/relay/revoked`, which relays poll with
`--revoked-url` (authenticated with a credential derived from `RELAY_SECRET`; the endpoint is unavailable when
no secret is set). **Restore** reverses it. See `server/README.md` for how relays apply it.
