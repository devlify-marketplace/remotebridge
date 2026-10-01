"""Per-device relay revocation: relay (static / file / admin-console sources), the admin console's side of it,
and the two together. Real sockets on loopback, like test_relay_auth.py."""
import http.server, json, os, re, socket, sys, tempfile, threading, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for sub in ("server", "desktop", "admin", "tests"):
    sys.path.insert(0, os.path.join(ROOT, sub))

import relay as relay_mod
import relay_client as rc
import relayauth
import server, store
from test_admin_p12 import AdminBase
from test_relay_auth import start_relay, raw_register, closed_by_peer

SECRET = "s3cret-A"


def wait_until(fn, timeout=6.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if fn(): return True
        time.sleep(step)
    return fn()


class Helpers:
    def tok(self, dev, secret=SECRET): return relay_mod.make_token(secret, dev)

    def register(self, relay, dev, label="video"):
        return rc.host_register_on(("127.0.0.1", relay.port), f"{dev}-{label}", token=self.tok(dev))

    def check(self, relay, dev, token=None):
        return rc.check_token(("127.0.0.1", relay.port), f"{dev}-video", token if token is not None else self.tok(dev))

    def refused(self, relay, dev):
        """True if REGISTER with a valid token is closed by the relay and nothing ends up registered."""
        s = raw_register(("127.0.0.1", relay.port), f"REGISTER {dev}-video {self.tok(dev)}")
        gone = closed_by_peer(s, 2.0); s.close()
        return gone and relay.stats()["waiting"] == 0


class Basics(unittest.TestCase):
    def test_parse_revoked_lines(self):
        self.assertEqual(relay_mod.parse_revoked_lines("a\n  b  # trailing\n# whole line\n\n\tc\n"),
                         frozenset({"a", "b", "c"}))
        self.assertEqual(relay_mod.parse_revoked_lines(""), frozenset())

    def test_relay_and_admin_agree_on_the_bearer(self):
        self.assertEqual(relay_mod.revocation_bearer("x"), relayauth.revocation_bearer("x"))
        self.assertNotEqual(relay_mod.revocation_bearer("x"), relay_mod.revocation_bearer("y"))
        self.assertNotEqual(relay_mod.revocation_bearer("x"), relay_mod.make_token("x", ""),
                            "the list credential must not be a valid device token")


class StaticRevocation(unittest.TestCase, Helpers):
    def setUp(self):
        self.relay = start_relay(secrets=[SECRET], revoked=["lost-laptop"])

    def tearDown(self): self.relay.stop()

    def test_revoked_device_cannot_register_even_with_a_valid_token(self):
        self.assertTrue(self.refused(self.relay, "lost-laptop"))
        self.assertEqual(self.relay.stats()["revoked_refused"], 1)
        self.assertEqual(self.relay.stats()["auth_refused"], 0, "not an authentication failure")

    def test_other_devices_are_unaffected_including_lookalikes(self):
        for dev in ("lost-laptop2", "lost", "xlost-laptop", "lost-laptop-2"):
            with self.subTest(dev=dev):
                s = self.register(self.relay, dev); self.assertFalse(closed_by_peer(s, 0.3)); s.close()

    def test_revoked_retries_never_get_the_shared_address_banned(self):
        for _ in range(25):                        # far past the default limit of 10
            self.assertTrue(self.refused(self.relay, "lost-laptop"))
        s = self.register(self.relay, "colleague")  # same address, different (legit) device
        self.assertFalse(closed_by_peer(s, 0.3)); s.close()
        self.assertEqual(self.relay.stats()["auth_banned"], 0)

    def test_check_tells_a_token_holder_but_not_a_prober(self):
        self.assertEqual(self.check(self.relay, "lost-laptop"), "revoked")
        self.assertEqual(self.check(self.relay, "lost-laptop", token="0" * 64), "unauthorized",
                         "someone without the token must not learn the device is revoked")
        self.assertEqual(self.check(self.relay, "fine-pc"), "ok")

    def test_revoking_cuts_a_waiting_registration_and_blocks_the_viewer(self):
        s = self.register(self.relay, "pc1")
        self.assertTrue(wait_until(lambda: self.relay.stats()["waiting"] == 1))   # REGISTER is asynchronous
        self.assertEqual(self.relay.set_revoked(["pc1"]), 1)
        self.assertTrue(closed_by_peer(s, 2.0)); s.close()
        with self.assertRaises(ConnectionError):
            rc.viewer_connect_one(("127.0.0.1", self.relay.port), "pc1-video")

    def test_revoking_cuts_a_live_session_in_both_directions(self):
        host = self.register(self.relay, "pc1"); viewer = rc.viewer_connect_one(("127.0.0.1", self.relay.port), "pc1-video")
        viewer.sendall(b"ping"); host.settimeout(3); self.assertEqual(host.recv(4), b"ping")
        self.assertEqual(self.relay.stats()["active_sessions"], 1)
        self.relay.set_revoked(["pc1"])
        self.assertTrue(closed_by_peer(host, 2.0)); self.assertTrue(closed_by_peer(viewer, 2.0))
        self.assertTrue(wait_until(lambda: self.relay.stats()["active_sessions"] == 0))
        self.assertEqual(self.relay.stats()["revoked_cut"], 2)
        host.close(); viewer.close()

    def test_another_devices_live_session_survives(self):
        a = self.register(self.relay, "pc-a"); va = rc.viewer_connect_one(("127.0.0.1", self.relay.port), "pc-a-video")
        self.relay.set_revoked(["lost-laptop", "pc-b"])
        va.sendall(b"still here"); a.settimeout(3); self.assertEqual(a.recv(10), b"still here")
        a.close(); va.close()

    def test_restoring_lets_the_device_register_again(self):
        self.assertTrue(self.refused(self.relay, "lost-laptop"))
        self.relay.set_revoked([])
        s = self.register(self.relay, "lost-laptop"); self.assertFalse(closed_by_peer(s, 0.3)); s.close()
        self.assertEqual(self.check(self.relay, "lost-laptop"), "ok")

    def test_stats_report_the_count(self):
        self.assertEqual(self.relay.stats()["revoked_devices"], 1)

    def test_revoke_while_the_host_is_registering_does_not_leave_a_registration(self):
        """set_revoked() between _handle's check and the insert: the insert re-checks under the lock."""
        self.relay.set_revoked(["racer"])
        self.relay._handle_register(_FakeConn(), ("127.0.0.1", 1), "racer-video")
        self.assertEqual(self.relay.stats()["waiting"], 0)


class _FakeConn:
    closed = False
    def close(self): self.closed = True
    def shutdown(self, *a): pass


class FileSource(unittest.TestCase, Helpers):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); self.path = os.path.join(self.tmp, "revoked.txt")

    def tearDown(self): self.relay.stop()

    def write(self, text):
        with open(self.path, "w") as f: f.write(text)

    def test_loaded_before_serving_hot_reloaded_and_cuts_live_sessions(self):
        self.write("# lost devices\nold-pc   # sold\n")
        self.relay = start_relay(secrets=[SECRET], revoked_file=self.path, revoked_poll=0.2)
        self.assertTrue(self.refused(self.relay, "old-pc"), "a restart must not open a window")
        host = self.register(self.relay, "pc1"); viewer = rc.viewer_connect_one(("127.0.0.1", self.relay.port), "pc1-video")
        self.write("old-pc\npc1\n")
        self.assertTrue(wait_until(lambda: self.relay.revoked_devices() == {"old-pc", "pc1"}))
        self.assertTrue(closed_by_peer(host, 2.0)); self.assertTrue(closed_by_peer(viewer, 2.0))
        self.write("")                                # empty the file to restore everyone
        self.assertTrue(wait_until(lambda: not self.relay.revoked_devices()))
        s = self.register(self.relay, "old-pc"); self.assertFalse(closed_by_peer(s, 0.3)); s.close()
        host.close(); viewer.close()

    def test_missing_file_means_nobody_revoked_and_a_later_delete_keeps_the_last_list(self):
        self.relay = start_relay(secrets=[SECRET], revoked_file=self.path, revoked_poll=0.2)
        self.assertEqual(self.relay.revoked_devices(), frozenset())
        self.write("pc1\n"); self.assertTrue(wait_until(lambda: "pc1" in self.relay.revoked_devices()))
        os.remove(self.path); time.sleep(0.6)
        self.assertEqual(self.relay.revoked_devices(), {"pc1"}, "vanishing file must not un-revoke")

    def test_unreadable_file_keeps_the_previous_list(self):
        self.write("pc1\n")
        self.relay = start_relay(secrets=[SECRET], revoked_file=self.path, revoked_poll=0.2)
        os.remove(self.path); os.mkdir(self.path)     # now a directory: open() raises OSError
        time.sleep(0.6)
        self.assertEqual(self.relay.revoked_devices(), {"pc1"})
        os.rmdir(self.path)


class FakeConsole:
    """A stand-in for the admin console's /api/v1/relay/revoked: checks the bearer, serves a mutable list."""
    def __init__(self, secrets, revoked):
        outer = self; self.secrets, self.revoked, self.status, self.hits = secrets, list(revoked), 200, 0
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                outer.hits += 1
                ok = any(self.headers.get("Authorization") == "Bearer " + relay_mod.revocation_bearer(s)
                         for s in outer.secrets)
                code = outer.status if ok else 401
                body = json.dumps({"revoked": outer.revoked} if code == 200 else {"error": "no"}).encode()
                self.send_response(code); self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/v1/relay/revoked"

    def stop(self): self.httpd.shutdown(); self.httpd.server_close()


class UrlSource(unittest.TestCase, Helpers):
    def setUp(self): self.console = None; self.relay = None

    def tearDown(self):
        if self.relay: self.relay.stop()
        if self.console: self.console.stop()

    def start(self, console_secrets, relay_secrets, revoked):
        self.console = FakeConsole(console_secrets, revoked)
        self.relay = start_relay(secrets=relay_secrets, revoked_url=self.console.url, revoked_poll=0.2)

    def test_polls_the_console_and_applies_changes(self):
        self.start([SECRET], [SECRET], ["pc1"])
        self.assertTrue(wait_until(lambda: self.relay.revoked_devices() == {"pc1"}))
        self.assertTrue(self.refused(self.relay, "pc1"))
        self.console.revoked = []
        self.assertTrue(wait_until(lambda: not self.relay.revoked_devices()))

    def test_console_outage_or_error_keeps_the_last_list(self):
        self.start([SECRET], [SECRET], ["pc1"])
        self.assertTrue(wait_until(lambda: "pc1" in self.relay.revoked_devices()))
        self.console.status = 500; time.sleep(0.7)
        self.assertEqual(self.relay.revoked_devices(), {"pc1"})
        self.console.stop(); time.sleep(0.7)
        self.assertEqual(self.relay.revoked_devices(), {"pc1"}, "an outage must never un-revoke a device")
        self.console = None

    def test_wrong_secret_changes_nothing(self):
        self.start(["some-other-secret"], [SECRET], ["pc1"])
        time.sleep(0.8)
        self.assertEqual(self.relay.revoked_devices(), frozenset())
        self.assertGreater(self.console.hits, 0)

    def test_secret_rotation_tries_each_secret(self):
        self.start(["old"], ["new", "old"], ["pc1"])      # console still on the old secret only
        self.assertTrue(wait_until(lambda: self.relay.revoked_devices() == {"pc1"}))

    def test_sources_combine(self):
        self.start([SECRET], [SECRET], ["from-console"])
        self.relay.set_revoked(["from-static"], "static")
        self.assertTrue(wait_until(lambda: self.relay.revoked_devices() == {"from-console", "from-static"}))
        self.relay.set_revoked([], "static")
        time.sleep(0.4)
        self.assertEqual(self.relay.revoked_devices(), {"from-console"})

    def test_rejects_malformed_responses(self):
        self.start([SECRET], [SECRET], [])
        self.console.revoked = "not-a-list"
        time.sleep(0.7)
        self.assertEqual(self.relay.revoked_devices(), frozenset())


class AdminSide(AdminBase, Helpers):
    def setUp(self):
        super().setUp()
        self._old = os.environ.get("RELAY_SECRET"); os.environ["RELAY_SECRET"] = SECRET

    def tearDown(self):
        if self._old is None: os.environ.pop("RELAY_SECRET", None)
        else: os.environ["RELAY_SECRET"] = self._old

    def revoked_list(self, bearer=None):
        h = {"Authorization": "Bearer " + bearer} if bearer else {}
        return self.c.get("/api/v1/relay/revoked", headers=h)

    def relay_token(self, report_token):
        return self.c.get("/api/v1/relay-token", headers={"Authorization": f"Bearer {report_token}"})

    def post_ui(self, client, path, page):
        tok = re.search(r'name="csrf_token" value="([^"]+)"', client.get(page).get_data(as_text=True)).group(1)
        return client.post(path, data={"csrf_token": tok})

    def test_list_endpoint_needs_the_relay_credential(self):
        self.assertEqual(self.revoked_list().status_code, 401)
        self.assertEqual(self.revoked_list("nope").status_code, 401)
        self.assertEqual(self.revoked_list(SECRET).status_code, 401, "the raw secret is not the credential")
        self.assertEqual(self.revoked_list(relayauth.revocation_bearer(SECRET)).get_json(), {"revoked": []})
        self.assertEqual(self.revoked_list(relayauth.revocation_bearer(SECRET)).headers["Cache-Control"], "no-store")

    def test_a_device_report_token_cannot_read_the_list(self):
        tok = self.enroll("pc1")
        self.assertEqual(self.revoked_list(tok).status_code, 401)

    def test_unavailable_when_the_relay_is_open(self):
        os.environ.pop("RELAY_SECRET", None)
        self.assertEqual(self.revoked_list(relayauth.revocation_bearer("")).status_code, 403)

    def test_any_secret_in_a_rotation_list_is_accepted(self):
        os.environ["RELAY_SECRET"] = "new, old"
        for s in ("new", "old"):
            self.assertEqual(self.revoked_list(relayauth.revocation_bearer(s)).status_code, 200)

    def test_revoking_and_restoring_through_the_ui(self):
        a, b = self.enroll("pc-a"), self.enroll("pc-b")
        self.login()
        self.assertIn("Revoke", self.c.get("/devices").get_data(as_text=True))
        r = self.post_ui(self.c, "/devices/pc-a/revoke", "/devices"); self.assertEqual(r.status_code, 302)
        self.assertEqual(self.revoked_list(relayauth.revocation_bearer(SECRET)).get_json(), {"revoked": ["pc-a"]})
        self.assertIn("Relay access revoked", self.c.get("/devices").get_data(as_text=True))
        self.assertEqual(self.relay_token(a).status_code, 403, "a revoked device can't fetch a fresh token")
        self.assertEqual(self.relay_token(b).status_code, 200)
        r = self.post_ui(self.c, "/devices/pc-a/restore", "/devices"); self.assertEqual(r.status_code, 302)
        self.assertEqual(self.revoked_list(relayauth.revocation_bearer(SECRET)).get_json(), {"revoked": []})
        self.assertEqual(self.relay_token(a).status_code, 200)

    def test_unknown_device_is_404(self):
        self.login()
        self.assertEqual(self.post_ui(self.c, "/devices/ghost/revoke", "/devices").status_code, 404)

    def test_auditors_cannot_revoke(self):
        self.enroll("pc-a")
        eve = self.login("eve", "pw-eve", client=server.app.test_client())
        r = self.post_ui(eve, "/devices/pc-a/revoke", "/devices")
        self.assertIn(r.status_code, (302, 403)); self.assertEqual(store.list_revoked_device_ids(self.conn), [])
        self.assertNotIn("/revoke", eve.get("/devices").get_data(as_text=True))

    def test_revoke_needs_a_csrf_token(self):
        self.enroll("pc-a"); self.login()
        self.assertEqual(self.c.post("/devices/pc-a/revoke").status_code, 400)
        self.assertEqual(store.list_revoked_device_ids(self.conn), [])

    def test_existing_database_gets_the_new_column(self):
        import sqlite3
        path = os.path.join(self.tmp, "old.db")
        c = sqlite3.connect(path)
        c.executescript("CREATE TABLE devices (id INTEGER PRIMARY KEY, device_id TEXT UNIQUE, display_name TEXT, "
                        "group_id INTEGER, report_token TEXT, enrolled_at TEXT, last_seen_at TEXT);")
        c.commit(); c.close()
        conn = store.init_db(path)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(devices)")}
        self.assertIn("revoked_at", cols)


class ConsoleToRelay(AdminBase, Helpers):
    """The whole chain: click Revoke in the console -> a relay polling it cuts a live session."""
    def setUp(self):
        super().setUp()
        from werkzeug.serving import make_server
        self._old = os.environ.get("RELAY_SECRET"); os.environ["RELAY_SECRET"] = SECRET
        self.httpd = make_server("127.0.0.1", 0, server.app, threaded=True)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{self.httpd.server_port}/api/v1/relay/revoked"
        self.relay = start_relay(secrets=[SECRET], revoked_url=url, revoked_poll=0.2)

    def tearDown(self):
        self.relay.stop(); self.httpd.shutdown()
        if self._old is None: os.environ.pop("RELAY_SECRET", None)
        else: os.environ["RELAY_SECRET"] = self._old

    def test_revoke_in_console_cuts_the_live_session(self):
        self.enroll("pc1")
        host = self.register(self.relay, "pc1"); viewer = rc.viewer_connect_one(("127.0.0.1", self.relay.port), "pc1-video")
        viewer.sendall(b"x"); host.settimeout(3); self.assertEqual(host.recv(1), b"x")
        self.login()
        tok = re.search(r'name="csrf_token" value="([^"]+)"', self.c.get("/devices").get_data(as_text=True)).group(1)
        self.assertEqual(self.c.post("/devices/pc1/revoke", data={"csrf_token": tok}).status_code, 302)
        self.assertTrue(closed_by_peer(host, 4.0)); self.assertTrue(closed_by_peer(viewer, 4.0))
        self.assertTrue(self.refused(self.relay, "pc1"))
        host.close(); viewer.close()


if __name__ == "__main__":
    unittest.main()
