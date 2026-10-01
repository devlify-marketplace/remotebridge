"""Phase 13 relay-authentication tests. Real sockets on loopback (see test_relay.py for why), and the
admin endpoint through Flask's real request pipeline."""
import os, socket, sys, tempfile, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for sub in ("server", "desktop", "admin"):
    sys.path.insert(0, os.path.join(ROOT, sub))

import relay as relay_mod
import relay_client as rc
import relayauth
import server, store

SECRET = "s3cret-A"


def start_relay(**kw):
    kw.setdefault("quiet", True); kw.setdefault("host", "127.0.0.1"); kw.setdefault("port", 0)
    kw.setdefault("connect_wait", 1.0); kw.setdefault("reap_interval", 0.2)
    r = relay_mod.RelayServer(**kw); r.start(); return r


def raw_register(addr, line, hold=0.0):
    s = socket.create_connection(addr, timeout=3); s.sendall(line.encode() + b"\n")
    if hold: time.sleep(hold)
    return s


def closed_by_peer(sock, within=2.0):
    sock.settimeout(within)
    try:
        return sock.recv(1) == b""
    except socket.timeout:
        return False
    except OSError:
        return True


class Tokens(unittest.TestCase):
    def test_relay_and_admin_agree(self):
        for dev in ("pc1", "office-pc", "weird id?"):
            self.assertEqual(relay_mod.make_token(SECRET, dev), relayauth.make_token(SECRET, dev))

    def test_token_is_per_device_and_per_secret(self):
        self.assertNotEqual(relay_mod.make_token(SECRET, "a"), relay_mod.make_token(SECRET, "b"))
        self.assertNotEqual(relay_mod.make_token(SECRET, "a"), relay_mod.make_token("other", "a"))

    def test_device_id_of(self):
        f = relay_mod.device_id_of
        self.assertEqual(f("devZ-video"), "devZ"); self.assertEqual(f("my-pc-audio"), "my-pc")
        self.assertEqual(f("my-pc"), "my-pc")            # 'pc' isn't a channel suffix
        self.assertEqual(f("-video"), "-video")


class AuthRelay(unittest.TestCase):
    def setUp(self):
        self.relay = start_relay(secrets=[SECRET])
        self.addr = ("127.0.0.1", self.relay.port)

    def tearDown(self):
        self.relay.stop()

    def tok(self, dev): return relay_mod.make_token(SECRET, dev)

    def test_valid_token_registers_and_pairs_all_channels(self):
        for label in ("video", "input", "control", "audio"):
            name = f"pc1-{label}"
            host = rc.host_register_on(self.addr, name, token=self.tok("pc1"))
            viewer = rc.viewer_connect_one(self.addr, name)      # viewers need no token
            viewer.sendall(b"hi"); host.settimeout(3)
            self.assertEqual(host.recv(2), b"hi")
            host.close(); viewer.close()
        self.assertEqual(self.relay.stats()["auth_refused"], 0)

    def test_missing_or_wrong_token_is_refused_and_not_registered(self):
        for line in ("REGISTER pc1-video", "REGISTER pc1-video deadbeef", "REGISTER pc1-video " + "0" * 64):
            s = raw_register(self.addr, line)
            self.assertTrue(closed_by_peer(s), line); s.close()
        self.assertEqual(self.relay.stats()["waiting"], 0)
        with self.assertRaises(ConnectionError):
            rc.viewer_connect_one(self.addr, "pc1-video")         # nothing registered -> not_found
        self.assertEqual(self.relay.stats()["auth_refused"], 3)

    def test_token_for_one_device_cannot_register_another(self):
        s = raw_register(self.addr, f"REGISTER victim-video {self.tok('attacker')}")
        self.assertTrue(closed_by_peer(s)); s.close()
        self.assertEqual(self.relay.stats()["waiting"], 0)

    def test_squatter_cannot_displace_the_real_host(self):
        real = rc.host_register_on(self.addr, "pc1-video", token=self.tok("pc1"))
        time.sleep(0.2)
        s = raw_register(self.addr, "REGISTER pc1-video nope"); self.assertTrue(closed_by_peer(s)); s.close()
        viewer = rc.viewer_connect_one(self.addr, "pc1-video")     # still paired with the REAL host
        viewer.sendall(b"x"); real.settimeout(3); self.assertEqual(real.recv(1), b"x")
        real.close(); viewer.close()

    def test_check_command(self):
        self.assertEqual(rc.check_token(self.addr, "pc1", self.tok("pc1")), "ok")
        self.assertEqual(rc.check_token(self.addr, "pc1-video", self.tok("pc1")), "ok")
        self.assertEqual(rc.check_token(self.addr, "pc1", None), "unauthorized")
        self.assertEqual(rc.check_token(self.addr, "pc1", self.tok("pc2")), "unauthorized")
        self.assertTrue(rc.check_token(("127.0.0.1", 1), "pc1", "x").startswith("unreachable"))

    def test_ping_and_connect_need_no_token(self):
        self.assertTrue(rc.ping(self.addr))


class SecretRotation(unittest.TestCase):
    def test_old_and_new_secret_both_accepted_during_rotation(self):
        r = start_relay(secrets=["new", "old"]); addr = ("127.0.0.1", r.port)
        try:
            self.assertEqual(rc.check_token(addr, "pc1", relay_mod.make_token("old", "pc1")), "ok")
            self.assertEqual(rc.check_token(addr, "pc1", relay_mod.make_token("new", "pc1")), "ok")
            self.assertEqual(rc.check_token(addr, "pc1", relay_mod.make_token("gone", "pc1")), "unauthorized")
        finally:
            r.stop()


class OpenRelay(unittest.TestCase):
    def test_no_secret_means_old_behavior_and_tokens_are_ignored(self):
        r = start_relay(); addr = ("127.0.0.1", r.port)
        try:
            for token in (None, "anything"):
                host = rc.host_register_on(addr, "pc1-video", token=token)
                viewer = rc.viewer_connect_one(addr, "pc1-video")
                viewer.sendall(b"ok"); host.settimeout(3); self.assertEqual(host.recv(2), b"ok")
                host.close(); viewer.close(); time.sleep(0.1)
            self.assertEqual(rc.check_token(addr, "pc1", None), "ok")
        finally:
            r.stop()


class Ban(unittest.TestCase):
    def test_repeated_failures_ban_the_address_then_it_expires(self):
        r = start_relay(secrets=[SECRET], auth_fail_limit=3, auth_ban=1.0); addr = ("127.0.0.1", r.port)
        try:
            for _ in range(3):
                s = raw_register(addr, "REGISTER pc1-video bad"); closed_by_peer(s); s.close()
            good = relay_mod.make_token(SECRET, "pc1")
            # banned: even a correct token is refused, for both commands
            s = raw_register(addr, f"REGISTER pc1-video {good}"); self.assertTrue(closed_by_peer(s)); s.close()
            self.assertTrue(rc.check_token(addr, "pc1", good).startswith("unreachable"))
            self.assertGreaterEqual(r.stats()["auth_banned"], 1)
            # viewers and health checks are unaffected by the ban
            self.assertTrue(rc.ping(addr))
            time.sleep(1.2)
            self.assertEqual(rc.check_token(addr, "pc1", good), "ok")
        finally:
            r.stop()

    def test_successes_never_count_toward_a_ban(self):
        r = start_relay(secrets=[SECRET], auth_fail_limit=3); addr = ("127.0.0.1", r.port)
        try:
            good = relay_mod.make_token(SECRET, "pc1")
            for _ in range(10):
                self.assertEqual(rc.check_token(addr, "pc1", good), "ok")
        finally:
            r.stop()


class AdminEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        server._DB_PATH = os.path.join(self.tmp, "admin.db")
        conn = store.init_db(server._DB_PATH)
        store.set_setting(conn, "flask_secret_key", "k"); server.app.secret_key = "k"
        store.set_setting(conn, "org_enrollment_key", "ORGKEY")
        conn.close()
        server.app.config["TESTING"] = True
        self.c = server.app.test_client()
        self._old = os.environ.get("RELAY_SECRET")

    def tearDown(self):
        if self._old is None: os.environ.pop("RELAY_SECRET", None)
        else: os.environ["RELAY_SECRET"] = self._old

    def enroll(self, dev):
        return self.c.post("/api/v1/enroll", json={"device_id": dev, "enrollment_key": "ORGKEY"}).get_json()["report_token"]

    def get(self, tok):
        return self.c.get("/api/v1/relay-token", headers={"Authorization": f"Bearer {tok}"} if tok else {})

    def test_token_issued_only_for_own_id_and_matches_relay(self):
        os.environ["RELAY_SECRET"] = SECRET
        a, b = self.enroll("pc-a"), self.enroll("pc-b")
        ja, jb = self.get(a).get_json(), self.get(b).get_json()
        self.assertEqual(ja["token"], relay_mod.make_token(SECRET, "pc-a"))
        self.assertEqual(jb["token"], relay_mod.make_token(SECRET, "pc-b"))
        self.assertNotEqual(ja["token"], jb["token"])
        self.assertEqual(self.get(a).headers.get("Cache-Control"), "no-store")

    def test_requires_a_valid_report_token(self):
        os.environ["RELAY_SECRET"] = SECRET
        self.assertEqual(self.get(None).status_code, 401)
        self.assertEqual(self.get("not-a-token").status_code, 401)

    def test_null_token_when_relay_is_open(self):
        os.environ.pop("RELAY_SECRET", None)
        self.assertIsNone(self.get(self.enroll("pc-a")).get_json()["token"])

    def test_first_of_a_rotation_list_is_issued(self):
        os.environ["RELAY_SECRET"] = "new, old"
        self.assertEqual(self.get(self.enroll("pc-a")).get_json()["token"], relay_mod.make_token("new", "pc-a"))

    def test_end_to_end_console_token_registers_on_relay(self):
        os.environ["RELAY_SECRET"] = SECRET
        tok = self.get(self.enroll("pc-a")).get_json()["token"]
        r = start_relay(secrets=[SECRET])
        try:
            self.assertEqual(rc.check_token(("127.0.0.1", r.port), "pc-a-video", tok), "ok")
        finally:
            r.stop()


if __name__ == "__main__":
    unittest.main()
