# Phase 10: Deployment & Branding — Implementation

Builds on Phase 9's admin console rather than adding a new service - branding
and release info live in the same `admin/server.py`, using the same settings
table and the same bearer-token API a host already talks to for policy.
Nothing changes for a host with no `--admin-url` (or no `deploy_config.json`
- see below): it behaves exactly like Phase 9, which behaved exactly like
Phase 8.

## Files

| File | Purpose |
|------|---------|
| **admin/store.py**, **admin/policy.py** | +`auto_update` policy column, +branding/release settings, +per-device `current_version`, +a schema-migration helper so an existing Phase 9 `admin.db` upgrades in place |
| **admin/server.py** | +`/deployment` page (branding + release publishing), +public `GET /api/v1/branding`, +bearer `GET /api/v1/release`, `GET /api/v1/policy` now accepts `?client_version=` |
| **admin/templates/deployment.html** | Branding form, release form, and a ready-to-copy `deploy_config.json` snippet for IT |
| **desktop/protocol_p10.py** | One new message, `MSG_BRANDING` |
| **desktop/updater.py** | Version compare, download, checksum verify, and (OS-specific) apply-and-relaunch |
| **desktop/admin_client.py** | +`fetch_branding` (public, falls back to a default rather than erroring), +`fetch_release` (bearer), +`load_deploy_config` |
| **desktop/host_p10.py** | Reads `deploy_config.json` for admin/enrollment defaults; sends branding to each viewer; runs a background update-check loop gated on the group's `auto_update` policy |
| **desktop/viewer_p10.py** | Retitles its window (and prints a one-line banner) on receiving `MSG_BRANDING` |
| **deploy/** | `linux/install.sh` (systemd, actually run here), `pyinstaller/host.spec` + `wix/*` (Windows MSI, written but not run here - see `deploy/README.md`) |

## What each roadmap item became

- **Scripted/MSI deployment for IT rollout** — `deploy/linux/install.sh` for
  Linux (a venv + optional systemd unit; genuinely exercised in this
  project's own environment) and `deploy/pyinstaller` + `deploy/wix` for
  Windows (PyInstaller + WiX Toolset; written correctly against both
  tools' documented interfaces but not compiled here - neither tool, nor
  network to fetch them, was available). Either way, the payload is the
  same `deploy_config.json` (see `deploy/deploy_config.template.json`):
  drop it next to the host and every machine enrolls with the org's
  admin console with nobody typing `--admin-url`/`--enrollment-key` by
  hand. The admin console's new Deployment page even generates that
  snippet pre-filled, so there's nothing to look up manually.
- **Auto-update mechanism for all clients** — an org publishes a version/
  URL/checksum once, in the admin console (Deployment page); every
  enrolled host compares that against its own `updater.CURRENT_VERSION`
  on the same cadence as its policy refresh. Whether a host *applies* an
  available update automatically or just prints that one's available is
  a per-group policy toggle (`auto_update`), not a separate setting -
  consistent with everything else Phase 9 already made group-configurable.
  Download and checksum verification are plain, tested Python; actually
  replacing a running executable is inherently OS-specific (see
  `updater.py`'s docstring for exactly what was and wasn't exercised).
- **Custom branding / white-label options** — an org's display name and
  support URL, set once on the admin console's Deployment page, show up
  in two places: the console's own UI (sidebar, page titles, login page)
  and, via the new `MSG_BRANDING` control-channel message, a connecting
  viewer's window title and a one-line "connected via \<org\>" banner.
  Deliberately left out: an accent/theme color - this app's only real UI
  surface today is an OpenCV video window with no themable chrome to
  paint it onto (see the root README's Phase 4 notes on the GUI
  toolkit gap), so that field would have been decorative rather than
  something with anywhere real to go.

## Running it

```bash
# Publish branding + (optionally) a release, from the admin console's
# Deployment page - or seed them directly for a quick test:
python3 -c "
import store
conn = store.init_db('admin.db')
store.set_branding(conn, 'Acme Corp Remote', 'https://support.acme.example')
store.set_release(conn, '10.0.1', 'https://dl.acme.example/host.msi', '<sha256>', 'notes')
"

# Host: deploy_config.json (optional) + normal Phase 9 flags
cd desktop
python3 host_p10.py --video-port 5000 --input-port 5001 \
                     --control-port 5002 --audio-port 5003 \
                     --admin-url http://admin.example.org:8443 \
                     --device-id front-desk-pc --enrollment-key <org key>

# Or a scripted install that needs no flags at all on each machine:
cp ../deploy/deploy_config.template.json ./deploy_config.json  # then fill it in
python3 host_p10.py --video-port 5000 --input-port 5001 --control-port 5002 --audio-port 5003

# Viewer - unchanged from Phase 9 except it now shows the host org's branding
python3 viewer_p10.py 192.168.1.100:5000 --id alice --password mypassword
```

## Notes / follow-ups for a later pass

- **No true Windows service, no true systemd-independent daemon-of-record
  on either OS** - what's here is "an app that runs happily with no TTY"
  (already true because `console.py`'s interactive prompt exits cleanly
  on `EOFError`), packaged with a launcher (Start Menu shortcut / systemd
  unit). A real Windows SCM service wrapper is a reasonable next step if
  centralized start/stop/restart from `services.msc` matters; today
  that's Task Scheduler's job, one level up from what this phase ships.
- **MSI upgrade path and in-app self-update are two different
  mechanisms that happen to produce the same result.** IT re-pushing a
  newer MSI via SCCM/Intune goes through Windows Installer's own
  major-upgrade handling; a host with `auto_update` on does its own
  thing entirely (download, verify, swap the .exe, relaunch) without
  Windows Installer involved at all. Neither knows about the other;
  both just end up with the same `RemoteBridgeHost.exe` in place.
- **One org, one branding/release.** Like Phase 9's policy model, there's
  no per-group branding or staged/canary releases (e.g. "group A gets
  10.0.1 first") - every enrolled device sees the same branding and the
  same published release, gated only by whether *that device's group*
  auto-applies it.
- **A host that's never reachable by the console never gets told about
  branding or an update**, the same fire-and-forget characteristic
  Phase 9's session reporting already has - it just keeps running on
  whatever it last knew (or Phase 8-identical defaults, if it's never
  reached the console at all).
