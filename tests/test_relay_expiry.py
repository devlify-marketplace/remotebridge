"""Short-lived relay tokens: the token format (relay and admin must agree), what the relay does with an
expiring token (accept, refuse, close waiting registrations, leave live sessions alone), how the host keeps
its token fresh (TokenProvider + renewing a registration in place), and what the admin console issues.
Real sockets on loopback and Flask's real request pipeline; nothing is faked except the clock, where a test
says so."""
import os, socket, subprocess, sys, tempfile, threading, time, unittest

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


def closed_by_peer(sock, within=2.0):
    sock.settimeout(within)
    try:
        return sock.recv(1) == b""
    except socket.timeout:
        return False
    except OSError:
        return True


def exp_token(dev, in_seconds, secret=SECRET):
    return relay_mod.make_expiring_token(secret, dev, int(time.time()) + in_seconds)


class Format(unittest.TestCase):
    def test_relay_and_admin_agree(self):
        for dev in ("pc1", "office-pc", "weird id?"):
            for exp in (1, 1900000000, 4102444800):
                self.assertEqual(relay_mod.make_expiring_token(SECRET, dev, exp),
                                 relayauth.make_expiring_token(SECRET, dev, exp))

    def test_shape_and_parse(self):
        t = relay_mod.make_expiring_token(SECRET, "pc1", 1900000000)
        self.assertTrue(t.startswith("1900000000."))
        self.assertEqual(relay_mod.parse_token(t), (1900000000, t.split(".")[1]))
        self.assertEqual(relay_mod.token_expiry(t), 1900000000)
        legacy = relay_mod.make_token(SECRET, "pc1")
        self.assertEqual(relay_mod.parse_token(legacy), (None, legacy))
        self.assertIsNone(relay_mod.token_expiry(legacy))

    def test_malformed_tokens_do_not_parse(self):
        for bad in (".abc", "123.", "12.3.4", "x.abc", "-5.abc", "١٢٣.abc", "9" * 13 + ".abc", "1e9.abc", ""):
            r = relay_mod.parse_token(bad)
            self.assertTrue(r is None or r[0] is None, bad)       # "" is legacy-shaped (and then simply wrong)

    def test_signature_covers_device_expiry_and_secret(self):
        base = relay_mod.make_expiring_token(SECRET, "a", 1000)
        self.assertNotEqual(base, relay_mod.make_expiring_token(SECRET, "b", 1000))
        self.assertNotEqual(base, relay_mod.make_expiring_token(SECRET, "a", 1001))
        self.assertNotEqual(base, relay_mod.make_expiring_token("other", "a", 1000))
        # The same signature with a later expiry pasted in front is not a valid token.
        sig = base.split(".")[1]
        r = start_relay(secrets=[SECRET])
        try:
            self.assertEqual(r._token_status("a-video", f"9999999999.{sig}")[0], "bad")
        finally:
            r.stop()

    def test_expiring_and_legacy_tokens_are_different_things(self):
        self.assertNotEqual(relay_mod.make_expiring_token(SECRET, "a", 5).split(".")[1],
                            relay_mod.make_token(SECRET, "a"))


class Status(unittest.TestCase):
    """_token_status, with the clock passed in."""
    def setUp(self):
        self.r = relay_mod.RelayServer("127.0.0.1", 0, quiet=True, secrets=[SECRET], expiry_leeway=30.0)

    def st(self, name, token, now=None):
        return self.r._token_status(name, token, now)

    def test_ok_before_expiry_and_expired_after_leeway(self):
        t = relay_mod.make_expiring_token(SECRET, "pc1", 10_000)
        self.assertEqual(self.st("pc1-video", t, 9_999), ("ok", 10_000))
        self.assertEqual(self.st("pc1-video", t, 10_000)[0], "ok", "inside the leeway")
        self.assertEqual(self.st("pc1-video", t, 10_029)[0], "ok")
        self.assertEqual(self.st("pc1-video", t, 10_030), ("expired", 10_000))

    def test_one_token_covers_all_four_channels(self):
        t = relay_mod.make_expiring_token(SECRET, "pc1", 10_000)
        for label in ("video", "input", "control", "audio"):
            self.assertEqual(self.st(f"pc1-{label}", t, 100)[0], "ok")

    def test_wrong_device_secret_or_tampering_is_bad_not_expired(self):
        t = relay_mod.make_expiring_token(SECRET, "pc1", 10_000)
        self.assertEqual(self.st("pc2-video", t, 100)[0], "bad")
        self.assertEqual(self.st("pc1-video", relay_mod.make_expiring_token("nope", "pc1", 10_000), 100)[0], "bad")
        # a forged claim far in the future fails; so does one that makes an expired token look current
        sig = t.split(".")[1]
        self.assertEqual(self.st("pc1-video", f"99999999999.{sig}", 100)[0], "bad")
        self.assertEqual(self.st("pc1-video", f"20000.{sig}", 100)[0], "bad")
        # an expired-and-forged token must not reveal "expired" (that would be an oracle for probing)
        self.assertEqual(self.st("pc1-video", "5.deadbeef", 100)[0], "bad")

    def test_expired_is_only_reported_for_a_genuine_signature(self):
        t = relay_mod.make_expiring_token(SECRET, "pc1", 10)
        self.assertEqual(self.st("pc1-video", t, 100_000)[0], "expired")
        self.assertEqual(self.st("pc2-video", t, 100_000)[0], "bad")

    def test_legacy_tokens_accepted_unless_expiry_required(self):
        legacy = relay_mod.make_token(SECRET, "pc1")
        self.assertEqual(self.st("pc1-video", legacy), ("ok", None))
        strict = relay_mod.RelayServer("127.0.0.1", 0, quiet=True, secrets=[SECRET], require_expiry=True)
        self.assertEqual(strict._token_status("pc1-video", legacy), ("legacy", None))
        self.assertEqual(strict._token_status("pc1-video", relay_mod.make_token("nope", "pc1"))[0], "bad")
        good = relay_mod.make_expiring_token(SECRET, "pc1", int(time.time()) + 60)
        self.assertEqual(strict._token_status("pc1-video", good)[0], "ok")

    def test_secret_rotation_works_for_expiring_tokens(self):
        r = relay_mod.RelayServer("127.0.0.1", 0, quiet=True, secrets=["new", "old"])
        for sec in ("new", "old"):
            self.assertEqual(r._token_status("pc1-video", relay_mod.make_expiring_token(sec, "pc1", 2**33), 5)[0], "ok")
        self.assertEqual(r._token_status("pc1-video", relay_mod.make_expiring_token("gone", "pc1", 2**33), 5)[0], "bad")

    def test_open_relay_ignores_tokens_entirely(self):
        r = relay_mod.RelayServer("127.0.0.1", 0, quiet=True)
        self.assertEqual(r._token_status("pc1-video", "")[0], "ok")
        self.assertEqual(r._token_status("pc1-video", "garbage")[0], "ok")


class LiveRelay(unittest.TestCase):
    def setUp(self):
        self.relays = []

    def tearDown(self):
        for r in self.relays: r.stop()

    def relay(self, **kw):
        r = start_relay(secrets=[SECRET], **kw); self.relays.append(r)
        return r, ("127.0.0.1", r.port)

    def test_expiring_token_registers_and_pairs(self):
        r, addr = self.relay()
        host = rc.host_register_on(addr, "pc1-video", token=exp_token("pc1", 60))
        viewer = rc.viewer_connect_one(addr, "pc1-video")
        viewer.sendall(b"hi"); host.settimeout(3)
        self.assertEqual(host.recv(2), b"hi")
        self.assertEqual(r.stats()["auth_refused"], 0)

    def test_expired_token_is_refused_silently_and_does_not_register(self):
        r, addr = self.relay(expiry_leeway=0)
        host = rc.host_register_on(addr, "pc1-video", token=exp_token("pc1", -5))
        self.assertTrue(closed_by_peer(host))
        st = r.stats()
        self.assertEqual((st["waiting"], st["token_expired"]), (0, 1))
        with self.assertRaises(Exception):
            rc.viewer_connect_one(addr, "pc1-video")

    def test_check_reports_expired_ok_unauthorized_and_legacy(self):
        r, addr = self.relay(expiry_leeway=0)
        self.assertEqual(rc.check_token(addr, "pc1", exp_token("pc1", 60)), "ok")
        self.assertEqual(rc.check_token(addr, "pc1", exp_token("pc1", -5)), "expired")
        self.assertEqual(rc.check_token(addr, "pc1", exp_token("pc2", -5)), "unauthorized", "wrong device: no oracle")
        self.assertEqual(rc.check_token(addr, "pc1", exp_token("pc1", -5, secret="other")), "unauthorized")
        self.assertEqual(rc.check_token(addr, "pc1", relay_mod.make_token(SECRET, "pc1")), "ok", "legacy still fine")
        strict, saddr = self.relay(require_expiry=True)
        self.assertEqual(rc.check_token(saddr, "pc1", relay_mod.make_token(SECRET, "pc1")), "unauthorized")
        self.assertEqual(rc.check_token(saddr, "pc1", exp_token("pc1", 60)), "ok")
        self.assertEqual(strict.stats()["legacy_refused"], 1)

    def test_require_expiry_refuses_a_legacy_register(self):
        r, addr = self.relay(require_expiry=True)
        host = rc.host_register_on(addr, "pc1-video", token=relay_mod.make_token(SECRET, "pc1"))
        self.assertTrue(closed_by_peer(host))
        self.assertEqual(r.stats()["waiting"], 0)

    def test_expired_tokens_do_not_get_an_address_banned_but_bad_ones_do(self):
        r, addr = self.relay(expiry_leeway=0, auth_fail_limit=3, auth_ban=60)
        for _ in range(6):                           # a stale host retrying in a loop
            closed_by_peer(rc.host_register_on(addr, "pc1-video", token=exp_token("pc1", -5)), 0.5)
        good = rc.host_register_on(addr, "pc1-video", token=exp_token("pc1", 60))
        time.sleep(0.3)
        self.assertEqual(r.stats()["waiting"], 1, "stale-token retries must not ban the address")
        good.close()
        for _ in range(3):
            closed_by_peer(rc.host_register_on(addr, "pc1-video", token="1.nope"), 0.5)
        late = rc.host_register_on(addr, "pc1-video", token=exp_token("pc1", 60))
        self.assertTrue(closed_by_peer(late), "guessing tokens still earns a ban")

    def test_waiting_registration_is_closed_when_its_token_expires(self):
        r, addr = self.relay(expiry_leeway=0)
        host = rc.host_register_on(addr, "pc1-video", token=exp_token("pc1", 2))
        deadline = time.time() + 1.0
        while r.stats()["waiting"] != 1 and time.time() < deadline: time.sleep(0.05)
        self.assertEqual(r.stats()["waiting"], 1)
        self.assertFalse(closed_by_peer(host, 0.5), "still registered while the token is good")
        self.assertTrue(closed_by_peer(host, 4.0), "closed by the relay after expiry")
        st = r.stats()
        self.assertEqual((st["waiting"], st["expired_dropped"]), (0, 1))
        with self.assertRaises(Exception):
            rc.viewer_connect_one(addr, "pc1-video")

    def test_non_expiring_registration_is_never_swept(self):
        r, addr = self.relay(expiry_leeway=0)
        host = rc.host_register_on(addr, "pc1-video", token=relay_mod.make_token(SECRET, "pc1"))
        time.sleep(0.8)
        self.assertEqual(r.expire_registrations(now=time.time() + 10 ** 9), 0)
        self.assertEqual(r.stats()["waiting"], 1); host.close()

    def test_replacing_a_registration_does_not_inherit_the_old_expiry(self):
        r, addr = self.relay(expiry_leeway=0, reap_interval=60)
        old = rc.host_register_on(addr, "pc1-video", token=exp_token("pc1", 1))
        time.sleep(0.3)
        new = rc.host_register_on(addr, "pc1-video", token=exp_token("pc1", 3600))    # the renewal
        time.sleep(0.3)
        self.assertEqual(r.expire_registrations(now=time.time() + 5), 0, "old entry is stale, new one is good")
        self.assertEqual(r.stats()["waiting"], 1)
        viewer = rc.viewer_connect_one(addr, "pc1-video"); viewer.sendall(b"k"); new.settimeout(3)
        self.assertEqual(new.recv(1), b"k")
        # and renewing with a NON-expiring token drops the expiry bookkeeping entirely
        a = rc.host_register_on(addr, "pc2-video", token=exp_token("pc2", 1)); time.sleep(0.2)
        b = rc.host_register_on(addr, "pc2-video", token=relay_mod.make_token(SECRET, "pc2")); time.sleep(0.2)
        self.assertEqual(r.expire_registrations(now=time.time() + 10 ** 6), 0)
        self.assertNotIn("pc2-video", r._expiry)
        old.close(); a.close(); b.close()

    def test_a_paired_registration_is_not_swept_and_live_sessions_survive_expiry(self):
        r, addr = self.relay(expiry_leeway=0, reap_interval=60)
        host = rc.host_register_on(addr, "pc1-video", token=exp_token("pc1", 600))
        viewer = rc.viewer_connect_one(addr, "pc1-video")
        viewer.sendall(b"a"); host.settimeout(3); self.assertEqual(host.recv(1), b"a")
        self.assertEqual(r.expire_registrations(now=time.time() + 10 ** 6), 0)
        host.sendall(b"b"); viewer.settimeout(3); self.assertEqual(viewer.recv(1), b"b")
        viewer.sendall(b"c"); self.assertEqual(host.recv(1), b"c")
        self.assertEqual(r.stats()["active_sessions"], 1)

    def test_stale_expiry_entries_are_cleaned_up(self):
        r, addr = self.relay(expiry_leeway=0, reap_interval=60)
        h = rc.host_register_on(addr, "pc1-video", token=exp_token("pc1", 600)); time.sleep(0.3)
        v = rc.viewer_connect_one(addr, "pc1-video"); time.sleep(0.3)         # claims it
        r.expire_registrations()
        self.assertEqual(r._expiry, {})
        h.close(); v.close()

    def test_stats_exposes_the_new_counters(self):
        r, addr = self.relay()
        for key in ("token_expired", "legacy_refused", "expired_dropped"):
            self.assertEqual(r.stats()[key], 0)


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


class Provider(unittest.TestCase):
    def make(self, ttl=100.0, results=None):
        self.clock = Clock(); self.calls = 0; self.logs = []
        results = results if results is not None else []

        def refresh():
            self.calls += 1
            r = results.pop(0) if results else (f"tok{self.calls + 1}", ttl)
            if isinstance(r, Exception): raise r
            return r
        return rc.TokenProvider("tok1", ttl, refresh=refresh, log=self.logs.append, clock=self.clock)

    def test_returns_the_current_token_and_is_callable(self):
        p = self.make()
        self.assertEqual((p.get(), p()), ("tok1", "tok1")); self.assertEqual(self.calls, 0)

    def test_refreshes_after_half_the_lifetime(self):
        p = self.make(100)
        self.clock.t += 49; self.assertEqual(p.get(), "tok1"); self.assertEqual(self.calls, 0)
        self.clock.t += 2;  self.assertEqual(p.get(), "tok2"); self.assertEqual(self.calls, 1)
        self.assertEqual(p.get(), "tok2"); self.assertEqual(self.calls, 1, "not asked again until half of the NEW lifetime")
        self.clock.t += 51; self.assertEqual(p.get(), "tok3")

    def test_failure_keeps_the_old_token_and_retries_later(self):
        p = self.make(1000, results=[OSError("console down"), OSError("still down"), ("fresh", 1000)])
        self.clock.t += 501
        self.assertEqual(p.get(), "tok1"); self.assertEqual(self.calls, 1)
        self.assertTrue(any("console down" in m and "keeping the current one" in m for m in self.logs))
        self.assertEqual(p.get(), "tok1"); self.assertEqual(self.calls, 1, "not hammered on every call")
        self.clock.t += 61; self.assertEqual(p.get(), "tok1"); self.assertEqual(self.calls, 2)
        self.clock.t += 61; self.assertEqual(p.get(), "fresh"); self.assertEqual(self.calls, 3)

    def test_retry_interval_scales_down_for_short_lifetimes(self):
        p = self.make(20, results=[OSError("x"), ("fresh", 20)])
        self.clock.t += 11; p.get()                        # fails
        self.clock.t += 5.1                                # floor is 5 s
        self.assertEqual(p.get(), "fresh")

    def test_non_expiring_or_refresh_less_tokens_are_never_refreshed(self):
        clock = Clock(); p = rc.TokenProvider("hand-made", None, clock=clock)
        clock.t += 10 ** 7; self.assertEqual(p.get(), "hand-made")
        p2 = rc.TokenProvider("x", 100, refresh=None, clock=clock); clock.t += 10 ** 7
        self.assertEqual(p2.get(), "x")

    def test_console_switching_to_non_expiring_tokens_stops_the_refreshing(self):
        p = self.make(100, results=[("forever", None)])
        self.clock.t += 60; self.assertEqual(p.get(), "forever")
        self.clock.t += 10 ** 6; self.assertEqual(p.get(), "forever"); self.assertEqual(self.calls, 1)

    def test_console_no_longer_issuing_a_token_stops_the_refreshing(self):
        p = self.make(100, results=[(None, None)])
        self.clock.t += 60; self.assertEqual(p.get(), "tok1")
        self.clock.t += 10 ** 6; p.get(); self.assertEqual(self.calls, 1)

    def test_seconds_left(self):
        p = self.make(100); self.clock.t += 30
        self.assertAlmostEqual(p.seconds_left(), 70)
        self.assertIsNone(rc.TokenProvider("x", None).seconds_left())

    def test_works_as_the_token_argument_of_host_register_on(self):
        r = start_relay(secrets=[SECRET])
        try:
            addr = ("127.0.0.1", r.port)
            p = rc.TokenProvider(exp_token("pc1", 600))
            h = rc.host_register_on(addr, "pc1-video", token=p); time.sleep(0.3)
            self.assertEqual(r.stats()["waiting"], 1); h.close()
        finally:
            r.stop()


class RenewInPlace(unittest.TestCase):
    """host_wait_paired swaps its registration when the token provider hands out a new token."""
    def setUp(self):
        self.r = start_relay(secrets=[SECRET], expiry_leeway=0, reap_interval=0.2)
        self.addr = ("127.0.0.1", self.r.port)
        self.stop = threading.Event(); self.out = []; self.logs = []

    def tearDown(self):
        self.stop.set(); self.r.stop()

    def run_host(self, token):
        def go():
            try:
                self.out.append(rc.host_wait_paired([self.addr], "pc1-video", stop=self.stop, poll=0.1,
                                                    log=self.logs.append, token=token))
            except InterruptedError:
                pass
        t = threading.Thread(target=go, daemon=True); t.start(); return t

    def wait(self, cond, what, timeout=8):
        end = time.time() + timeout
        while time.time() < end:
            if cond(): return
            time.sleep(0.05)
        self.fail(f"timed out: {what}\n{self.logs}\n{self.r.stats()}")

    def test_a_new_token_renews_the_registration_without_a_gap(self):
        cur = [exp_token("pc1", 600)]
        t = self.run_host(lambda: cur[0])
        self.wait(lambda: self.r.stats()["waiting"] == 1, "initial registration")
        before = self.r.stats()["replaced"]
        cur[0] = exp_token("pc1", 1200)                    # the provider refreshed
        self.wait(lambda: self.r.stats()["replaced"] == before + 1, "registration renewed")
        self.assertEqual(self.r.stats()["waiting"], 1)
        self.assertTrue(any("renewed" in m for m in self.logs))
        v = rc.viewer_connect_one(self.addr, "pc1-video"); v.sendall(b"hello")
        t.join(5); self.assertEqual(len(self.out), 1, "a viewer still pairs after the renewal")
        sock, relay = self.out[0]; sock.settimeout(3); self.assertEqual(sock.recv(5), b"hello")

    def test_registration_survives_past_the_first_tokens_expiry_when_renewed(self):
        state = {"tok": exp_token("pc1", 2)}
        provider = rc.TokenProvider(state["tok"], 2, refresh=lambda: (exp_token("pc1", 2), 2), clock=time.monotonic)
        t = self.run_host(provider)
        time.sleep(7)                                      # several full lifetimes
        self.assertEqual(self.r.stats()["waiting"], 1, "kept alive across expiries by renewals")
        self.assertEqual(self.r.stats()["expired_dropped"], 0)
        v = rc.viewer_connect_one(self.addr, "pc1-video"); v.sendall(b"x")
        t.join(5); self.assertEqual(len(self.out), 1)

    def test_without_renewal_the_relay_drops_it_and_the_host_is_refused(self):
        t = self.run_host(exp_token("pc1", 1))
        self.wait(lambda: self.r.stats()["expired_dropped"] >= 1, "relay drops the expired registration", 6)
        self.wait(lambda: self.r.stats()["token_expired"] >= 1, "the re-register attempt is refused as expired", 8)
        self.assertEqual(self.r.stats()["waiting"], 0)
        self.assertTrue(any("dropped our registration" in m for m in self.logs))

    def test_renewal_that_cannot_reach_the_relay_keeps_the_old_registration(self):
        cur = [exp_token("pc1", 600)]
        t = self.run_host(lambda: cur[0])
        self.wait(lambda: self.r.stats()["waiting"] == 1, "initial registration")
        real = rc.host_register_on
        calls = []
        def flaky(*a, **k):
            calls.append(1); raise OSError("relay unreachable")
        rc.host_register_on = flaky
        try:
            cur[0] = exp_token("pc1", 1200); time.sleep(0.6)
        finally:
            rc.host_register_on = real
        self.assertTrue(calls); self.assertEqual(self.r.stats()["waiting"], 1, "old registration untouched")
        self.assertLessEqual(len(calls), 2, "retried on a back-off, not every poll")
        self.wait(lambda: self.r.stats()["replaced"] >= 1, "renewed once the relay is reachable again", 10)


class AdminIssuing(unittest.TestCase):
    def setUp(self):
        server._DB_PATH = os.path.join(tempfile.mkdtemp(), "admin.db")
        conn = store.init_db(server._DB_PATH)
        store.set_setting(conn, "flask_secret_key", "k"); server.app.secret_key = "k"
        store.set_setting(conn, "org_enrollment_key", "ORGKEY"); conn.close()
        server.app.config["TESTING"] = True
        self.c = server.app.test_client()
        self._env = {k: os.environ.get(k) for k in ("RELAY_SECRET", "RELAY_TOKEN_TTL")}

    def tearDown(self):
        for k, v in self._env.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v

    def token_for(self, dev="pc-a"):
        rt = self.c.post("/api/v1/enroll", json={"device_id": dev, "enrollment_key": "ORGKEY"}).get_json()["report_token"]
        return self.c.get("/api/v1/relay-token", headers={"Authorization": f"Bearer {rt}"})

    def test_without_a_ttl_it_is_the_original_token_and_expires_in_is_null(self):
        os.environ["RELAY_SECRET"] = SECRET; os.environ.pop("RELAY_TOKEN_TTL", None)
        j = self.token_for().get_json()
        self.assertEqual(j["token"], relay_mod.make_token(SECRET, "pc-a")); self.assertIsNone(j["expires_in"])

    def test_with_a_ttl_it_is_an_expiring_token_the_relay_accepts(self):
        os.environ["RELAY_SECRET"] = SECRET; os.environ["RELAY_TOKEN_TTL"] = "12h"
        before = int(time.time()); j = self.token_for().get_json(); after = int(time.time())
        self.assertEqual(j["expires_in"], 43200)
        exp = relay_mod.token_expiry(j["token"])
        self.assertTrue(before + 43200 <= exp <= after + 43200)
        self.assertEqual(j["token"], relay_mod.make_expiring_token(SECRET, "pc-a", exp))
        r = relay_mod.RelayServer("127.0.0.1", 0, quiet=True, secrets=[SECRET], require_expiry=True)
        self.assertEqual(r._token_status("pc-a-video", j["token"])[0], "ok")
        self.assertEqual(r._token_status("pc-b-video", j["token"])[0], "bad", "still bound to its own device")

    def test_first_secret_of_a_rotation_list_signs_expiring_tokens(self):
        os.environ["RELAY_SECRET"] = "new, old"; os.environ["RELAY_TOKEN_TTL"] = "600"
        j = self.token_for().get_json()
        exp = relay_mod.token_expiry(j["token"])
        self.assertEqual(j["token"], relay_mod.make_expiring_token("new", "pc-a", exp))

    def test_open_relay_gets_no_token_and_no_expiry(self):
        os.environ.pop("RELAY_SECRET", None); os.environ["RELAY_TOKEN_TTL"] = "1h"
        j = self.token_for().get_json()
        self.assertEqual((j["token"], j["expires_in"]), (None, None))

    def test_ttl_parsing(self):
        for raw, want in (("", 0), ("0", 0), ("90", 90), ("90s", 90), ("15m", 900), ("12h", 43200), ("30d", 2592000),
                          (" 2H ", 7200), ("abc", 0), ("-5", 0), ("1.5h", 0), ("h", 0)):
            os.environ["RELAY_TOKEN_TTL"] = raw
            self.assertEqual(relayauth.token_ttl(), want, repr(raw))
        os.environ.pop("RELAY_TOKEN_TTL")
        self.assertEqual(relayauth.token_ttl(), 0)

    def test_revoked_devices_still_get_nothing(self):
        os.environ["RELAY_SECRET"] = SECRET; os.environ["RELAY_TOKEN_TTL"] = "1h"
        rt = self.c.post("/api/v1/enroll", json={"device_id": "pc-r", "enrollment_key": "ORGKEY"}).get_json()["report_token"]
        conn = store.init_db(server._DB_PATH)
        self.assertTrue(store.set_device_revoked(conn, "pc-r", True)); conn.close()
        resp = self.c.get("/api/v1/relay-token", headers={"Authorization": f"Bearer {rt}"})
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("token", resp.get_json())


class Cli(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, os.path.join(ROOT, "server", "relay_token.py"), "--secret", SECRET, *args],
                              capture_output=True, text=True)

    def test_ttl_makes_an_expiring_token(self):
        now = int(time.time())
        out = self.run_cli("--id", "office-pc", "--ttl", "2h")
        self.assertEqual(out.returncode, 0, out.stderr)
        tok = out.stdout.strip(); exp = relay_mod.token_expiry(tok)
        self.assertTrue(now + 7200 <= exp <= now + 7205)
        self.assertEqual(tok, relay_mod.make_expiring_token(SECRET, "office-pc", exp))

    def test_without_ttl_it_is_the_original_token(self):
        out = self.run_cli("--id", "office-pc")
        self.assertEqual(out.stdout.strip(), relay_mod.make_token(SECRET, "office-pc"))

    def test_bad_ttl_is_a_clear_error(self):
        for bad in ("soon", "0", "-3h", "1.5h", ""):
            out = self.run_cli("--id", "x", "--ttl", bad)
            if bad == "":
                self.assertEqual(out.stdout.strip(), relay_mod.make_token(SECRET, "x"))   # empty = not given
            else:
                self.assertNotEqual(out.returncode, 0, bad); self.assertIn("--ttl", out.stderr)


# --- end to end: real host process, real relay that REQUIRES expiry, real admin console issuing 6 s tokens -------

import shutil
NEEDS = [c for c in ("Xvfb", "openssl") if not shutil.which(c)]


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f: return f.read()
    except OSError:
        return ""


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def wait_for(cond, timeout, what, logs=()):
    end = time.time() + timeout
    while time.time() < end:
        if cond(): return
        time.sleep(0.2)
    tail = "".join(f"\n--- {os.path.basename(p)} ---\n" + "\n".join(read(p).splitlines()[-25:]) for p in logs)
    raise AssertionError(f"timed out waiting for {what}{tail}")


@unittest.skipIf(NEEDS, f"needs {', '.join(NEEDS)}")
class EndToEndExpiry(unittest.TestCase):
    TTL = 6

    @classmethod
    def setUpClass(cls):
        from werkzeug.serving import make_server
        cls.tmp = tempfile.mkdtemp(prefix="expiry-e2e-")
        cls.procs = []
        cls._env = {k: os.environ.get(k) for k in ("RELAY_SECRET", "RELAY_TOKEN_TTL")}
        os.environ["RELAY_SECRET"] = SECRET; os.environ["RELAY_TOKEN_TTL"] = str(cls.TTL)

        server._DB_PATH = os.path.join(cls.tmp, "admin.db")
        conn = store.init_db(server._DB_PATH)
        store.set_setting(conn, "flask_secret_key", "k"); server.app.secret_key = "k"
        store.set_setting(conn, "org_enrollment_key", "ORG"); conn.close()
        cls.admin_port = free_port()
        cls.admin = make_server("127.0.0.1", cls.admin_port, server.app, threaded=True)
        threading.Thread(target=cls.admin.serve_forever, daemon=True).start()
        cls.admin_up = True

        cls.relay = start_relay(secrets=[SECRET], require_expiry=True, expiry_leeway=0, reap_interval=0.3)
        cls.relay_arg = f"127.0.0.1:{cls.relay.port}"

        cls.display = f":{80 + os.getpid() % 9}"
        cls.procs.append(subprocess.Popen(["Xvfb", cls.display, "-screen", "0", "1024x768x24"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(1.0)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", "host_key.pem", "-out",
                        "host_cert.pem", "-days", "2", "-nodes", "-subj", "/CN=test"], cwd=cls.tmp,
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import auth
        cfg = auth.load_config(os.path.join(cls.tmp, "host_config.json"))
        cfg["unattended_password_salt"], cfg["unattended_password_hash"] = auth.hash_password("pw")
        auth.save_config(cfg, os.path.join(cls.tmp, "host_config.json"))
        cls.env = dict(os.environ, DISPLAY=cls.display, PYTHONUNBUFFERED="1", QT_LOGGING_RULES="*=false",
                       REMOTEBRIDGE_KNOWN_HOSTS=os.path.join(cls.tmp, "known_hosts.json"))
        for k in ("RELAY_SECRET", "RELAY_TOKEN_TTL"):
            cls.env.pop(k, None)                    # the host gets its token from the console, nowhere else
        cls.host_log = os.path.join(cls.tmp, "host.log")
        cls.host = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "desktop", "host_p12.py"), "--relay", cls.relay_arg, "--id", "exp-dev",
             "--admin-url", f"http://127.0.0.1:{cls.admin_port}", "--enrollment-key", "ORG", "--lang", "en",
             "--no-update-check"],
            cwd=cls.tmp, env=cls.env, stdin=subprocess.DEVNULL, stdout=open(cls.host_log, "w"), stderr=subprocess.STDOUT)
        cls.procs.append(cls.host)
        wait_for(lambda: cls.relay.stats()["waiting"] == 1, 25, "host to register on the relay", [cls.host_log])

    @classmethod
    def tearDownClass(cls):
        for p in reversed(cls.procs): p.terminate()
        for p in cls.procs:
            try: p.wait(5)
            except subprocess.TimeoutExpired: p.kill()
        cls.relay.stop()
        if cls.admin_up:
            cls.admin.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._env.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v

    def viewer(self, tag):
        log = os.path.join(self.tmp, f"viewer-{tag}.log")
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "desktop", "viewer_p12.py"), "--relay", self.relay_arg,
                              "--device", "exp-dev", "--id", tag, "--password", "pw", "--lang", "en", "--no-voice"],
                             cwd=self.tmp, env=self.env, stdin=subprocess.PIPE, stdout=open(log, "w"),
                             stderr=subprocess.STDOUT, text=True)
        self.procs.append(p)
        return p, log

    def test_01_host_announces_its_token_and_keeps_registering_across_expiries(self):
        out = read(self.host_log)
        self.assertIn("Relay token expires in 6 seconds; it will be renewed automatically.", out)
        self.assertNotIn("REJECTED", out); self.assertNotIn("EXPIRED", out)
        t0 = time.time()
        time.sleep(self.TTL * 2.5)                   # well past two lifetimes of the first token
        st = self.relay.stats()
        self.assertEqual(st["waiting"], 1, "still registered")
        self.assertEqual(st["expired_dropped"], 0, "renewed in time: the relay never had to drop it")
        self.assertEqual(st["token_expired"], 0)
        self.assertEqual(st["legacy_refused"], 0)
        self.assertGreaterEqual(st["replaced"], 2, "registration renewed in place at least twice")
        self.assertIn("refreshed the relay token", read(self.host_log))

    def test_02_a_viewer_connects_long_after_the_first_token_expired(self):
        p, log = self.viewer("late")
        wait_for(lambda: "approved" in read(log).lower(), 30, "viewer approved through the relay",
                 [log, self.host_log])
        p.terminate(); p.wait(5)
        self.assertEqual(self.relay.stats()["expired_dropped"], 0)

    def test_03_when_the_console_goes_away_the_registration_lapses_with_the_last_token(self):
        type(self).admin_up = False
        self.admin.shutdown(); self.admin.server_close()
        wait_for(lambda: self.relay.stats()["waiting"] == 0, self.TTL + 6,
                 "relay to drop the registration once the last token expired", [self.host_log])
        wait_for(lambda: "could not refresh the relay token" in read(self.host_log), 10,
                 "host to report that it cannot refresh", [self.host_log])
        self.assertGreaterEqual(self.relay.stats()["expired_dropped"], 1)
        self.assertTrue(self.host.poll() is None, "the host keeps running; it just cannot register")


if __name__ == "__main__":
    unittest.main(verbosity=2)
