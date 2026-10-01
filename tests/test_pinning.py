"""Certificate pinning: real TLS handshakes against real (self-signed) certificates."""
import json, os, shutil, socket, ssl, subprocess, sys, tempfile, threading, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "desktop"))
import pinning


def make_cert(d, name):
    cert, key = os.path.join(d, f"{name}.pem"), os.path.join(d, f"{name}.key")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                    "-days", "2", "-subj", f"/CN={name}"], check=True, capture_output=True)
    return cert, key


class TlsServer:
    """Accepts connections, completes the TLS handshake, then holds them open."""
    def __init__(self, cert, key):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)
        self.sock = socket.socket(); self.sock.bind(("127.0.0.1", 0)); self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.conns = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                raw, _ = self.sock.accept()
                self.conns.append(self.ctx.wrap_socket(raw, server_side=True))
            except (OSError, ssl.SSLError):
                if self.sock.fileno() == -1: return

    def close(self):
        self.sock.close()
        for c in self.conns:
            try: c.close()
            except OSError: pass


def client_ctx():
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); c.check_hostname = False; c.verify_mode = ssl.CERT_NONE
    return c


@unittest.skipUnless(shutil.which("openssl"), "needs the openssl CLI")
class PinningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pin-")
        cls.cert_a, cls.key_a = make_cert(cls.tmp, "hostA")
        cls.cert_b, cls.key_b = make_cert(cls.tmp, "hostB")
        cls.fp_a = pinning.fingerprint_pem_file(cls.cert_a)
        cls.fp_b = pinning.fingerprint_pem_file(cls.cert_b)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.store_path = os.path.join(self.tmp, f"kh-{self.id().split('.')[-1]}.json")
        self.store = pinning.PinStore(self.store_path)
        self.servers, self.clients = [], []

    def tearDown(self):
        for c in self.clients:
            try: c.close()
            except OSError: pass
        for s in self.servers: s.close()

    def serve(self, which="a"):
        srv = TlsServer(*((self.cert_a, self.key_a) if which == "a" else (self.cert_b, self.key_b)))
        self.servers.append(srv)
        return srv

    def dial(self, srv, pinner):
        raw = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        conn = pinning.wrap_and_check(client_ctx(), raw, "127.0.0.1", pinner)
        self.clients.append(conn)
        return conn

    def pinner(self, **kw):
        return pinning.Pinner(self.store, "127.0.0.1:5000", **kw)

    # --- trust on first use ---
    def test_fingerprint_matches_openssl(self):
        out = subprocess.run(["openssl", "x509", "-in", self.cert_a, "-noout", "-fingerprint", "-sha256"],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.strip().split("=", 1)[1], self.fp_a)

    def test_first_use_is_remembered_then_matches(self):
        p1 = self.pinner(); self.dial(self.serve("a"), p1)
        self.assertEqual((p1.status, p1.fingerprint), ("new", self.fp_a))
        p2 = self.pinner(); self.dial(self.serve("a"), p2)
        self.assertEqual(p2.status, "match")

    def test_changed_certificate_is_refused_and_not_stored(self):
        self.dial(self.serve("a"), self.pinner())
        with self.assertRaises(pinning.PinMismatch) as cm:
            self.dial(self.serve("b"), self.pinner())
        self.assertEqual((cm.exception.expected, cm.exception.actual), (self.fp_a, self.fp_b))
        self.assertEqual(self.store.get("127.0.0.1:5000"), self.fp_a, "attacker's cert must not replace the pin")

    def test_replace_pin_accepts_and_overwrites(self):
        self.dial(self.serve("a"), self.pinner())
        p = self.pinner(replace=True); self.dial(self.serve("b"), p)
        self.assertEqual(p.status, "replaced")
        self.assertEqual(self.store.get("127.0.0.1:5000"), self.fp_b)

    def test_user_declining_first_use_stores_nothing(self):
        with self.assertRaises(pinning.PinRejected):
            self.dial(self.serve("a"), self.pinner(confirm_new=lambda k, fp: False))
        self.assertIsNone(self.store.get("127.0.0.1:5000"))
        self.assertFalse(os.path.exists(self.store_path))

    def test_confirm_callback_sees_the_fingerprint(self):
        seen = []
        self.dial(self.serve("a"), self.pinner(confirm_new=lambda k, fp: seen.append((k, fp)) or True))
        self.assertEqual(seen, [("127.0.0.1:5000", self.fp_a)])

    def test_confirm_not_asked_when_already_pinned(self):
        self.dial(self.serve("a"), self.pinner())
        self.dial(self.serve("a"), self.pinner(confirm_new=lambda k, fp: self.fail("asked again")))

    # --- explicit --pin ---
    def test_expected_pin_accepts_right_cert_in_any_format(self):
        for fmt in (self.fp_a, self.fp_a.lower(), self.fp_a.replace(":", ""), "sha256:" + self.fp_a):
            with self.subTest(fmt=fmt[:12]):
                p = self.pinner(expected=fmt); self.dial(self.serve("a"), p)
                self.assertEqual(p.status, "pinned")

    def test_expected_pin_rejects_wrong_cert_without_asking_or_storing(self):
        with self.assertRaises(pinning.PinMismatch):
            self.dial(self.serve("b"), self.pinner(expected=self.fp_a, confirm_new=lambda k, f: self.fail("asked")))
        self.assertIsNone(self.store.get("127.0.0.1:5000"))

    def test_expected_pin_overrides_a_stale_stored_pin(self):
        self.dial(self.serve("a"), self.pinner())
        p = self.pinner(expected=self.fp_b); self.dial(self.serve("b"), p)   # user verified the new cert
        self.assertEqual(self.store.get("127.0.0.1:5000"), self.fp_b)

    def test_malformed_expected_pin(self):
        for bad in ("", "abc", "zz" * 32, self.fp_a[:-3]):
            if bad == "": continue   # empty means "no --pin"
            with self.subTest(bad=bad[:8]), self.assertRaises(ValueError):
                self.pinner(expected=bad)

    # --- channels must agree ---
    def test_every_channel_must_present_the_same_certificate(self):
        p = self.pinner()
        self.dial(self.serve("a"), p)                         # video: real host
        with self.assertRaises(pinning.PinMismatch):          # input: someone else
            self.dial(self.serve("b"), p)

    def test_mismatch_closes_the_socket(self):
        self.dial(self.serve("a"), self.pinner())
        srv = self.serve("b")
        raw = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        with self.assertRaises(pinning.PinMismatch):
            pinning.wrap_and_check(client_ctx(), raw, "x", self.pinner())
        self.assertEqual(raw.fileno(), -1)

    # --- store ---
    def test_keys_are_independent_and_device_keys_differ_from_direct(self):
        self.store.set(pinning.key_device("office-pc"), self.fp_a)
        self.assertIsNone(self.store.get(pinning.key_direct("office-pc", 5000)))
        self.assertEqual(pinning.key_direct(" Example.COM ", "5000"), "example.com:5000")

    def test_corrupt_store_fails_closed(self):
        open(self.store_path, "w").write("{ not json")
        with self.assertRaises(pinning.PinStoreError):
            self.dial(self.serve("a"), self.pinner())
        open(self.store_path, "w").write('["wrong shape"]')
        with self.assertRaises(pinning.PinStoreError):
            self.store.get("x")

    def test_store_file_is_private_and_atomic(self):
        self.store.set("k", self.fp_a)
        if os.name == "posix":
            self.assertEqual(os.stat(self.store_path).st_mode & 0o777, 0o600)
        self.assertEqual([f for f in os.listdir(os.path.dirname(self.store_path)) if f.startswith(".known_hosts-")], [])
        self.assertEqual(json.load(open(self.store_path, encoding="utf-8"))["hosts"]["k"]["fingerprint"], self.fp_a)

    def test_forget(self):
        self.store.set("k", self.fp_a)
        self.assertTrue(self.store.forget("k")); self.assertFalse(self.store.forget("k"))
        self.assertIsNone(self.store.get("k"))

    def test_first_seen_survives_replacement(self):
        self.store.set("k", self.fp_a)
        first = json.load(open(self.store_path, encoding="utf-8"))["hosts"]["k"]["first_seen"]
        self.store.set("k", self.fp_b)
        self.assertEqual(json.load(open(self.store_path, encoding="utf-8"))["hosts"]["k"]["first_seen"], first)

    def test_no_pinner_skips_pinning(self):
        self.dial(self.serve("a"), None)    # backward-compatible path


if __name__ == "__main__":
    unittest.main()
