# Phase 9: Admin Console & Policy — Implementation

Builds directly on Phase 8 (collaboration & support tools). Nothing about
Phase 8's video/input/clipboard/file/chat/whiteboard/print/voice handling
changes when Phase 9 isn't configured - a host started with no `--admin-url`
behaves identically to Phase 8. What Phase 9 adds is a central place
(`admin/`) that can see every enrolled host's session activity and push
down a policy each host actually enforces, plus the host-side plumbing
that fetches and obeys that policy.

## Files

| File | Purpose |
|------|---------|
| **admin/store.py** | SQLite persistence: operator accounts, groups (policy bundles), enrolled devices, session history |
| **admin/policy.py** | Turns a group's DB row into the JSON policy dict a host enforces |
| **admin/server.py** | Flask app: web console (dashboard, devices, groups, session history, operators) + `/api/v1/` JSON API for hosts |
| **admin/templates/**, **admin/static/style.css** | The console's own pages |
| **desktop/protocol_p9.py** | One new message, `MSG_POLICY_DENIED` - host tells a viewer why an action didn't happen |
| **desktop/admin_client.py** | Host-side HTTP client: enrollment (cached locally), policy fetch/refresh, best-effort session reporting |
| **desktop/host_p9.py** | Host: enforces the fetched policy at connection time and at every gated action; reports attempt/start/end to the console |
| **desktop/viewer_p9.py** | Viewer: prints the reason when the host sends `MSG_POLICY_DENIED`, instead of an action just silently not happening |

## What each roadmap item became

- **Central admin console (users, devices, permissions)** — `admin/server.py`'s
  web console. "Users" here are console operators (who can sign in to
  the dashboard - role `admin` or read-only `auditor`), not remote-session
  viewers, which are still the same self-declared `viewer_id` strings
  Phase 2 introduced. "Devices" are hosts that have enrolled via
  `--admin-url`; each belongs to exactly one group, and the Devices page
  is where an admin moves one to a different group or renames it.
- **Group policies and enforced settings** — a group's settings (Groups
  page) are the effective policy for every device in it: turn off
  unattended access, file transfer, clipboard, chat, whiteboard,
  printing, or voice individually; force every viewer into view-only;
  cap simultaneous viewers or session length in minutes; and layer an
  org-wide viewer-ID allow/block list on top of (not instead of) each
  host's own local `auth.py` whitelist. A host fetches its group's
  policy at startup and re-fetches every 5 minutes by default
  (`--policy-refresh-seconds`), so a change here reaches a *running*
  host without restarting it. Enforcement lives entirely on the host
  (`host_p9.py`) - the console hands down settings, it doesn't sit in
  the data path.
- **Usage reporting and session history dashboard** — every
  attempt/start/end a host already wrote to its local `session_log`
  also gets reported to the console (best-effort, non-blocking - an
  unreachable console never stalls a real session). The Dashboard page
  shows enrolled-device and session counts, a 7-day busiest-devices
  view, and recent activity; the Sessions page is the full history,
  filterable by device, event type, and time range.

## Running it

```bash
# Admin console (once, anywhere reachable by your hosts)
cd admin
pip install -r requirements.txt
python3 server.py --port 8443
# -> first run prompts for an operator account and prints a one-time
#    org enrollment key; save both

# Host, enrolling with that console
cd desktop
python3 host_p9.py --video-port 5000 --input-port 5001 \
                    --control-port 5002 --audio-port 5003 \
                    --admin-url http://admin.example.org:8443 \
                    --device-id front-desk-pc --enrollment-key <the key from above>

# Host with no admin console at all - identical to Phase 8
python3 host_p9.py --video-port 5000 --input-port 5001 \
                    --control-port 5002 --audio-port 5003

# Viewer - unchanged from Phase 8 except it now understands policy notices
python3 viewer_p9.py 192.168.1.100:5000 --id alice --password mypassword
```

New dependency: `pip install flask` for the admin console only
(`admin/requirements.txt`). `desktop/admin_client.py` uses nothing beyond
the standard library, so a host gains no new dependency whether or not
`--admin-url` is used.

## Notes / follow-ups for a later pass

- **One device, one group, one policy.** There's no per-device override
  layer on top of a group yet - if one host in a group genuinely needs
  an exception, today that means giving it its own group. Keeps "why is
  this host behaving this way" a one-hop question for now; a richer
  override model is a reasonable next step if a real deployment needs it.
- **`viewer_id` is still just a self-declared string** (Phase 2). Phase 9
  adds visibility and policy around it - org allow/block lists, showing
  up in session history - but not an identity system for it; tying it to
  real accounts or SSO is future work, not this phase.
- **Flask's built-in server, not a production one.** Fine for a LAN or
  behind your own TLS-terminating reverse proxy, same spirit as
  `relay.py` being a "dumb, always-works" fallback rather than an
  optimized one - don't expose `admin/server.py` directly to the public
  internet without real TLS and rate limiting in front of it.
- **Session reporting is fire-and-forget.** A host that's never reachable
  by the console (or vice versa) just never shows up in session
  history; there's no local queue-and-retry for events missed while
  the console was down. For most deployments the console is the
  steadier of the two processes, so this cuts the right way, but a
  spotty link to the console would lose history entries.
- **While making per-viewer "end" events reportable, this phase also
  fixes them locally.** Phase 7/8's `accept_viewer` only logged an
  "end" event when the *entire* multi-user session shut down (Ctrl+C
  on the host), using the session's overall duration - a viewer
  disconnecting on their own while others stayed connected was never
  logged as "end" at all. `host_p9.py` now logs (and reports) "end" the
  moment that specific viewer disconnects, with their own connected
  duration. Harmless for a single viewer; needed for Phase 9's session
  history to be accurate once several viewers share a session.
