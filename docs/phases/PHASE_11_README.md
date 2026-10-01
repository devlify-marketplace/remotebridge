# Phase 11: Automation & Remote Ops — Implementation

Builds on Phase 9's admin console and Phase 10's per-group policy - Wake-on-LAN
and one-time connections are both driven by the same console a host is already
talking to, and "may this host auto-update" becomes "may this host be woken /
connected to unattended" following the exact same per-group policy pattern.
Nothing changes for a host with no `--admin-url`: it behaves exactly like
Phase 10.

**Also fixes a real bug in Phases 7-10** - see below before anything else.
It doesn't touch the wire protocol, so it's a pure bug fix, not a new phase
of its own, but it's the most important thing in this batch of changes.

## The bug: every multi-user host has been deadlocking on real connections

`host_p7.py` through `host_p10.py` all do this in `accept_viewer`:

```python
msg_type, payload = proto.recv_message(video_conn)   # read the auth request themselves...
auth_req = proto.unpack_auth_request_p7(payload)      # ...to get view_mode, which Phase 2's
...                                                    # wire format doesn't carry
result = auth.perform_host_auth(video_conn, config, peer_addr)   # ...then hand the same
                                                                   # socket to a function that
                                                                   # tries to read ANOTHER
                                                                   # auth request from it
```

A real viewer sends exactly one `MSG_AUTH_REQUEST` and then blocks waiting for
the response. `perform_host_auth`'s second `recv_message` call therefore blocks
forever on a live socket - not an error, a hang, on every single connection
attempt, for every phase from 7 onward. **This was never caught before now
because every accept_viewer test in the Phase 9 and 10 write-ups used a fake
socket** whose `recv()` returns `b""` once its buffer is empty rather than
blocking - `_recv_exact` treats that as "connection closed," which surfaced as
a `protocol_error`/`malformed auth request` decision. That result satisfied
tests that only checked "is the connection accepted or rejected," which is
exactly why it looked fine through two phases of otherwise-thorough testing.
Phase 11's tests use real `socket.socketpair()`s instead, specifically
because a fake socket that can't block can prove logic correct without ever
proving the code doesn't hang.
`host.py` (the original Phase 0-4 single-viewer host) was checked and is
**not** affected - it never pre-reads the request, so `perform_host_auth` is
its only reader, which is exactly the shape it expects.

The fix, in `desktop/auth.py`: `perform_host_auth` (still exactly what it
was, still what `host.py` calls) is now built on top of a new
`decide_host_auth(viewer_id, password, totp_code, config, addr,
preauth_check=None)` - the same decision tree, taking already-parsed fields
instead of reading them off a socket. `host_p7.py` through `host_p11.py` now
call `decide_host_auth` with the fields they already parsed. `host_p11.py`
also passes `preauth_check` - see below - the other four don't, so nothing
about their approve/reject behavior changes, only that they no longer hang.

`host_p7.py` had a second, unrelated bug fixed alongside this one: its
`accept_viewer`/control-thread wiring never had real per-session
`clipboard`/`file_session`/`bitrate`/`monitor_host` objects to pass -
`run()` built them and then didn't forward them, so the control-channel
thread crashed with a `NameError` the moment a control message arrived.
Fixed the same way Phase 8 already does it.

## Files

| File | Purpose |
|------|---------|
| **admin/wol.py** | Builds and sends a Wake-on-LAN magic packet |
| **admin/store.py**, **admin/policy.py** | +`mac_address`/`last_seen_address` on devices, +`api_keys`, +`pending_connections` (one-time preauth), +schema migration for both |
| **admin/server.py** | +`/api/v1/ops/` (API-key or session authenticated): list devices, pull a device's sessions, wake, issue a one-time connection. +device-authenticated `GET /api/v1/preauth/<viewer_id>`. +`/api/v1/policy` now also accepts `mac_address`. +a Wake button and API-key management in the web console |
| **desktop/auth.py** | +`decide_host_auth` (see the bug fix above); `perform_host_auth` unchanged |
| **desktop/admin_client.py** | +`get_own_mac_address`, +`check_preauth` (fails closed, never raises) |
| **desktop/host_p11.py** | Reports its MAC alongside its version; passes a `preauth_check` hook into `decide_host_auth` so a one-time `auto-` viewer_id never has to wait on a human at the console |
| **cli/remotebridge.py**, **cli/README.md** | Standard-library CLI: `devices`, `sessions`, `wake [--wait]`, `wait`, `connect [--launch]` against `/api/v1/ops/` |

No new `protocol_p11.py` - nothing here touches the video/input/control/audio
wire protocol; it's all the admin-console side plus one host-side auth hook.
`viewer_p10.py` is used as-is for automated connections (an `auto-` ID with no
password is already valid input to the existing protocol).

## What each roadmap item became

- **REST API for triggering connections and pulling session data** —
  `/api/v1/ops/devices` (list) and `/api/v1/ops/devices/<id>/sessions` (pull)
  cover the "pulling session data" half directly, API-key or logged-in-session
  authenticated (the Devices page's own Wake button calls the identical wake
  logic a script does - one implementation, two ways in). "Triggering a
  connection" is `POST .../connect`: it mints a single-use `auto-<8 hex>`
  viewer_id (default 5-minute lifetime), which a host recognizes via
  `GET /api/v1/preauth/<viewer_id>` at the exact moment that ID tries to
  connect - fully real-time, not on the 5-minute policy-refresh cadence. The
  API deliberately never opens a video connection itself - "trigger" means
  hand back something a real viewer can use, not embed a capture client in
  the admin console.
- **CLI for scripted/unattended tasks** — `cli/remotebridge.py`, a thin wrapper
  around the same REST API (nothing it does isn't also reachable via a raw
  HTTP call). `wake --wait` and `wait` poll the console's own `last_seen_at`
  for that device rather than trying to ping the machine directly, so they
  work the same way regardless of network topology between the CLI and the
  target.
- **Wake-on-LAN support** — `admin/wol.py`, called from both the web UI and
  the REST API. The one real constraint: a magic packet is a LAN broadcast,
  so whatever sends it has to share a broadcast domain with the target (or
  have a router forwarding directed broadcasts) - true of any WoL tool, not
  a limitation of this one. A Phase 11 host reports its own MAC automatically;
  an older host needs one typed in on the Devices page.

## Running it

```bash
# Admin console (adjust --wol-broadcast if it isn't on the target's own LAN)
cd admin && python3 server.py --port 8443

# Create an API key on the console's Operators page, then:
cd ../cli
export REMOTEBRIDGE_ADMIN_URL=http://localhost:8443 REMOTEBRIDGE_API_KEY=dvfy_...
./remotebridge.py wake front-desk --wait
ID=$(./remotebridge.py connect front-desk --host-address 192.168.1.50)
python3 ../desktop/viewer_p10.py 192.168.1.50:5000 --id "$ID"   # no --password

# Host, reporting itself so Wake-on-LAN and the Devices page have something to show
cd ../desktop
python3 host_p11.py --video-port 5000 --input-port 5001 --control-port 5002 --audio-port 5003 \
                     --admin-url http://localhost:8443 --device-id front-desk --enrollment-key <org key>
```

## Notes / follow-ups for a later pass

- **One org, one Wake-on-LAN broadcast target.** `--wol-broadcast` is a
  single console-wide setting; a console managing devices across several
  separate LANs would need one console instance per LAN, or a WoL relay
  running on each - not something this phase builds.
- **`connect`'s suggested address is a best-effort hint** (where the device
  last reached the console *from*), not a guaranteed reachable one - behind
  NAT or on a multi-homed host it can easily be wrong. `--host-address` is
  there for exactly that case.
- **No headless capture client.** `connect --launch` still opens
  `viewer_p10.py`'s normal video window; a machine truly running a script
  with no display needs something like `xvfb-run`, or a proper headless
  client, which doesn't exist yet.
- **Single-use, not single-attempt.** A one-time ID is consumed the moment a
  host checks it - successfully or not doesn't matter, it's gone either way.
  A `connect` call that a script never gets around to using just expires
  quietly after its TTL.
