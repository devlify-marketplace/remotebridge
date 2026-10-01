# Phase 13 - Relay authentication

**Problem.** The relay accepted any `REGISTER <id>` from anyone. Anybody could register a device ID they
didn't own (squatting it, or displacing the real host by registering it again), and nothing limited how
fast an attacker could try.

**What changed.** Wire protocol is backward compatible (an old client on an open relay is unchanged).

| Piece | Change |
|---|---|
| `server/relay.py` | Optional secret (`--secret` / `$RELAY_SECRET`; comma-separate several to rotate). With a secret, `REGISTER <name> <token>` must carry `token = HMAC-SHA256(secret, device_id)`. New `CHECK <name> <token>` -> `OK` / `ERROR unauthorized`. Per-address ban after repeated failures (`--auth-fail-limit`, `--auth-ban`). New STATS counters `auth_refused`, `auth_banned`. |
| `admin/server.py` | `GET /api/v1/relay-token` (Bearer report token) returns the device's own token, from `$RELAY_SECRET`. Only an enrolled device gets one, and only for its own ID. |
| `desktop/host_p12.py` | Fetches its token from the console (or `--relay-token` / `$REMOTEBRIDGE_RELAY_TOKEN`), verifies it with `CHECK` at startup and says plainly if a relay rejects it. |
| `server/relay_token.py` | Mint a token by hand for a host that doesn't use the console. |
| `mobile/host` | Optional relay-token field, sent on REGISTER (uncompiled - see below). |

**Why it stops squatting.** Device IDs are already unique per console (enrollment). The token binds a
relay registration to an enrolled ID, so a host that isn't enrolled - or is enrolled under a different ID -
cannot register someone else's. One token covers all four channels (`<id>-video/-input/-control/-audio`).

**Viewers are deliberately unauthenticated.** `CONNECT` needs no token; a viewer still has to get through
the host's TLS and its password/whitelist/approval. Gating viewers at the relay would need viewer accounts.

## Limits - read these
- **Tokens don't expire by default.** (Per-device revocation and optional expiry were added afterwards:
  see `server/README.md`. With `RELAY_TOKEN_TTL` set, a leaked token dies on its own - but a live session is
  not cut at expiry, and a host whose console is unreachable for longer than the token lives can't register.)
  Rotating the secret still works for everyone at once (`RELAY_SECRET=new,old` on the relay, then drop `old`).
- **A token is a bearer credential.** Anyone who sees it can register that one device ID. On a plain relay
  port it is sent in cleartext; give the relay a certificate (`--tls-cert`, see `server/README.md`) and have
  hosts use `tls://host:port`, so it never crosses the network readable.
- **The ban is per source address** - easy to evade with many addresses, and it can lock out several users
  behind one NAT. It limits guessing; the 256-bit token is what makes guessing impractical.
- A rejected `REGISTER` is closed silently (hosts expect raw bytes, never text), so a host that skipped the
  startup `CHECK` sees only "dropped our registration". The startup check exists to give the clear message.
- Not covered: authenticating the relay to the host (a rogue relay could still relay, though it can't read
  the TLS session), and relay-to-relay trust (relays share no state).

## Verified vs not
- 18 new tests (`tests/test_relay_auth.py`) on real loopback sockets + the real Flask pipeline: valid/invalid/
  missing tokens, cross-device tokens, squatter cannot displace the real host, `CHECK`, rotation, open-relay
  compatibility, ban and ban expiry, console<->relay token agreement, and console-issued token -> relay.
- The host wiring (`resolve_relay_token`) parses and is exercised only as far as the pieces it calls; the
  full host/viewer e2e test needs `pynput` and could not run in the build environment.
- The Dart changes were edited by hand and **not compiled** (no Flutter SDK).
