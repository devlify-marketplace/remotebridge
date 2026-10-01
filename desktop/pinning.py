"""
Certificate pinning for the viewer (trust on first use).

Hosts use self-signed certificates, so the viewer cannot validate them against a
CA. Until now it accepted whatever certificate it was shown, which means anyone
on the path (a rogue Wi-Fi, a compromised relay operator who terminates TLS)
could sit in the middle of a session undetected. This module closes that:

  - The first time a viewer connects to a host, the SHA-256 fingerprint of the
    host's certificate is shown to the user and remembered.
  - Every later connection must present the same certificate. A different one is
    refused loudly, never silently accepted.
  - A user who has the real fingerprint from the host's owner can supply it
    (--pin) and skip the trust-on-first-use step entirely; that is the strong form.
  - All four channels of one connection must present the SAME certificate, so an
    attacker cannot intercept just one of them.

What is pinned, and under which key:
  - direct connections:  "<host>:<video port>"
  - relay connections:   "device:<device id>"  (the relay is only a pipe; the
    certificate belongs to the device, so the pin survives a change of relay)

This module prints nothing; it raises exceptions carrying the facts and lets the
caller (which owns localization) say them.
"""

import datetime
import hashlib
import json
import os
import re
import ssl
import tempfile
import threading

ENV_PATH = "REMOTEBRIDGE_KNOWN_HOSTS"


def default_path() -> str:
    return os.environ.get(ENV_PATH) or os.path.join(
        os.path.expanduser("~"), ".remotebridge", "known_hosts.json")


class PinError(Exception):
    """Base class."""


class PinMismatch(PinError):
    def __init__(self, key: str, expected: str, actual: str):
        super().__init__(f"certificate for {key} changed: expected {expected}, got {actual}")
        self.key, self.expected, self.actual = key, expected, actual


class PinRejected(PinError):
    """The user declined to trust a host's certificate on first use."""

    def __init__(self, key: str, fingerprint: str):
        super().__init__(f"certificate for {key} not trusted")
        self.key, self.fingerprint = key, fingerprint


class PinStoreError(PinError):
    """The known-hosts file exists but cannot be read. Fails closed: guessing 'empty'
    would let an attacker who corrupts the file get a free first-use."""

    def __init__(self, path: str, cause):
        super().__init__(f"cannot read {path}: {cause}")
        self.path, self.cause = path, cause


# --- Fingerprints -------------------------------------------------------------

def fingerprint_der(der: bytes) -> str:
    """'AB:CD:...' (uppercase, colon-separated): the same shape `openssl x509
    -fingerprint -sha256` prints, so it can be compared by eye or by script."""
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def fingerprint_pem_file(path: str) -> str:
    with open(path, "r", encoding="ascii", errors="replace") as f:
        text = f.read()
    m = re.search(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", text, re.S)
    if not m:
        raise ValueError(f"no certificate found in {path}")
    return fingerprint_der(ssl.PEM_cert_to_DER_cert(m.group(0)))


def normalize(fp: str) -> str:
    """Canonical comparison form: bare lowercase hex. Accepts 'sha256:' prefixes,
    colons, spaces and either case; raises ValueError if it isn't a SHA-256."""
    s = re.sub(r"^\s*sha-?256\s*[:=]?", "", fp or "", flags=re.I)
    s = re.sub(r"[\s:]", "", s).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", s):
        raise ValueError("a SHA-256 fingerprint is 64 hex digits (colons optional)")
    return s


def same(a: str, b: str) -> bool:
    return normalize(a) == normalize(b)


def key_direct(host: str, video_port) -> str:
    return f"{host.strip().lower()}:{int(video_port)}"


def key_device(device_id: str) -> str:
    return f"device:{device_id.strip()}"


# --- Persistent store -----------------------------------------------------------

class PinStore:
    """known_hosts.json: {"hosts": {key: {"fingerprint", "first_seen"}}}. Re-read
    on every access so two viewers running at once see each other's pins."""

    _lock = threading.Lock()

    def __init__(self, path: str = None):
        self.path = path or default_path()

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("hosts", {}), dict):
                raise ValueError("unexpected structure")
            return data
        except FileNotFoundError:
            return {"hosts": {}}
        except (OSError, ValueError) as e:     # JSONDecodeError is a ValueError
            raise PinStoreError(self.path, e)

    def get(self, key: str):
        entry = self._load()["hosts"].get(key)
        return entry.get("fingerprint") if isinstance(entry, dict) else None

    def set(self, key: str, fingerprint: str) -> None:
        with self._lock:
            data = self._load()
            old = data["hosts"].get(key) if isinstance(data["hosts"].get(key), dict) else {}
            data["hosts"][key] = {
                "fingerprint": fingerprint,
                "first_seen": old.get("first_seen") or
                              datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            }
            self._write(data)

    def forget(self, key: str) -> bool:
        with self._lock:
            data = self._load()
            existed = data["hosts"].pop(key, None) is not None
            if existed:
                self._write(data)
            return existed

    def _write(self, data: dict) -> None:
        d = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".known_hosts-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.path)       # atomic: a crash never leaves half a file
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# --- Per-connection checker ---------------------------------------------------------

class Pinner:
    """One instance per connection attempt. Call check(tls_socket) right after each
    channel's TLS handshake. The first call decides trust; later calls just confirm the
    other channels present the same certificate.

      store        PinStore
      key          from key_direct / key_device
      expected     fingerprint the user supplied out of band (--pin), or None
      confirm_new  callable(key, fingerprint) -> bool, asked only on first use with no
                   --pin. None means "trust and remember without asking".
      replace      accept and overwrite a DIFFERENT remembered pin (--replace-pin)

    After the first check, .status is 'pinned' (matched --pin), 'new', 'match' or
    'replaced', and .fingerprint is the certificate's.
    """

    def __init__(self, store: PinStore, key: str, expected: str = None,
                 confirm_new=None, replace: bool = False):
        self.store, self.key, self.replace = store, key, replace
        self.expected = normalize(expected) if expected else None
        self.confirm_new = confirm_new
        self.fingerprint = None
        self.status = None
        self._lock = threading.Lock()

    def check(self, conn) -> str:
        der = conn.getpeercert(binary_form=True)
        if not der:
            raise PinError(f"{self.key} presented no certificate")
        fp = fingerprint_der(der)
        with self._lock:
            if self.fingerprint is None:
                self._decide(fp)
                self.fingerprint = fp
            elif fp != self.fingerprint:
                raise PinMismatch(self.key, self.fingerprint, fp)
        return fp

    def _decide(self, fp: str) -> None:
        if self.expected is not None:
            if normalize(fp) != self.expected:
                raise PinMismatch(self.key, self.expected, fp)
            self.store.set(self.key, fp)      # the user vouched for it; remember
            self.status = "pinned"
            return
        stored = self.store.get(self.key)
        if stored is None:
            if self.confirm_new is not None and not self.confirm_new(self.key, fp):
                raise PinRejected(self.key, fp)
            self.store.set(self.key, fp)
            self.status = "new"
        elif normalize(stored) == normalize(fp):
            self.status = "match"
        elif self.replace:
            self.store.set(self.key, fp)
            self.status = "replaced"
        else:
            raise PinMismatch(self.key, stored, fp)


def wrap_and_check(tls_ctx: ssl.SSLContext, raw, server_hostname: str, pinner):
    """wrap_socket + pin check; closes the socket if the certificate is refused.
    pinner=None skips pinning (kept so existing callers and tests still work)."""
    conn = tls_ctx.wrap_socket(raw, server_hostname=server_hostname)
    if pinner is not None:
        try:
            pinner.check(conn)
        except BaseException:
            try:
                conn.close()
            except OSError:
                pass
            raise
    return conn
