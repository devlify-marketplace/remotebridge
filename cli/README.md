# cli/ — Command-line client (Phase 11)

`remotebridge.py` drives the admin console's REST API from a shell script, cron
job, or another system - no browser, no one clicking through the web UI.
Standard library only; nothing to `pip install`.

```bash
export REMOTEBRIDGE_ADMIN_URL=http://admin.example.org:8443
export REMOTEBRIDGE_API_KEY=dvfy_...          # create one on the console's Operators page

./remotebridge.py devices                     # what's enrolled, versions, MACs, last seen
./remotebridge.py sessions front-desk         # pull a device's session history
./remotebridge.py wake front-desk --wait      # Wake-on-LAN, then block until it checks in
./remotebridge.py connect front-desk          # prints a one-time viewer ID on stdout
./remotebridge.py connect front-desk --launch # ...and runs viewer_p12.py with it
```

`--json` on `devices`, `sessions`, `connect` (and `wake`) gives machine-readable
output; without it you get a table. In `connect`, stdout is *only* the viewer ID
so `ID=$(./remotebridge.py connect front-desk --host-address 10.0.0.5)` just works;
human-readable notes go to stderr.

## A whole unattended run

```bash
./remotebridge.py wake conference-room-pc --wait --timeout 180 || exit 1
ID=$(./remotebridge.py connect conference-room-pc --host-address 10.0.0.42) || exit 1
python3 desktop/viewer_p12.py 10.0.0.42:5000 --id "$ID"       # no --password
./remotebridge.py sessions conference-room-pc --json > audit.json
```

## Configuration

Highest priority first, so a secret never has to sit on a command line (where it
shows up in shell history and `ps`):

1. `--admin-url` / `--api-key` (accepted before *or* after the subcommand)
2. `REMOTEBRIDGE_ADMIN_URL` / `REMOTEBRIDGE_API_KEY`
3. a JSON file - `$REMOTEBRIDGE_CONFIG`, else `~/.remotebridge/cli_config.json`:
   `{"admin_url": "http://...", "api_key": "dvfy_..."}`

## Exit codes

| | |
|-|-|
| 0 | success |
| 1 | the admin console rejected the request, or couldn't be reached |
| 2 | usage / configuration problem (nothing was sent) |
| 3 | `wait` / `wake --wait` timed out |

`connect --launch` returns the viewer's own exit code.

## What this does and doesn't do

- **`wake`** asks the admin console to send the magic packet. Wake-on-LAN is a
  LAN broadcast, so the console has to be on the same network as the target (or
  your router must forward directed broadcasts) - see `admin/README.md` and
  `admin/server.py --wol-broadcast`. The console needs the device's MAC: a Phase
  11 host reports it itself; for an older host, type it in on the Devices page.
- **`wake --wait` / `wait`** watch for the device's *next check-in* with the
  console, which a host does the moment it starts. Both timestamps come from the
  console, so clock differences between machines don't matter. It only knows the
  host checked in, not that it's ready for a session.
- **`connect`** issues a single-use ID (valid 5 minutes by default) that the
  *host* accepts without anyone approving anything at its console, and without a
  shared password living in your script. "Unattended" means unattended **at the
  host**. `viewer_p12.py` still opens a video window, so the machine running it
  needs a display (or something like `xvfb-run`); there is no headless capture
  client yet.
- The address `connect` suggests is where the device last reached the console
  *from* - a hint, and often wrong behind NAT. Pass `--host-address` when you
  know better. Host and viewer still connect directly; there's no relay path.
