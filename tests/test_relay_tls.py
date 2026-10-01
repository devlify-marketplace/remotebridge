"""TLS to the relay: the command phase (REGISTER/CONNECT/CHECK, i.e. the device token) is protected by TLS and
then dropped to plain TCP once a connection is parked or paired, so the end-to-end host<->viewer stream and the
relay's peeking at parked registrations are untouched. Real sockets on loopback and a real certificate
generated per run; nothing is faked."""
import datetime, ipaddress, os, socket, ssl, sys, tempfile, threading, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for sub in ("server", "desktop"):
    sys.path.insert(0, os.path.join(ROOT, sub))

import relay as relay_mod
import relay_client as rc

SECRET = "tls-secret"
TMP = tempfile.mkdtemp(prefix="rb-tls-")


def make_cert(name: str, san_dns=("localhost",), san_ip=("127.0.0.1",)):
    """Self-signed cert + key written to TMP; returns (certfile, keyfile)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    sans = [x509.DNSName(d) for d in san_dns] + [x509.IPAddress(ipaddress.ip_address(i)) for i in san_ip]
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .sign(key, hashes.SHA256()))
    cf, kf = os.path.join(TMP, name + ".crt"), os.path.join(TMP, name + ".key")
    with open(cf, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(kf, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return cf, kf


CERT, KEY = make_cert("relay")
OTHER_CERT, _ = make_cert("other")


def start_relay(tls=True, **kw):
    kw.setdefault("quiet", True); kw.setdefault("host", "127.0.0.1"); kw.setdefault("port", 0)
    kw.setdefault("connect_wait", 1.0); kw.setdefault("reap_interval", 0.2)
    kw.setdefault("handshake_timeout", 1.5)
    if tls:
        kw["tls_context"] = relay_mod.make_server_tls_context(CERT, KEY)
    r = relay_mod.RelayServer(**kw); r.start(); return r


def tls_relay(r, host="127.0.0.1"):
    return rc.Relay(host, r.tls_port, True)


def plain_relay(r):
    return rc.Relay("127.0.0.1", r.port, False)


class Base(unittest.TestCase):
    def setUp(self):
        rc.set_ca_file(CERT)
        self.relays = []

    def tearDown(self):
        rc.set_ca_file(None)
        for r in self.relays:
            r.stop()

    def relay(self, **kw):
        r = start_relay(**kw); self.relays.append(r); return r


class Parsing(unittest.TestCase):
    def test_tls_prefix_sets_flag_and_stays_a_tuple(self):
        a, b = rc.parse_relays("tls://a.example.com:6443, b.example.com:6000")
        self.assertEqual(a, ("a.example.com", 6443)); self.assertTrue(a.tls)
        self.assertEqual(b, ("b.example.com", 6000)); self.assertFalse(getattr(b, "tls", False))
        host, port = a
        self.assertEqual((host, port), ("a.example.com", 6443))

    def test_existing_relays_pass_through_parse(self):
        rel = rc.parse_relays("tls://x:1")
        self.assertTrue(rc.parse_relays(rel)[0].tls)
        self.assertEqual(rc.parse_relays([("h", 1)]), [("h", 1)])

    def test_bad_addresses(self):
        for bad in ("tls://nohost", "tls://:6443", "tls://h:abc"):
            with self.assertRaises(ValueError, msg=bad):
                rc.parse_relays(bad)


class RoundTrip(Base):
    def test_pairing_and_bytes_both_ways(self):
        r = self.relay()
        rel = tls_relay(r)
        host = rc.host_register_on(rel, "dev1")
        viewer = rc.viewer_connect_one(rel, "dev1")
        self.assertFalse(isinstance(host, ssl.SSLSocket) and host._sslobj is not None)   # dropped to plain
        host.settimeout(3); viewer.settimeout(3)
        viewer.sendall(b"hello host"); self.assertEqual(host.recv(64), b"hello host")
        host.sendall(b"hello viewer"); self.assertEqual(viewer.recv(64), b"hello viewer")
        host.close(); viewer.close()
        self.assertEqual(r.stats()["tls_connections"], 2)

    def test_end_to_end_tls_runs_through_a_tls_relay(self):
        """The real stack: relay TLS for the command phase, then host<->viewer TLS across the pair."""
        r = self.relay()
        rel = tls_relay(r)
        cf, kf = make_cert("e2e")
        host = rc.host_register_on(rel, "dev-e2e")
        viewer = rc.viewer_connect_one(rel, "dev-e2e")
        sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); sctx.load_cert_chain(cf, kf)
        cctx = ssl.create_default_context(cafile=cf)
        out = {}

        def serve():
            try:
                with sctx.wrap_socket(host, server_side=True) as s:
                    s.settimeout(5); out["got"] = s.recv(100); s.sendall(b"pong")
            except Exception as e:                 # surfaced by the assertion below
                out["err"] = repr(e)
        t = threading.Thread(target=serve); t.start()
        with cctx.wrap_socket(viewer, server_hostname="localhost") as c:
            c.settimeout(5); c.sendall(b"ping"); reply = c.recv(100)
        t.join(5)
        self.assertEqual(out.get("err"), None); self.assertEqual(out["got"], b"ping"); self.assertEqual(reply, b"pong")

    def test_host_wait_paired_over_tls(self):
        """host_wait_paired peeks at parked sockets with MSG_PEEK, which an SSL socket can't do - proves
        the registration really is plain TCP by the time it is parked."""
        r = self.relay()
        rel = tls_relay(r)
        result = {}

        def host():
            result["sock"], result["relay"] = rc.host_wait_paired([rel], "dev-w", poll=0.1)
        t = threading.Thread(target=host); t.start()
        time.sleep(0.4)
        viewer, _ = rc.viewer_connect([rel], "dev-w")
        viewer.sendall(b"x")      # a host notices the pairing by the viewer's first bytes (its TLS hello)
        t.join(5)
        self.assertIn("sock", result)
        result["sock"].settimeout(3); self.assertEqual(result["sock"].recv(1), b"x")
        self.assertEqual(result["relay"], rel)

    def test_a_dead_host_is_reaped_after_tls_registration(self):
        r = self.relay()
        rel = tls_relay(r)
        host = rc.host_register_on(rel, "dev-dead")
        time.sleep(0.2)
        host.close()
        deadline = time.time() + 3
        while time.time() < deadline and r.stats()["waiting"]:
            time.sleep(0.1)
        self.assertEqual(r.stats()["waiting"], 0)
        with self.assertRaises((ConnectionError, OSError)):
            rc.viewer_connect_one(rel, "dev-dead")

    def test_not_found_is_reported_over_tls(self):
        r = self.relay()
        with self.assertRaises(ConnectionError) as cm:
            rc.viewer_connect_one(tls_relay(r), "nobody")
        self.assertIn("not_found", str(cm.exception))

    def test_ping_and_stats_over_tls(self):
        r = self.relay()
        self.assertTrue(rc.ping(tls_relay(r)))


class Verification(Base):
    def test_untrusted_certificate_is_refused(self):
        r = self.relay()
        rc.set_ca_file(OTHER_CERT)
        with self.assertRaises(ssl.SSLError):
            rc.host_register_on(tls_relay(r), "dev")
        self.assertFalse(rc.ping(tls_relay(r)))

    def test_system_store_does_not_trust_a_self_signed_cert(self):
        r = self.relay()
        rc.set_ca_file(None)
        self.assertFalse(rc.ping(tls_relay(r)))

    def test_hostname_must_match(self):
        wrong_cert, wrong_key = make_cert("wrong", san_dns=("not-this-host.example",), san_ip=())
        r = self.relay(tls=False)
        r.stop(); self.relays.remove(r)
        r = relay_mod.RelayServer("127.0.0.1", 0, quiet=True, tls_port=0,
                                  tls_context=relay_mod.make_server_tls_context(wrong_cert, wrong_key))
        r.start(); self.relays.append(r)
        rc.set_ca_file(wrong_cert)              # trusted, but issued for another name
        with self.assertRaises(ssl.SSLCertVerificationError):
            rc._open(tls_relay(r), 2.0)

    def test_env_var_supplies_the_ca(self):
        r = self.relay()
        rc.set_ca_file(None)
        os.environ["REMOTEBRIDGE_RELAY_CA"] = CERT
        try:
            self.assertTrue(rc.ping(tls_relay(r)))
        finally:
            del os.environ["REMOTEBRIDGE_RELAY_CA"]


class Listeners(Base):
    def test_plaintext_cannot_speak_to_the_tls_port(self):
        r = self.relay()
        s = socket.create_connection(("127.0.0.1", r.tls_port), timeout=3)
        s.sendall(b"PING\n"); s.settimeout(3)
        try:
            data = s.recv(100)
        except OSError:
            data = b""
        self.assertNotIn(b"PONG", data)
        s.close()
        deadline = time.time() + 2
        while time.time() < deadline and not r.stats()["tls_failed"]:
            time.sleep(0.05)
        self.assertGreaterEqual(r.stats()["tls_failed"], 1)

    def test_tls_client_cannot_use_the_plain_port(self):
        r = self.relay()
        self.assertFalse(rc.ping(rc.Relay("127.0.0.1", r.port, True)))

    def test_plain_and_tls_listeners_work_side_by_side(self):
        r = self.relay()
        self.assertNotEqual(r.port, r.tls_port)
        self.assertTrue(rc.ping(plain_relay(r)))
        self.assertTrue(rc.ping(tls_relay(r)))
        h = rc.host_register_on(plain_relay(r), "mix")
        v = rc.viewer_connect_one(tls_relay(r), "mix")        # a TLS viewer reaches a plain host
        v.sendall(b"k"); h.settimeout(3); self.assertEqual(h.recv(1), b"k")

    def test_no_plain_closes_the_plaintext_listener(self):
        r = self.relay(plain=False)
        self.assertTrue(rc.ping(tls_relay(r)))
        self.assertIsNone(r._server)

    def test_plain_false_without_a_context_keeps_plain(self):
        r = self.relay(tls=False, plain=False)
        self.assertTrue(rc.ping(plain_relay(r)))

    def test_silent_tls_client_does_not_block_others(self):
        r = self.relay()
        idle = socket.create_connection(("127.0.0.1", r.tls_port), timeout=3)   # connects, never handshakes
        try:
            self.assertTrue(rc.ping(tls_relay(r)))
            idle.settimeout(4)
            self.assertEqual(idle.recv(10), b"")                                # relay hangs up on its own
        finally:
            idle.close()


class Authentication(Base):
    def test_token_checked_over_tls(self):
        r = self.relay(secrets=[SECRET])
        rel = tls_relay(r)
        good = relay_mod.make_token(SECRET, "pc1")
        self.assertEqual(rc.check_token(rel, "pc1", good), "ok")
        self.assertEqual(rc.check_token(rel, "pc1", "wrong"), "unauthorized")
        self.assertEqual(rc.check_token(rel, "pc1", ""), "unauthorized")

    def test_good_token_registers_bad_token_is_a_clear_error(self):
        r = self.relay(secrets=[SECRET])
        rel = tls_relay(r)
        sock = rc.host_register_on(rel, "pc1", token=relay_mod.make_token(SECRET, "pc1"))
        viewer = rc.viewer_connect_one(rel, "pc1")
        viewer.sendall(b"z"); sock.settimeout(3); self.assertEqual(sock.recv(1), b"z")
        with self.assertRaises(ConnectionError) as cm:
            rc.host_register_on(rel, "pc2", token="nope")
        self.assertIn("token", str(cm.exception))
        self.assertGreaterEqual(r.stats()["auth_refused"], 1)

    def test_revoked_and_expired_over_tls(self):
        r = self.relay(secrets=[SECRET], revoked=["pc3"])
        rel = tls_relay(r)
        self.assertEqual(rc.check_token(rel, "pc3", relay_mod.make_token(SECRET, "pc3")), "revoked")
        old = relay_mod.make_expiring_token(SECRET, "pc4", int(time.time()) - 600)
        self.assertEqual(rc.check_token(rel, "pc4", old), "expired")
        with self.assertRaises(ConnectionError):
            rc.host_register_on(rel, "pc3", token=relay_mod.make_token(SECRET, "pc3"))

    def test_tls_failures_do_not_count_toward_the_address_ban(self):
        r = self.relay(secrets=[SECRET], auth_fail_limit=3)
        for _ in range(6):
            s = socket.create_connection(("127.0.0.1", r.tls_port), timeout=2)
            s.sendall(b"garbage\n"); s.close()
        time.sleep(0.3)
        self.assertEqual(rc.check_token(tls_relay(r), "pc1", relay_mod.make_token(SECRET, "pc1")), "ok")

    def test_token_is_not_visible_on_the_wire(self):
        """A passive listener between host and relay sees only ciphertext."""
        r = self.relay(secrets=[SECRET])
        seen = bytearray()
        lsock = socket.socket(); lsock.bind(("127.0.0.1", 0)); lsock.listen(1)
        proxy_port = lsock.getsockname()[1]

        def proxy():
            c, _ = lsock.accept()
            up = socket.create_connection(("127.0.0.1", r.tls_port))
            def pump(a, b, record):
                try:
                    while True:
                        d = a.recv(4096)
                        if not d:
                            break
                        if record:
                            seen.extend(d)
                        b.sendall(d)
                except OSError:
                    pass
            threading.Thread(target=pump, args=(up, c, True), daemon=True).start()
            pump(c, up, True)
        threading.Thread(target=proxy, daemon=True).start()
        token = relay_mod.make_token(SECRET, "pc9")
        rel = rc.Relay("localhost", proxy_port, True)
        self.assertEqual(rc.check_token(rel, "pc9", token), "ok")
        self.assertGreater(len(seen), 0)
        self.assertNotIn(token.encode(), bytes(seen))
        self.assertNotIn(b"CHECK", bytes(seen))
        lsock.close()


class Failover(Base):
    def test_viewer_falls_back_from_a_dead_tls_relay_to_a_plain_one(self):
        dead = socket.socket(); dead.bind(("127.0.0.1", 0)); dead_port = dead.getsockname()[1]; dead.close()
        r = self.relay()
        h = rc.host_register_on(plain_relay(r), "fo")
        sock, used = rc.viewer_connect([rc.Relay("127.0.0.1", dead_port, True), plain_relay(r)], "fo")
        self.assertEqual(used, plain_relay(r))
        sock.close(); h.close()

    def test_relay_error_message_still_formats(self):
        with self.assertRaises(rc.RelayError) as cm:
            rc.viewer_connect([rc.Relay("127.0.0.1", 1, True)], "x", connect_timeout=0.5)
        self.assertIn("127.0.0.1:1", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
