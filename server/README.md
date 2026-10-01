# server/ — Relay & Signaling Server

## What's built (Phase 1)
- `relay.py` — a minimal relay: a host registers under an ID
  (`REGISTER <id>`), a viewer connects to that ID (`CONNECT <id>`), and
  the relay pairs the two sockets and blindly forwards bytes between
  them. TLS is negotiated end-to-end between host and viewer on top of
  this, so the relay itself never sees plaintext.

  Run it with:
  ```bash
  python3 relay.py --port 6000
  ```
  It needs to be reachable by both the host and the viewer — for a
  first test, run it on the same machine as one of them, or on any
  small server/VPS you control.

## Not yet built (later phases)
- Real NAT traversal / direct P2P connection attempts before falling
  back to the relay (this phase always relays, even when a direct
  connection would work)
- Device ID persistence tied to an account (right now the ID is just
  whatever string you pass with `--id`; nothing stops two hosts from
  picking the same one)
- Account/auth service (Phase 2: passwords, 2FA, whitelisting)
- Admin console, usage reporting (Phase 9)

See `../docs/roadmap.md` for what happens in each phase.


## Phase 12
`relay.py` is hardened (dead-host reaping, CONNECT wait, limits, `PING`/`STATS`) - wire protocol
unchanged. `loadtest.py` measures it: `python3 loadtest.py --sessions 2000 --ramp 5`. Redundancy is
client-side (`desktop/relay_client.py`, `--relay a:6000,b:6000`). Numbers and limits:
`../docs/phases/PHASE_12_README.md`.


## Phase 13 - authentication
Start with a secret to require a per-device token on `REGISTER`:
`RELAY_SECRET=<long random> python3 relay.py` (or `--secret`). Hosts enrolled with the admin console fetch
their token automatically (set the same `RELAY_SECRET` on the console); for others,
`python3 relay_token.py --id <device-id>`. Without a secret the relay is open and warns at startup.
Details and limits: `../docs/phases/PHASE_13_README.md`.

## Short-lived tokens
A token can expire, so one that leaks stops working on its own. On the console set `RELAY_TOKEN_TTL`
(seconds, or `12h` / `7d`): hosts then get an expiring token and renew it by themselves at half its life.
For a host that doesn't use the console, `python3 relay_token.py --id <device-id> --ttl 30d` (it cannot be
renewed - mint a new one before it expires). The relay accepts expiring and original tokens alike until
you start it with `--require-expiry` (or `RELAY_REQUIRE_EXPIRY=1`), which refuses the non-expiring kind.

**Turn it on in this order**, because a host that predates this change cannot renew a token and would be
locked out when its first one expires: (1) upgrade the relay, (2) upgrade every host, (3) set
`RELAY_TOKEN_TTL` on the console, (4) once nothing uses the old kind, add `--require-expiry`.

What expiry does and doesn't do: after expiry the token can't register, and a registration made with it is
closed (`expired_dropped` in `STATS`; the host re-registers with a fresh token). A **live session is not
cut** at expiry - it was authorized while the token was good; revoke the device to end one. Relay and
console clocks must roughly agree (30 s of slack is allowed) - run NTP. If the console is down for longer
than the token lives, hosts stop being able to register until it is back, so pick a TTL comfortably longer
than the outage you'd tolerate (a day or more is typical).
The mobile host takes a token by hand, so it can't renew one: give it a token without `--ttl`, or re-enter it.

## Revoking a device (without rotating the secret)

A lost laptop or a departed employee's machine still holds a valid token. To cut it off without touching the
shared `RELAY_SECRET` (which would break every other device), revoke its ID. Two sources, usable together:

```bash
# a file, one device ID per line, '#' comments - re-read every 30 s
python3 relay.py --secret "$RELAY_SECRET" --revoked-file /etc/remotebridge/revoked.txt

# the admin console (Devices page -> Revoke); the relay polls it, authenticating with a credential
# derived from the same secret - nothing extra to provision
python3 relay.py --secret "$RELAY_SECRET" --revoked-url https://admin.example.org/api/v1/relay/revoked
```

Adding an ID cuts that device's waiting registrations **and any live session** immediately (at the next poll);
the device is refused when it retries, and its host prints a clear "REVOKED" message. To restore, remove the ID
(or empty the file, or click Restore). Notes:

- If a source can't be read (console down, file unreadable or deleted) the relay **keeps its last good list**: an
  outage never un-revokes anyone. Empty the file to clear it; deleting it does not.
- Revoked retries are not counted toward the address ban, so a revoked host behind a shared NAT can't get its
  colleagues banned.
- The console also stops issuing relay tokens to a revoked device.
- Revocation is only meaningful with authentication on (`--secret`): on an open relay a device can just register
  under a different ID.
- Polling is every `--revoked-poll` seconds (default 30), so revocation takes effect within that window.


## TLS to the relay

Without this, a device token (and a `CHECK` reply) crosses the network in cleartext. The video/input stream was
never exposed - that is TLS end-to-end between host and viewer - but a token is a bearer credential, so protect it:

```bash
# certificate for the relay's public name (Let's Encrypt, or your own CA)
python3 relay.py --secret "$RELAY_SECRET" --tls-cert fullchain.pem --tls-key privkey.pem   # TLS on 6443, plain on 6000
python3 relay.py ... --tls-cert fullchain.pem --tls-key privkey.pem --no-plain             # TLS only
# env equivalents: RELAY_TLS_CERT, RELAY_TLS_KEY, RELAY_TLS_PORT, RELAY_NO_PLAIN
```

Hosts and viewers opt in per relay, in the address: `--relay tls://relay.example.org:6443` (or a mixed list,
`tls://a:6443,b:6000`, for failover during a migration). The certificate is verified against that hostname with the
system trust store; for a private CA or a self-signed certificate pass `--relay-ca ca.pem` (or set
`REMOTEBRIDGE_RELAY_CA`). Admin-console enrolment, `relay_token.py` and `CHECK` all work unchanged.

How it works, and what it does not do:
- TLS covers only the **command phase** (`REGISTER`/`CONNECT`/`CHECK`/`PING`/`STATS`, the token and the replies).
  Once a connection is parked or paired, both ends send a TLS `close_notify` and carry on over plain TCP, because what
  follows is already the host<->viewer TLS stream and the relay only forwards it. (Nesting a second TLS session inside
  the first would cost CPU and bandwidth on every session for no gain, and would break the relay's cheap dead-host
  detection.) So this protects the token and the relay's replies, not the relay's view of *who talks to whom*: an
  observer can still see that a connection was paired and the traffic volume.
- A client of an old version cannot use the TLS port; the plaintext port stays up until you pass `--no-plain`.
  Roll out: relay with a certificate -> hosts/viewers to `tls://` -> `--no-plain`.
- A host's token can still be learned by an attacker who can intercept the connection **and** present a certificate
  your clients trust. Certificate verification is on and cannot be turned off from the command line.
- `STATS` gained `tls_connections` and `tls_failed` (a failed handshake or a refused downgrade). TLS failures do not
  count toward the per-address auth ban (they carry no token to guess).
- Not covered: the mobile apps still connect in plaintext (not yet taught `tls://`); `loadtest.py` is plaintext only;
  there is no certificate hot-reload (restart the relay after renewing); no client-certificate (mTLS) option.
