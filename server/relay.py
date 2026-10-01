"""
Relay server (Phase 1; hardened for scale in Phase 12).

A byte-forwarding relay so a host and viewer can connect using an ID
instead of a raw IP address, and so they can reach each other even when
they're on different networks (as long as both can reach this relay).

Wire protocol (text, newline-terminated, ASCII) - UNCHANGED from Phase 1,
so every existing host/viewer keeps working against this relay:
  Host sends:   REGISTER <name>\\n      (no reply - the host's next bytes
                                         are whatever the viewer sends)
  Viewer sends: CONNECT <name>\\n       (reply: OK\\n, or ERROR <reason>\\n)
Phase 13 (relay authentication) adds an OPTIONAL token to REGISTER/CHECK. When the relay is
started with a secret (--secret or $RELAY_SECRET), a host must prove it owns its device ID:
  Host sends:   REGISTER <name> <token>\\n   (token = hmac_sha256(secret, device_id), hex;
                                         <name> is the device ID or "<id>-video|input|control|audio")
  Host sends:   CHECK <name> <token>\\n      (reply: OK\\n or ERROR unauthorized\\n - lets a
                                         host verify its token at startup with a clear message)
The admin console hands each enrolled device its token (GET /api/v1/relay-token), so nobody who
isn't enrolled can register - or squat - a device ID. CONNECT (viewers) stays unauthenticated:
a viewer still has to get past the host's own TLS + password/whitelist. Without a secret the relay
behaves exactly as before and ignores any token. A bad REGISTER is closed silently (the host
expects raw bytes next, never text). Repeated failures from one address are temporarily banned.
Revocation: a device whose token must stop working (lost laptop, departed employee) can be cut off
without rotating the shared secret that every other device depends on. List its ID in --revoked-file
(one per line) and/or have the admin console publish it (Devices page -> Revoke; the relay polls
--revoked-url). The relay re-reads both every --revoked-poll seconds (30 by default). Newly revoked
devices lose their waiting registrations AND any live session at once; a revoked host that retries gets
the connection closed (CHECK answers "ERROR revoked" to a caller holding a valid token). Each source
keeps its last good list if it can't be read, so an outage never un-revokes anyone.
Expiry: a token can carry an expiry, "<unix-seconds>.<hmac_sha256(secret, device_id + expiry)>", so a
leaked token stops working on its own. Such a token is refused once it is past its expiry (30 s of
leeway for clock drift), and a waiting registration made with one is closed when it expires (the host
re-registers with a fresh token); a live session is not cut - use revocation for that. The original
non-expiring tokens keep working unless the relay is started with --require-expiry. CHECK answers
"ERROR expired" to a caller whose token has a genuine signature. Expired tokens do not count toward
the address ban (they are not a guessing attempt).
Phase 12 adds two operator commands, answered on the same port:
  PING\\n   -> PONG\\n            (load-balancer / monitoring health check)
  STATS\\n  -> one JSON line     (counters; loopback callers only unless
                                 --stats-open is given)

Once a REGISTER and a matching CONNECT meet, the relay pipes raw bytes
both ways until either side closes. Everything after pairing is opaque -
host and viewer negotiate TLS end-to-end on top of it.

What Phase 12 changed (the wire protocol did not):
  * A registered host that goes away (crash, network drop) is noticed and
    dropped by a reaper thread; before, its dead socket sat in the table
    forever and a viewer that CONNECTed would be paired with a corpse.
  * A second REGISTER for the same name replaces (and closes) the first
    instead of silently leaking it.
  * CONNECT waits up to --connect-wait seconds for the name to register
    rather than failing instantly. Multi-channel hosts register their
    input/control/audio names only *after* the video channel pairs, so a
    viewer that connects the next channel immediately would otherwise lose
    that race.
  * Slow-loris protection: the command line must arrive within
    --handshake-timeout seconds and is capped at 256 bytes.
  * Capacity limits (--max-waiting, --max-sessions) that refuse cleanly
    instead of exhausting file descriptors.
  * TCP keepalive on every socket, so half-open peers are eventually
    detected by the OS; optional --idle-timeout for stricter cleanup.
  * 64 KiB forwarding buffer and TCP_NODELAY; two threads per active
    session instead of three.
  * SIGTERM/SIGINT shut down cleanly.

Redundancy is client-side, not server-side: run several independent
relays and give hosts/viewers the whole list (desktop/relay_client.py).
Relays share no state, so there is nothing to synchronize and no relay is
special. See docs/phases/PHASE_12_README.md.

Usage:
    python3 relay.py [--port 6000] [--host 0.0.0.0]
"""

import argparse
import hashlib
import hmac
import json
import os
import signal
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request

MAX_COMMAND_BYTES = 256
PIPE_BUFFER = 64 * 1024

# A host registers "<device-id>-<label>" for each of its channels; the token is bound to the
# device ID, so one token covers all four.
CHANNEL_SUFFIXES = ("video", "input", "control", "audio")
_TOKEN_CONTEXT = b"remotebridge-relay-v1:"


def device_id_of(name: str) -> str:
    """'devZ-video' -> 'devZ'. A name without a known channel suffix is itself the device ID."""
    head, sep, tail = name.rpartition("-")
    return head if sep and head and tail in CHANNEL_SUFFIXES else name


def make_token(secret: str, device_id: str) -> str:
    """The token a device needs to register on a relay holding `secret`. admin/relayauth.py carries
    an identical copy (the admin console deploys separately); a test asserts they agree."""
    return hmac.new(secret.encode("utf-8"), _TOKEN_CONTEXT + device_id.encode("utf-8"),
                    hashlib.sha256).hexdigest()


# Expiring tokens: "<expiry>.<signature>", expiry = unix seconds, signature covers the device ID AND the
# expiry, so the expiry can't be edited without the secret. A token without a "." is the original
# non-expiring form. The relay accepts both unless it was started with --require-expiry.
_EXPIRING_TOKEN_CONTEXT = b"remotebridge-relay-v2:"
EXPIRY_LEEWAY = 30.0        # seconds of clock slack between whoever issued the token and this relay


def make_expiring_token(secret: str, device_id: str, expires_at: int) -> str:
    """A token that stops working at `expires_at` (unix seconds). admin/relayauth.py carries an identical
    copy; tests/test_relay_expiry.py asserts they agree."""
    expires_at = int(expires_at)
    sig = hmac.new(secret.encode("utf-8"),
                   _EXPIRING_TOKEN_CONTEXT + device_id.encode("utf-8") + b":" + str(expires_at).encode("ascii"),
                   hashlib.sha256).hexdigest()
    return f"{expires_at}.{sig}"


def parse_token(token: str):
    """(expires_at or None, signature) for a well-formed token, None for a malformed one."""
    expiry, dot, sig = token.partition(".")
    if not dot:
        return None, token
    if not (expiry.isascii() and expiry.isdigit() and 0 < len(expiry) <= 12 and sig and "." not in sig):
        return None
    return int(expiry), sig


def token_expiry(token):
    """The expiry (unix seconds) a token carries, or None for a non-expiring or unparseable one. Reads the
    claim only - it does not (and without the secret cannot) check the signature."""
    parsed = parse_token(token or "")
    return parsed[0] if parsed else None


_REVOCATION_CONTEXT = b"remotebridge-relay-revocation-list-v1"
MAX_REVOCATION_BYTES = 1024 * 1024


def revocation_bearer(secret: str) -> str:
    """Credential the relay presents when it polls the admin console's revoked-device list. Derived from
    the shared secret (like device tokens) so nothing new needs provisioning. admin/relayauth.py carries
    an identical copy; a test asserts they agree."""
    return hmac.new(secret.encode("utf-8"), _REVOCATION_CONTEXT, hashlib.sha256).hexdigest()


def parse_revoked_lines(text: str) -> frozenset:
    """One device ID per line; '#' starts a comment (whole-line or trailing); blanks ignored."""
    ids = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            ids.add(line)
    return frozenset(ids)


def _enable_keepalive(sock: socket.socket) -> None:
    """Best-effort: ask the OS to probe idle connections so a peer that
    vanished without a FIN (pulled cable, NAT timeout) is eventually
    reported as closed. Tuning constants only exist on some platforms."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for opt, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 4)):
            if hasattr(socket, opt):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), value)
    except OSError:
        pass


def _read_line(conn: socket.socket) -> str:
    """Reads one command line a byte at a time (never over-reads past the
    newline - the bytes after it belong to the paired peer, not to us)."""
    line = bytearray()
    while True:
        chunk = conn.recv(1)
        if not chunk:
            raise ConnectionError("peer closed before sending a command")
        if chunk == b"\n":
            break
        line += chunk
        if len(line) > MAX_COMMAND_BYTES:
            raise ConnectionError("command line too long")
    return line.decode("utf-8", errors="replace").strip()


def _peer_closed(sock: socket.socket) -> bool:
    """True if a registered (idle) socket has been closed by its host. A live
    idle registration has nothing to read; a closed one reads as b''. Never
    blocks. On platforms without MSG_DONTWAIT it reports False and the
    caller falls back on TCP keepalive."""
    if not hasattr(socket, "MSG_DONTWAIT"):
        return False
    try:
        return sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
    except BlockingIOError:
        return False
    except OSError:
        return True


def _close(sock) -> None:
    try:
        sock.close()
    except OSError:
        pass


def _cut(sock) -> None:
    """Forcibly end a connection that another thread may be blocked reading. A bare close() doesn't wake a
    thread sitting in recv() on the same socket (the descriptor stays alive until recv returns), so the
    session would survive its own revocation; shutdown() does wake it."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    _close(sock)


def _drop_tls(conn, timeout: float):
    """Ends the TLS layer that protected the command line (token, replies) and returns the same connection as
    plain TCP. Called once a connection is about to be paired or parked: from then on the bytes are the
    host<->viewer end-to-end TLS stream, which the relay forwards untouched, and the plain socket keeps
    everything the relay does with a parked registration (peeking for a dead host, select) working. The
    client does the same on its side, so both ends send close_notify and read the other's. Returns None if
    the peer doesn't (a client that would not downgrade is a client we can't pair)."""
    try:
        conn.settimeout(timeout)
        raw = conn.unwrap()
        raw.settimeout(None)
        return raw
    except (OSError, ValueError):
        _close(conn)
        return None


def make_server_tls_context(certfile: str, keyfile: str = None) -> ssl.SSLContext:
    """TLS context for the relay's listener (TLS 1.2+; certfile may hold the key too)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile, keyfile)
    return ctx


class RelayServer:
    """The relay, as an object so tests and the load tester can start one
    in-process on an ephemeral port (port=0) and stop it again."""

    def __init__(self, host: str = "0.0.0.0", port: int = 6000, *,
                 max_waiting: int = 10000, max_sessions: int = 5000,
                 connect_wait: float = 3.0, handshake_timeout: float = 5.0,
                 idle_timeout: float = 0.0, reap_interval: float = 5.0,
                 stats_open: bool = False, quiet: bool = False,
                 secrets=(), auth_fail_limit: int = 10, auth_fail_window: float = 60.0,
                 auth_ban: float = 300.0, revoked=(), revoked_file: str = None,
                 revoked_url: str = None, revoked_poll: float = 30.0, require_expiry: bool = False,
                 expiry_leeway: float = EXPIRY_LEEWAY, tls_context: ssl.SSLContext = None,
                 tls_port: int = 0, plain: bool = True):
        # `secrets` may hold several entries so a secret can be rotated without downtime: add the
        # new one, roll hosts over to new tokens, then drop the old one.
        self.secrets = tuple(x for x in secrets if x)
        # With require_expiry, the original non-expiring tokens are refused too, so a leaked token always
        # dies on its own. Off by default so a relay can be upgraded before every host and console is.
        self.require_expiry = require_expiry
        self.expiry_leeway = expiry_leeway
        self.auth_fail_limit = auth_fail_limit
        self.auth_fail_window = auth_fail_window
        self.auth_ban = auth_ban
        # Revocation: devices whose token must stop working NOW, without rotating the shared secret.
        # Sources are a static set (tests/embedding), a file, and the admin console; each keeps its
        # last good value when it can't be read, so an outage never silently un-revokes anyone.
        self.revoked_file = revoked_file
        self.revoked_url = revoked_url
        self.revoked_poll = revoked_poll
        self._revoked_sources = {"static": frozenset(revoked), "file": frozenset(), "url": frozenset()}
        self._revoked: frozenset = frozenset(revoked)
        self._sessions: dict = {}               # id -> (device_id, host_conn, viewer_conn), live pairs
        self._session_seq = 0
        self._auth_fails: dict = {}             # ip -> [timestamps of recent failures]
        self._banned_until: dict = {}           # ip -> monotonic time the ban ends
        self.host = host
        self.port = port
        # TLS to the relay: with a context, a second listener (tls_port; 0 = pick one) speaks TLS for the
        # command phase. plain=False closes the plaintext listener (port is then unused) so a relay can
        # refuse cleartext tokens outright. Without a context nothing changes.
        self.tls_context = tls_context
        self.tls_port = tls_port
        self.plain = plain or tls_context is None
        self.max_waiting = max_waiting
        self.max_sessions = max_sessions
        self.connect_wait = connect_wait
        self.handshake_timeout = handshake_timeout
        self.idle_timeout = idle_timeout
        self.reap_interval = reap_interval
        self.stats_open = stats_open
        self.quiet = quiet

        self._cond = threading.Condition()      # guards everything below
        self._waiting: dict = {}                # name -> socket
        self._expiry: dict = {}                 # name -> (socket, expires_at) for registrations made with an expiring token
        self._waiters: dict = {}                # name -> [Event] of CONNECTs waiting for that name
        self._active = 0
        self._counters = {"registered": 0, "replaced": 0, "paired": 0, "not_found": 0,
                          "busy": 0, "reaped": 0, "bad_command": 0, "bytes_forwarded": 0,
                          "peak_active": 0, "peak_waiting": 0, "auth_refused": 0, "auth_banned": 0,
                          "revoked_refused": 0, "revoked_cut": 0, "token_expired": 0,
                          "legacy_refused": 0, "expired_dropped": 0, "tls_connections": 0,
                          "tls_failed": 0}
        self._started = time.time()
        self._server = None
        self._tls_server = None
        self._stopping = threading.Event()
        self._threads: list = []

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> int:
        """Binds, starts the accept and reaper threads, returns the port."""
        try:
            # Every session costs two threads and the relay does no deep recursion: the default
            # (8 MB virtual each) is what limits session count on a small box, not the work done.
            threading.stack_size(512 * 1024)
        except (ValueError, RuntimeError):
            pass
        def listener(port: int) -> socket.socket:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.host, port))
            server.listen(1024)
            server.settimeout(0.5)   # so the accept loop can notice stop()
            return server

        loops = [(self._reap_loop, (), "relay-reaper")]
        if self.plain:
            self._server = listener(self.port)
            self.port = self._server.getsockname()[1]
            loops.append((self._accept_loop, (self._server, False), "relay-accept"))
        if self.tls_context is not None:
            self._tls_server = listener(self.tls_port)
            self.tls_port = self._tls_server.getsockname()[1]
            loops.append((self._accept_loop, (self._tls_server, True), "relay-accept-tls"))
        for target, targs, name in loops:
            t = threading.Thread(target=target, args=targs, daemon=True, name=name)
            t.start()
            self._threads.append(t)
        if self.revoked_file:
            self._refresh_revoked_file()        # before serving: a restart never opens a window
        if self.revoked_file or self.revoked_url:
            t = threading.Thread(target=self._revocation_loop, daemon=True, name="relay-revocation")
            t.start()
            self._threads.append(t)
        if self.plain:
            self._log(f"Listening on {self.host}:{self.port}")
        if self.tls_context is not None:
            self._log(f"Listening (TLS) on {self.host}:{self.tls_port}")
        return self.port if self.plain else self.tls_port

    def stop(self) -> None:
        self._stopping.set()
        for srv in (self._server, self._tls_server):
            if srv is not None:
                _close(srv)
        with self._cond:
            for conn in self._waiting.values():
                _close(conn)
            self._waiting.clear()
            for events in self._waiters.values():
                for event in events:
                    event.set()
        for t in self._threads:
            t.join(timeout=2)

    def stats(self) -> dict:
        with self._cond:
            out = dict(self._counters)
            out["waiting"] = len(self._waiting)
            out["active_sessions"] = self._active
            out["revoked_devices"] = len(self._revoked)
        out["uptime_seconds"] = round(time.time() - self._started, 1)
        return out

    def _log(self, msg: str) -> None:
        if not self.quiet:
            print(f"[relay] {msg}", flush=True)

    def _bump(self, key: str, n: int = 1) -> None:
        with self._cond:
            self._counters[key] += n

    # --- accept / dispatch -------------------------------------------------

    def _accept_loop(self, server: socket.socket, tls: bool = False) -> None:
        while not self._stopping.is_set():
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn, addr, tls), daemon=True).start()

    def _handle(self, conn: socket.socket, addr, tls: bool = False) -> None:
        if tls:
            # The handshake happens here, on the connection's own thread under the handshake timeout, so a
            # client that opens a socket and never speaks can't hold up the accept loop.
            try:
                conn.settimeout(self.handshake_timeout)
                conn = self.tls_context.wrap_socket(conn, server_side=True)
            except (ssl.SSLError, OSError):
                self._bump("tls_failed")
                _close(conn)
                return
            self._bump("tls_connections")
        try:
            conn.settimeout(self.handshake_timeout)
            line = _read_line(conn)
            conn.settimeout(None)
        except (ConnectionError, OSError):
            self._bump("bad_command")
            _close(conn)
            return

        command, _, rest = line.partition(" ")
        fields = rest.split()
        name = fields[0] if fields else ""
        token = fields[1] if len(fields) > 1 else ""
        _enable_keepalive(conn)
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

        if command in ("REGISTER", "CHECK") and name and self._is_banned(addr[0]):
            self._bump("auth_banned")
            _close(conn)
            return
        if command == "REGISTER" and name:
            verdict, expires_at = self._authorize(addr, name, token)
            if verdict != "ok":
                _close(conn)        # silent: a registering host expects raw bytes, never text
                return
            if self._is_revoked(name):
                # Holds a valid token but has been cut off. Not counted toward the address ban: a revoked
                # host retrying from a shared NAT must not get other devices behind it banned.
                self._bump("revoked_refused")
                self._log(f"REGISTER '{name}' from {addr} refused: device revoked")
                _close(conn)
                return
            if tls:
                conn = _drop_tls(conn, self.handshake_timeout)
                if conn is None:
                    self._bump("tls_failed")
                    return
            self._handle_register(conn, addr, name, expires_at)
        elif command == "CHECK" and name:
            verdict, _ = self._authorize(addr, name, token)
            if verdict == "expired":
                # Said only to a caller whose token carries a valid signature, so it can't be used to probe.
                self._reply_and_close(conn, b"ERROR expired\n")
            elif verdict != "ok":
                self._reply_and_close(conn, b"ERROR unauthorized\n")
            elif self._is_revoked(name):
                # Only said to a caller that proved it holds the token, so this can't be used to probe
                # which IDs are revoked.
                self._bump("revoked_refused")
                self._reply_and_close(conn, b"ERROR revoked\n")
            else:
                self._reply_and_close(conn, b"OK\n")
        elif command == "CONNECT" and name:
            self._handle_connect(conn, addr, name, tls)
        elif command == "PING" and not name:
            self._reply_and_close(conn, b"PONG\n")
        elif command == "STATS" and not name:
            if self.stats_open or addr[0] in ("127.0.0.1", "::1"):
                self._reply_and_close(conn, (json.dumps(self.stats()) + "\n").encode())
            else:
                self._reply_and_close(conn, b"ERROR forbidden\n")
        else:
            self._bump("bad_command")
            _close(conn)

    # --- authentication (Phase 13) ---------------------------------------------

    def _token_status(self, name: str, token: str, now: float = None) -> tuple:
        """(status, expires_at). status is "ok", "expired" (genuine signature, past its expiry), "legacy"
        (genuine non-expiring token, refused because --require-expiry) or "bad" (anything else)."""
        if not self.secrets:
            return "ok", None                             # open relay: exactly the pre-Phase-13 behavior
        if not token:
            return "bad", None
        parsed = parse_token(token)
        if parsed is None:
            return "bad", None
        expires_at, sig = parsed
        device_id = device_id_of(name)
        if expires_at is None:
            if not any(hmac.compare_digest(sig, make_token(sec, device_id)) for sec in self.secrets):
                return "bad", None
            return ("legacy", None) if self.require_expiry else ("ok", None)
        genuine = any(hmac.compare_digest(token, make_expiring_token(sec, device_id, expires_at))
                      for sec in self.secrets)
        if not genuine:
            return "bad", None
        if (time.time() if now is None else now) >= expires_at + self.expiry_leeway:
            return "expired", expires_at
        return "ok", expires_at

    def _token_ok(self, name: str, token: str) -> bool:
        return self._token_status(name, token)[0] == "ok"

    def _authorize(self, addr, name: str, token: str) -> tuple:
        """(verdict, expires_at). Only a "bad" token counts toward the address ban: an expired or
        non-expiring-when-refused token carries a genuine signature, so it is no guessing attempt, and a
        host with a stale token retrying from a shared NAT must not get its neighbours banned."""
        verdict, expires_at = self._token_status(name, token)
        if verdict == "ok":
            return verdict, expires_at
        if verdict == "expired":
            self._bump("token_expired")
            self._log(f"REGISTER/CHECK '{name}' from {addr} refused: token expired")
            return verdict, expires_at
        if verdict == "legacy":
            self._bump("legacy_refused")
            self._log(f"REGISTER/CHECK '{name}' from {addr} refused: non-expiring token (relay requires expiry)")
            return verdict, None
        ip = addr[0]
        now = time.monotonic()
        with self._cond:
            self._counters["auth_refused"] += 1
            fails = [t for t in self._auth_fails.get(ip, ()) if now - t < self.auth_fail_window]
            fails.append(now)
            self._auth_fails[ip] = fails
            if self.auth_fail_limit and len(fails) >= self.auth_fail_limit:
                self._banned_until[ip] = now + self.auth_ban
                self._auth_fails.pop(ip, None)
                self._log(f"{ip} banned for {self.auth_ban:.0f}s after {len(fails)} failed authentications")
            if len(self._auth_fails) > 10000:             # bound memory under a spray of source addresses
                self._auth_fails.clear()
        self._log(f"REGISTER/CHECK '{name}' from {addr} refused: bad or missing token")
        return "bad", None

    def _is_banned(self, ip: str) -> bool:
        with self._cond:
            until = self._banned_until.get(ip)
            if until is None:
                return False
            if time.monotonic() >= until:
                del self._banned_until[ip]
                return False
            return True

    # --- revocation --------------------------------------------------------------

    def _is_revoked(self, name: str) -> bool:
        with self._cond:
            return device_id_of(name) in self._revoked

    def revoked_devices(self) -> frozenset:
        with self._cond:
            return self._revoked

    def set_revoked(self, ids, source: str = "static") -> int:
        """Replace one source's list, recompute the union, and cut off every newly revoked device: its
        waiting registrations AND any session already in progress. Returns how many connections were cut.
        Un-revoking just shrinks the set; the device registers again on its next retry."""
        cut = []
        with self._cond:
            self._revoked_sources[source] = frozenset(ids)
            union = frozenset().union(*self._revoked_sources.values())
            newly = union - self._revoked
            self._revoked = union
            if newly:
                for name in [n for n in self._waiting if device_id_of(n) in union]:
                    cut.append(self._waiting.pop(name))
                for device_id, host_conn, viewer_conn in list(self._sessions.values()):
                    if device_id in union:
                        cut.extend((host_conn, viewer_conn))
                self._counters["revoked_cut"] += len(cut)
        for conn in cut:
            _cut(conn)
        if newly:
            self._log(f"revoked {len(newly)} device(s): {', '.join(sorted(newly))}; cut {len(cut)} connection(s)")
        return len(cut)

    def _refresh_revoked_file(self) -> None:
        try:
            with open(self.revoked_file, "r", encoding="utf-8") as f:
                text = f.read(MAX_REVOCATION_BYTES + 1)
            if len(text) > MAX_REVOCATION_BYTES:
                raise ValueError("file too large")
        except FileNotFoundError:
            return                  # no list yet means nobody revoked; keeps the last good value otherwise
        except (OSError, ValueError) as e:
            self._log(f"could not read revoked file {self.revoked_file}: {e}; keeping the previous list")
            return
        self.set_revoked(parse_revoked_lines(text), "file")

    def _refresh_revoked_url(self) -> None:
        candidates = self.secrets or ("",)
        last_error = None
        for sec in candidates:          # several secrets during a rotation: use whichever the console accepts
            req = urllib.request.Request(self.revoked_url, headers={
                "Accept": "application/json",
                **({"Authorization": "Bearer " + revocation_bearer(sec)} if sec else {})})
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    body = resp.read(MAX_REVOCATION_BYTES + 1)
                if len(body) > MAX_REVOCATION_BYTES:
                    raise ValueError("response too large")
                ids = json.loads(body.decode("utf-8")).get("revoked")
                if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                    raise ValueError("unexpected response shape")
            except urllib.error.HTTPError as e:
                last_error = f"HTTP {e.code}"
                if e.code in (401, 403):
                    continue
                break
            except (OSError, ValueError) as e:
                last_error = str(e)
                break
            self.set_revoked(ids, "url")
            return
        self._log(f"could not refresh revoked list from {self.revoked_url}: {last_error}; keeping the previous list")

    def _revocation_loop(self) -> None:
        first = True
        while first or not self._stopping.wait(self.revoked_poll):
            first = False
            if self.revoked_file:
                self._refresh_revoked_file()
            if self.revoked_url:
                self._refresh_revoked_url()

    @staticmethod
    def _reply_and_close(conn: socket.socket, data: bytes) -> None:
        try:
            conn.sendall(data)
        except OSError:
            pass
        _close(conn)

    def _handle_register(self, conn, addr, name: str, expires_at=None) -> None:
        with self._cond:
            old = self._waiting.get(name)
            if device_id_of(name) in self._revoked:
                full, why = True, "device revoked"      # revoked between the check in _handle and here
            elif old is None and len(self._waiting) >= self.max_waiting:
                full, why = True, "waiting table full"
            else:
                full, why = False, ""
                self._waiting[name] = conn
                if expires_at is not None:
                    self._expiry[name] = (conn, expires_at)
                else:
                    self._expiry.pop(name, None)
                self._counters["registered"] += 1
                if old is not None:
                    self._counters["replaced"] += 1
                self._counters["peak_waiting"] = max(self._counters["peak_waiting"], len(self._waiting))
                for event in self._waiters.pop(name, ()):
                    event.set()     # only the CONNECTs waiting for THIS name - not every waiter
        if full:
            # A registering host expects raw bytes next, not text - the only
            # honest signal available is closing the connection.
            self._log(f"REGISTER '{name}' from {addr} refused: {why}")
            _close(conn)
            return
        if old is not None:
            _close(old)
        self._log(f"'{name}' registered from {addr}")
        # Deliberately not closed: stays registered until a CONNECT claims it.

    def _handle_connect(self, conn, addr, name: str, tls: bool = False) -> None:
        deadline = time.monotonic() + self.connect_wait
        host_conn = None
        busy = False
        event = threading.Event()
        with self._cond:
            while True:
                event.clear()
                if self._active >= self.max_sessions:
                    busy = True
                    break
                host_conn = self._waiting.pop(name, None)
                if host_conn is not None and _peer_closed(host_conn):
                    # The host left after registering and the reaper hasn't
                    # swept yet. Pairing a viewer with a corpse would fail
                    # the whole session; discard it and keep waiting for a
                    # live registration instead.
                    _close(host_conn)
                    self._counters["reaped"] += 1
                    host_conn = None
                if host_conn is not None or self._stopping.is_set():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._waiters.setdefault(name, []).append(event)
                self._cond.release()
                try:
                    event.wait(remaining)
                finally:
                    self._cond.acquire()
                    mine = self._waiters.get(name)
                    if mine is not None and event in mine:
                        mine.remove(event)
                        if not mine:
                            del self._waiters[name]
            if host_conn is not None:
                self._active += 1
                self._counters["paired"] += 1
                self._counters["peak_active"] = max(self._counters["peak_active"], self._active)
            elif busy:
                self._counters["busy"] += 1
            else:
                self._counters["not_found"] += 1

        if host_conn is None:
            reason = "busy" if busy else "not_found"
            self._reply_and_close(conn, f"ERROR {reason}\n".encode())
            self._log(f"CONNECT '{name}' from {addr} failed: {reason}")
            return

        try:
            conn.sendall(b"OK\n")
            if tls:
                conn = _drop_tls(conn, self.handshake_timeout)
                if conn is None:
                    raise OSError("viewer did not leave TLS")
        except OSError:
            _close(conn)
            _close(host_conn)
            with self._cond:
                self._active -= 1
            return

        self._log(f"pairing viewer {addr} with '{name}'")
        with self._cond:
            self._session_seq += 1
            session_id = self._session_seq
            self._sessions[session_id] = (device_id_of(name), host_conn, conn)
            revoked_now = device_id_of(name) in self._revoked
        if revoked_now:                 # revoked in the instant between pairing and registering the session
            _cut(host_conn)
            _cut(conn)
        try:
            self._relay_pair(host_conn, conn)
        finally:
            with self._cond:
                self._active -= 1
                self._sessions.pop(session_id, None)
        self._log(f"session for '{name}' ended")

    # --- forwarding --------------------------------------------------------

    def _pipe(self, src: socket.socket, dst: socket.socket) -> None:
        forwarded = 0
        try:
            src.settimeout(self.idle_timeout or None)
            while True:
                data = src.recv(PIPE_BUFFER)
                if not data:
                    break
                dst.sendall(data)
                forwarded += len(data)
        except OSError:
            pass    # includes socket.timeout when --idle-timeout trips
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            if forwarded:
                self._bump("bytes_forwarded", forwarded)

    def _relay_pair(self, host_conn: socket.socket, viewer_conn: socket.socket) -> None:
        other = threading.Thread(target=self._pipe, args=(viewer_conn, host_conn), daemon=True)
        other.start()
        self._pipe(host_conn, viewer_conn)     # this thread does the other direction
        other.join()
        _close(host_conn)
        _close(viewer_conn)

    # --- reaper ------------------------------------------------------------

    def _reap_loop(self) -> None:
        # Runs even where there is no non-blocking peek (reap_once skips the dead-host check there and
        # relies on TCP keepalive), because the expiry sweep needs no peek.
        while not self._stopping.wait(self.reap_interval):
            self.reap_once()

    def expire_registrations(self, now: float = None) -> int:
        """Closes waiting registrations whose token has expired. A registration is checked once, when it
        is made, but it can sit here for a long time; without this a token that leaked and was used just
        before it expired would keep its slot (and any viewer who pairs with it) indefinitely. Live
        sessions are NOT cut - they were authorized while the token was good; use revocation for that.
        The host re-registers on its own, with a fresh token. Returns how many were closed."""
        now = time.time() if now is None else now
        expired = []
        with self._cond:
            for name, (conn, expires_at) in list(self._expiry.items()):
                if self._waiting.get(name) is not conn:
                    del self._expiry[name]              # replaced, claimed or reaped since: stale entry
                elif now >= expires_at + self.expiry_leeway:
                    del self._expiry[name]
                    del self._waiting[name]
                    expired.append((name, conn))
            self._counters["expired_dropped"] += len(expired)
        for _, conn in expired:                         # closed outside the lock; the host sees EOF
            _close(conn)
        if expired:
            self._log(f"closed {len(expired)} registration(s) whose token expired: "
                      + ", ".join(sorted(n for n, _ in expired)))
        return len(expired)

    def reap_once(self) -> int:
        """Drops registered sockets whose host has closed. A live, idle
        registration has nothing to read (BlockingIOError); a closed one
        reads as b''. Returns how many were dropped."""
        self.expire_registrations()
        with self._cond:
            items = list(self._waiting.items())
        dead = []
        for name, conn in items:
            if _peer_closed(conn):
                dead.append((name, conn))
        if not dead:
            return 0
        removed = 0
        with self._cond:
            for name, conn in dead:
                if self._waiting.get(name) is conn:    # not replaced/claimed meanwhile
                    del self._waiting[name]
                    removed += 1
            self._counters["reaped"] += removed
        for _, conn in dead:
            _close(conn)
        if removed:
            self._log(f"reaped {removed} dead registration(s)")
        return removed


def main() -> None:
    parser = argparse.ArgumentParser(description="Relay server (Phase 1, hardened in Phase 12)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=6000)
    parser.add_argument("--max-waiting", type=int, default=10000,
                        help="max simultaneously registered (waiting) hosts/channels")
    parser.add_argument("--max-sessions", type=int, default=5000,
                        help="max simultaneously paired sessions (each uses 2 threads)")
    parser.add_argument("--connect-wait", type=float, default=3.0,
                        help="seconds a CONNECT waits for its name to register before ERROR not_found")
    parser.add_argument("--handshake-timeout", type=float, default=5.0)
    parser.add_argument("--idle-timeout", type=float, default=0.0,
                        help="close a session with no traffic in a direction for this many seconds "
                             "(0 = off; TCP keepalive still applies)")
    parser.add_argument("--stats-open", action="store_true",
                        help="answer STATS from any address, not just loopback")
    parser.add_argument("--quiet", action="store_true", help="don't log every registration/pairing")
    parser.add_argument("--secret", default=os.environ.get("RELAY_SECRET", ""),
                        help="Phase 13: require a per-device token to REGISTER. Comma-separate several "
                             "to rotate a secret without downtime. Default: $RELAY_SECRET; empty = open relay")
    parser.add_argument("--auth-fail-limit", type=int, default=10,
                        help="failed authentications from one address (per minute) before it is banned (0 = never)")
    parser.add_argument("--auth-ban", type=float, default=300.0, help="seconds an address stays banned")
    parser.add_argument("--require-expiry", action="store_true",
                        default=os.environ.get("RELAY_REQUIRE_EXPIRY", "").lower() in ("1", "true", "yes"),
                        help="refuse the original non-expiring device tokens, so every token a host presents "
                             "must carry an expiry (see relay_token.py --ttl, and RELAY_TOKEN_TTL on the admin "
                             "console). Turn this on only after every host and console issues expiring tokens. "
                             "Default: $RELAY_REQUIRE_EXPIRY")
    parser.add_argument("--revoked-file", default=os.environ.get("RELAY_REVOKED_FILE"),
                        help="file of revoked device IDs, one per line (# comments). Re-read every --revoked-poll "
                             "seconds; adding an ID cuts that device off immediately, including a live session")
    parser.add_argument("--revoked-url", default=os.environ.get("RELAY_REVOKED_URL"),
                        help="admin console revoked-device endpoint, e.g. https://admin.example.org/api/v1/relay/revoked "
                             "(authenticated with a credential derived from --secret). Default: $RELAY_REVOKED_URL")
    parser.add_argument("--revoked-poll", type=float, default=30.0,
                        help="seconds between revocation list refreshes (default 30)")
    parser.add_argument("--tls-cert", default=os.environ.get("RELAY_TLS_CERT"),
                        help="PEM certificate (chain) - listen for TLS on --tls-port so device tokens don't "
                             "cross the network in cleartext. Default: $RELAY_TLS_CERT")
    parser.add_argument("--tls-key", default=os.environ.get("RELAY_TLS_KEY"),
                        help="PEM private key for --tls-cert (omit if the cert file holds it). Default: $RELAY_TLS_KEY")
    parser.add_argument("--tls-port", type=int, default=int(os.environ.get("RELAY_TLS_PORT", "6443")),
                        help="port for the TLS listener (default 6443)")
    parser.add_argument("--no-plain", action="store_true",
                        default=os.environ.get("RELAY_NO_PLAIN", "").lower() in ("1", "true", "yes"),
                        help="close the plaintext listener (needs --tls-cert); only after every host and "
                             "viewer uses tls:// relay addresses. Default: $RELAY_NO_PLAIN")
    args = parser.parse_args()
    tls_ctx = None
    if args.tls_cert:
        tls_ctx = make_server_tls_context(args.tls_cert, args.tls_key)
        print(f"[relay] TLS listener on port {args.tls_port}"
              + ("; plaintext listener OFF." if args.no_plain else "; plaintext still on (use --no-plain once clients have moved)."),
              flush=True)
    elif args.no_plain:
        parser.error("--no-plain needs --tls-cert")
    else:
        print("[relay] NOTE: no TLS - device tokens cross the network in cleartext "
              "(set --tls-cert/--tls-key, or put a TLS terminator in front).", flush=True)
    secrets_list = [x.strip() for x in args.secret.split(",") if x.strip()]
    if secrets_list:
        print(f"[relay] authentication ON ({len(secrets_list)} secret(s)); hosts need a device token"
              + ("; non-expiring tokens are REFUSED." if args.require_expiry else "."), flush=True)
    else:
        print("[relay] WARNING: authentication OFF - anyone can register any device ID "
              "(set --secret or $RELAY_SECRET).", flush=True)

    relay = RelayServer(args.host, args.port, max_waiting=args.max_waiting,
                        max_sessions=args.max_sessions, connect_wait=args.connect_wait,
                        handshake_timeout=args.handshake_timeout, idle_timeout=args.idle_timeout,
                        stats_open=args.stats_open, quiet=args.quiet,
                        secrets=secrets_list, auth_fail_limit=args.auth_fail_limit,
                        auth_ban=args.auth_ban, revoked_file=args.revoked_file,
                        revoked_url=args.revoked_url, revoked_poll=args.revoked_poll,
                        require_expiry=args.require_expiry, tls_context=tls_ctx,
                        tls_port=args.tls_port, plain=not args.no_plain)
    relay.start()

    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    done.wait()
    print("[relay] shutting down...", flush=True)
    relay.stop()


if __name__ == "__main__":
    main()
