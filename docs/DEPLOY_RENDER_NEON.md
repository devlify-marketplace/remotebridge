# Deploying RemoteBridge on Render + Neon (free tier)

## What goes where

| Piece | Where | Why |
|---|---|---|
| Admin console + policy API (`admin/`) | **Render** free web service (`render.yaml`) | Plain HTTPS - fits Render |
| Database | **Neon** free Postgres | Render's free disk is ephemeral; Neon keeps the data |
| Relay (`server/relay.py`) | **Not Render** - a VPS / Fly.io / anything with a public TCP port (`server/Dockerfile`) | It speaks raw TCP; Render's free web services only expose HTTP(S)/WebSocket |
| Hosts / viewers | Users' machines | Point at the two URLs above |

Hosts and viewers only need the relay when they can't reach each other directly; the admin console
is optional for a bare host, but it's what enforces policy and stores session history.

## Steps

1. **Neon** - create a project. *Connect* -> pick the **pooled** connection string (host contains
   `-pooler`). Keep `?sslmode=require`.
2. **GitHub** - push this repo (`.gitignore` already excludes databases, certs and local configs).
3. **Render** - *New* -> *Blueprint* -> select the repo. It reads `render.yaml`. When prompted, set:
   - `DATABASE_URL` = the Neon string
   - `ADMIN_PASSWORD` = the first operator's password (username defaults to `admin`)
4. After the first deploy, open the service's *Environment* tab and copy `ORG_ENROLLMENT_KEY`
   (Render generated it). Hosts need it once, to enroll.
5. Open `https://<your-service>.onrender.com`, log in, change nothing else yet.
6. On each host:
   ```bash
   export REMOTEBRIDGE_ADMIN_TIMEOUT=60     # tolerate Render's cold start (see below)
   python3 desktop/host_p12.py --admin-url https://<your-service>.onrender.com \
       --enrollment-key <ORG_ENROLLMENT_KEY> --relay <your-relay-host>:6000
   ```
   (check `python3 desktop/host_p12.py --help` for the exact flag names in your version.)

## Relay authentication (do this before exposing the relay)

Render generated `RELAY_SECRET` for the console (Environment tab). Start the relay with the **same** value:

```bash
docker build -t remotebridge-relay server && docker run -d -p 6000:6000 -e RELAY_SECRET=<value> remotebridge-relay
```

Enrolled hosts then fetch their own token from the console automatically and verify it at startup; no
extra flags. A relay started *without* `RELAY_SECRET` is open to anyone and prints a warning. Limits
(bearer credential, cleartext to the relay): `docs/phases/PHASE_13_README.md`.

## Free-tier behavior to expect

- **Cold starts.** A free Render web service sleeps after a period with no traffic and takes
  on the order of a minute to wake. Hosts already fall back to cached/default policy when the console
  is unreachable; `REMOTEBRIDGE_ADMIN_TIMEOUT` just gives the first request time to succeed.
- **Neon scales to zero** when idle, so the first query after a quiet spell is slower; the
  connection layer retries. `/healthz` intentionally never queries the database - don't point an
  uptime pinger at a page that does, or the database will never sleep.
- **Storage is small.** Session history is the only table that grows; it's trimmed on every start
  to `SESSION_EVENT_RETENTION_DAYS` (default 90; `0` keeps everything).
- **Wake-on-LAN from the console won't reach your LAN** - a cloud server can't broadcast into your
  network. It needs something on the LAN to send the packet.
- Free-tier limits and sleep timings change; check Render's and Neon's current pricing pages.

## Configuration reference

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | `postgresql://...` -> Postgres; unset -> local `admin.db` (SQLite) |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Creates the first operator if none exists |
| `ORG_ENROLLMENT_KEY` | Key hosts present to enroll (synced into the database on boot) |
| `SECRET_KEY` | Flask session signing key (else generated and stored in the database) |
| `REMOTEBRIDGE_BEHIND_PROXY=1` | Trust Render's `X-Forwarded-*` headers |
| `REMOTEBRIDGE_SECURE_COOKIES=1` | Mark the session cookie `Secure` |
| `SESSION_EVENT_RETENTION_DAYS` | Session-history retention (default 90) |
| `RELAY_SECRET` | Shared with the relay; the console mints each device's relay token from it |
| `RELAY_TOKEN_TTL` | Optional. Makes relay tokens expire (`12h`, `7d`, or seconds); hosts renew them automatically. Upgrade the relay and hosts first - see `server/README.md` |
| `REMOTEBRIDGE_ADMIN_TIMEOUT` | (hosts) minimum seconds to wait on the console |

Local runs: `python3 server.py` (SQLite, interactive first-run prompt), or export the variables above
and run `gunicorn wsgi:app` from `admin/` (same entry point Render uses; works against SQLite or Neon).

## What was and wasn't verified

- The admin test-suite (35 tests) and the wsgi env-driven bootstrap pass against **SQLite**.
- The **Postgres path has not been run against a real Postgres/Neon** (no database or network in the
  build environment). The SQL translation layer (`admin/dbcompat.py`) was checked against a
  recording stub - placeholders, `SERIAL` keys, `RETURNING id`, `ADD COLUMN IF NOT EXISTS` - not a server.
  Expect to fix a query or two on first contact, and run the admin tests once against a scratch
  Neon branch before relying on it.
- `render.yaml` hasn't been deployed to Render.
