"""
Phase 12 - client side of relay redundancy and failover.

Relays share no state (see server/relay.py), so redundancy lives here:

  * A HOST registers its channel name on EVERY configured relay at once
    and waits. Whichever relay a viewer reaches first pairs it; the host
    detects that (the paired socket becomes readable - the viewer's first
    bytes are arriving), closes its registrations on the others, and
    carries on using the relay that won. A relay that is down at startup,
    or that drops the registration later (restart, network), is retried
    in the background with backoff while the others keep serving.
  * A VIEWER tries the relays in order until one pairs it. A relay that
    refuses the connection, times out, or doesn't know the name
    (ERROR not_found - the host isn't registered there) just moves on to
    the next; if none work, the error lists why each one failed.
  * The other channels of the same session (input/control/audio) are
    "pinned" to the relay the video channel used - a session never
    straddles relays.

Deliberate limits, so nobody is surprised:
  * Failover happens at CONNECT time. A session already running through a
    relay that then dies ends (the viewer's existing auto-reconnect
    starts a new one, which will pick a live relay). It is not migrated
    mid-stream.
  * A host that is registered on relay A and B, with viewers arriving at
    both at the same instant, pairs one and drops the other viewer's
    connection; that viewer's retry succeeds. Single-viewer-at-a-time
    hosts (host.py) never see this; multi-user hosts serve the second
    viewer on the retry.
"""

import os
import select
import socket
import ssl
import threading
import time

CONNECT_TIMEOUT = 5.0
REPLY_TIMEOUT = 15.0   # must exceed the relay's --connect-wait (default 3s) with margin
MAX_BACKOFF = 30.0


class RelayError(Exception):
    """No relay could satisfy the request. `attempts` is a list of
    (relay, reason) pairs, one per relay tried."""

    def __init__(self, attempts):
        self.attempts = list(attempts)
        detail = "; ".join(f"{h}:{p}: {why}" for (h, p), why in self.attempts) or "no relays configured"
        super().__init__(f"could not reach the device through any relay ({detail})")


class Relay(tuple):
    """A relay address: still a (host, port) tuple everywhere it is unpacked, compared or logged, with a
    `tls` flag saying the command phase (token, replies) must go over TLS. Written `tls://host:port`."""

    def __new__(cls, host: str, port: int, tls: bool = False):
        self = super().__new__(cls, (host, int(port)))
        self.tls = bool(tls)
        return self

    def __repr__(self):
        return f"Relay({self[0]!r}, {self[1]}, tls={self.tls})"


_ca_file = None          # trust this CA/cert for tls:// relays instead of the system store


def set_ca_file(path) -> None:
    """Trust the certificate(s) in `path` for tls:// relays (a private CA, or the relay's own self-signed
    certificate). Default: $REMOTEBRIDGE_RELAY_CA, else the system trust store."""
    global _ca_file
    _ca_file = path or None


def _tls_context() -> ssl.SSLContext:
    ca = _ca_file or os.environ.get("REMOTEBRIDGE_RELAY_CA")
    ctx = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _is_tls(relay) -> bool:
    return bool(getattr(relay, "tls", False))


def _open(relay, timeout: float) -> socket.socket:
    """Connects to a relay; for a tls:// relay, completes the TLS handshake and verifies the certificate
    against the relay's hostname. Raises OSError (ssl.SSLError is one) on any failure."""
    sock = socket.create_connection(tuple(relay), timeout=timeout)
    if not _is_tls(relay):
        return sock
    try:
        return _tls_context().wrap_socket(sock, server_hostname=relay[0])
    except BaseException:
        sock.close()
        raise


def _leave_tls(sock, timeout: float = 10.0, what: str = "the relay"):
    """Mirrors relay._drop_tls: once the command line is done, drop to plain TCP so the end-to-end stream
    (and our peeking at a parked registration) runs on the bare socket. Plain sockets pass straight through."""
    if not isinstance(sock, ssl.SSLSocket):
        return sock
    try:
        sock.settimeout(timeout)
        raw = sock.unwrap()
    except (OSError, ValueError) as e:
        sock.close()
        raise ConnectionError(f"{what} closed the secure channel instead of continuing "
                              f"(bad, expired or revoked token?): {e}") from e
    return raw


def parse_relays(spec) -> list:
    """'a.example.com:6000,tls://b.example.com:6443' -> [(host, port), ...] (the second marked tls).
    Also accepts an already-parsed list. Whitespace and empty entries are
    ignored; a duplicate is dropped (registering twice on one relay would
    just replace itself)."""
    if not spec:
        return []
    if isinstance(spec, (list, tuple)):
        return [r if isinstance(r, Relay) else tuple(r) for r in spec]
    relays = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        tls = part.lower().startswith("tls://")
        addr = part[6:] if tls else part
        host, sep, port = addr.rpartition(":")
        if not sep or not host or not port.isdigit():
            raise ValueError(f"bad relay address {part!r} - expected host:port or tls://host:port")
        entry = Relay(host, int(port), tls)
        if entry not in relays:
            relays.append(entry)
    return relays


def _keepalive(sock: socket.socket) -> None:
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for opt, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 4)):
            if hasattr(socket, opt):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), value)
    except OSError:
        pass


def _read_line(sock: socket.socket) -> str:
    line = bytearray()
    while True:
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectionError("relay closed the connection")
        if chunk == b"\n":
            return line.decode("utf-8", errors="replace").strip()
        line += chunk
        if len(line) > 1024:   # STATS is one JSON line; every other reply is tiny
            raise ConnectionError("relay sent an over-long reply")


# --- Viewer side -----------------------------------------------------------

def viewer_connect_one(relay: tuple, name: str, connect_timeout: float = CONNECT_TIMEOUT,
                       reply_timeout: float = REPLY_TIMEOUT) -> socket.socket:
    """CONNECT to `name` on one specific relay. Raises OSError/ConnectionError
    or RelayError-style ConnectionError('ERROR ...') on any failure."""
    sock = _open(relay, connect_timeout)
    try:
        _keepalive(sock)
        sock.sendall(f"CONNECT {name}\n".encode())
        sock.settimeout(reply_timeout)
        reply = _read_line(sock)
        if reply != "OK":
            raise ConnectionError(reply or "empty reply")
        sock = _leave_tls(sock)
        sock.settimeout(None)
        return sock
    except BaseException:
        sock.close()
        raise


def viewer_connect(relays: list, name: str, connect_timeout: float = CONNECT_TIMEOUT,
                   reply_timeout: float = REPLY_TIMEOUT, prefer=None) -> tuple:
    """Tries each relay in order (`prefer` first if given). Returns
    (socket, relay) for the first that pairs; raises RelayError otherwise."""
    order = list(relays)
    if prefer in order:
        order.remove(prefer)
        order.insert(0, prefer)
    attempts = []
    for relay in order:
        try:
            return viewer_connect_one(relay, name, connect_timeout, reply_timeout), relay
        except (OSError, ConnectionError) as e:
            attempts.append((relay, str(e) or type(e).__name__))
    raise RelayError(attempts)


# --- Host side -------------------------------------------------------------

class TokenProvider:
    """The relay token a host presents, and how it stays fresh.

    A token from the admin console may expire (RELAY_TOKEN_TTL). The host asks for a new one once half
    of the lifetime it was told about has passed - well before the old one stops working, so a failed
    attempt (console down, network) has the other half to be retried in. Until a refresh succeeds the old
    token is kept. A token that doesn't expire (or one given by hand) is never refreshed.

    Callable: provider() is the token to use right now, so it can be passed wherever a token string can.
    `refresh` is a callable returning (token, expires_in_seconds_or_None); it may raise. Times are
    monotonic and relative - nothing here depends on the host's clock being right."""

    def __init__(self, token: str = None, expires_in: float = None, refresh=None, log=None,
                 clock=time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self._log = log or (lambda _msg: None)
        self._refresh = refresh
        self._token = token
        self._ttl = expires_in
        self._issued = clock()
        self._retry_at = 0.0

    def __call__(self) -> str:
        return self.get()

    def get(self) -> str:
        with self._lock:
            if self._refresh is not None and self._ttl and self._clock() >= max(
                    self._issued + self._ttl * 0.5, self._retry_at):
                self._try_refresh()
            return self._token

    def seconds_left(self):
        """Seconds until the current token expires, as far as we were told; None if it doesn't."""
        with self._lock:
            if not self._ttl:
                return None
            return max(0.0, self._issued + self._ttl - self._clock())

    def _try_refresh(self) -> None:
        try:
            token, expires_in = self._refresh()
        except Exception as e:                       # the console being down must never stop the host
            self._retry_at = self._clock() + max(5.0, min(60.0, (self._ttl or 60.0) / 10))
            left = max(0.0, self._issued + (self._ttl or 0) - self._clock())
            self._log(f"could not refresh the relay token ({e}); keeping the current one "
                      f"(about {int(left)} s left), will retry")
            return
        if not token:
            self._refresh = None                     # the console no longer issues tokens: stop asking
            return
        self._token, self._ttl, self._issued = token, expires_in, self._clock()
        self._retry_at = 0.0
        if not expires_in:
            self._refresh = None                     # now a non-expiring token: nothing left to refresh
        self._log("refreshed the relay token")


def _current_token(token) -> str:
    """A token argument is a string, None, or a callable (TokenProvider) returning one."""
    return token() if callable(token) else token


def host_register_on(relay: tuple, name: str, connect_timeout: float = CONNECT_TIMEOUT,
                     token=None) -> socket.socket:
    """Registers `name` on one relay and returns the (unpaired) socket at
    once - it is the caller's job to wait for the viewer's bytes. Used
    directly for the input/control/audio channels, which are registered on
    the relay the video channel already chose. `token` may be a TokenProvider."""
    token = _current_token(token)
    sock = _open(relay, connect_timeout)
    try:
        _keepalive(sock)
        # Phase 13: an authenticating relay wants "REGISTER <name> <token>"; an open relay ignores the token.
        sock.sendall((f"REGISTER {name} {token}\n" if token else f"REGISTER {name}\n").encode())
        # Over TLS the relay answers a refused registration by closing, which shows up here as a failed
        # leave-TLS; a good one leaves TLS at once and parks the plain socket.
        sock = _leave_tls(sock, connect_timeout)
        sock.settimeout(None)
        return sock
    except BaseException:
        sock.close()
        raise


def host_wait_paired(relays: list, name: str, stop=None, poll: float = 0.5,
                     log=None, token=None) -> tuple:
    """Registers `name` on every relay and blocks until a viewer pairs on
    one of them. Returns (socket, relay); every other registration is
    closed first. `stop` is an optional threading.Event; setting it makes
    this raise InterruptedError. `log` is an optional callable(str) for
    status lines. `token` may be a TokenProvider: when it hands out a new token, each registration is
    renewed in place (new one registered first, which makes the relay close the old one), so a viewer
    arriving at that moment still finds the host."""
    say = log or (lambda _msg: None)
    regs: dict = {}                          # relay -> registered socket
    reg_token: dict = {}                     # relay -> the token that registration was made with
    rotate_at = {r: 0.0 for r in relays}     # relay -> earliest next attempt to renew with a new token
    retry_at = {r: 0.0 for r in relays}      # relay -> earliest next attempt
    backoff = {r: 1.0 for r in relays}

    def drop(relay):
        sock = regs.pop(relay, None)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        retry_at[relay] = time.monotonic() + backoff[relay]
        backoff[relay] = min(backoff[relay] * 2, MAX_BACKOFF)

    try:
        while True:
            if stop is not None and stop.is_set():
                raise InterruptedError("stopped while waiting for a viewer")
            now = time.monotonic()
            current = _current_token(token)
            for relay in relays:
                if relay in regs and reg_token.get(relay) != current and now >= rotate_at[relay]:
                    try:
                        fresh = host_register_on(relay, name, token=current)
                    except OSError:
                        rotate_at[relay] = now + 5.0    # the old registration is still good for a while
                    else:
                        old_sock, regs[relay], reg_token[relay] = regs[relay], fresh, current
                        try:
                            old_sock.close()
                        except OSError:
                            pass
                        say(f"renewed '{name}' on relay {relay[0]}:{relay[1]} with a fresh token")
                if relay not in regs and now >= retry_at[relay]:
                    try:
                        regs[relay] = host_register_on(relay, name, token=current)
                        reg_token[relay] = current
                        backoff[relay] = 1.0
                        say(f"registered '{name}' on relay {relay[0]}:{relay[1]}")
                    except OSError as e:
                        say(f"relay {relay[0]}:{relay[1]} unavailable ({e}); will retry")
                        retry_at[relay] = now + backoff[relay]
                        backoff[relay] = min(backoff[relay] * 2, MAX_BACKOFF)

            if not regs:
                time.sleep(min(poll, 0.5))
                continue

            by_sock = {s: r for r, s in regs.items()}
            try:
                readable, _, _ = select.select(list(by_sock), [], [], poll)
            except (OSError, ValueError):
                # a socket went bad between the check and the select
                for relay in list(regs):
                    if regs[relay].fileno() < 0:
                        drop(relay)
                continue

            for sock in readable:
                relay = by_sock[sock]
                try:
                    peeked = sock.recv(1, socket.MSG_PEEK)
                except OSError:
                    peeked = b""
                if peeked == b"":
                    say(f"relay {relay[0]}:{relay[1]} dropped our registration; will re-register"
                        + (" (if this repeats, the relay may be rejecting our token)" if current else ""))
                    drop(relay)
                    continue
                # Paired: bytes from a viewer are waiting. Keep this one.
                paired = regs.pop(relay)
                for other in list(regs.values()):
                    try:
                        other.close()
                    except OSError:
                        pass
                regs.clear()
                say(f"viewer paired via relay {relay[0]}:{relay[1]}")
                return paired, relay
    except BaseException:
        for sock in regs.values():
            try:
                sock.close()
            except OSError:
                pass
        raise


# --- Health check ----------------------------------------------------------

def ping(relay: tuple, timeout: float = 2.0) -> bool:
    """True if the relay answers PING with PONG - what a monitoring probe
    or a load balancer's health check would do."""
    try:
        with _open(relay, timeout) as sock:
            sock.sendall(b"PING\n")
            sock.settimeout(timeout)
            return _read_line(sock) == "PONG"
    except (OSError, ConnectionError):
        return False


def check_token(relay: tuple, name: str, token: str, timeout: float = 5.0) -> str:
    """Phase 13: asks one relay whether `token` is valid for `name`, without registering anything.
    Returns "ok", "unauthorized", "expired", "revoked", or "unreachable: <why>". An open relay answers
    "ok" to anything. "expired" means the signature is genuine but the token is past its expiry."""
    try:
        with _open(relay, timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(f"CHECK {name} {token or ''}".rstrip().encode() + b"\n")
            reply = _read_line(sock)
    except (OSError, ConnectionError) as e:
        return f"unreachable: {e}"
    if reply == "OK":
        return "ok"
    if reply == "ERROR unauthorized":
        return "unauthorized"
    if reply == "ERROR revoked":
        return "revoked"
    if reply == "ERROR expired":
        return "expired"
    return f"unreachable: unexpected reply {reply!r}"
