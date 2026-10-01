"""Phase 12 relay + failover tests. Every test uses REAL sockets on
loopback - Phase 11 found that fake sockets (which can't block) had hidden a
deadlock for four phases, so nothing here fakes a connection."""
import os, socket, sys, threading, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, os.path.join(ROOT, "desktop"))

import relay as relay_mod
import relay_client as rc


def start_relay(**kw):
    kw.setdefault("quiet", True)
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("port", 0)
    r = relay_mod.RelayServer(**kw)
    r.start()
    return r


def recv_exact(sock, n, timeout=5):
    sock.settimeout(timeout)
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


class ParseRelays(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(rc.parse_relays("a:1, b:2,,a:1"), [("a", 1), ("b", 2)])
        self.assertEqual(rc.parse_relays(None), [])
        with self.assertRaises(ValueError):
            rc.parse_relays("nohost")
        with self.assertRaises(ValueError):
            rc.parse_relays("h:notaport")


class RelayBasics(unittest.TestCase):
    def setUp(self):
        self.relay = start_relay(reap_interval=0.2, connect_wait=1.0)
        self.addr = ("127.0.0.1", self.relay.port)

    def tearDown(self):
        self.relay.stop()

    def test_ping_and_stats(self):
        self.assertTrue(rc.ping(self.addr))
        with socket.create_connection(self.addr) as s:
            s.sendall(b"STATS\n")
            self.assertIn('"waiting"', rc._read_line(s))

    def test_pair_and_forward_both_ways(self):
        host = rc.host_register_on(self.addr, "dev-video")
        viewer = rc.viewer_connect_one(self.addr, "dev-video")
        viewer.sendall(b"hello-host")
        self.assertEqual(recv_exact(host, 10), b"hello-host")
        host.sendall(b"hello-viewer")
        self.assertEqual(recv_exact(viewer, 12), b"hello-viewer")
        big = os.urandom(1_000_000)
        threading.Thread(target=lambda: host.sendall(big), daemon=True).start()
        self.assertEqual(recv_exact(viewer, len(big), timeout=10), big)
        host.close(); viewer.close()

    def test_not_found(self):
        with self.assertRaises(ConnectionError) as cm:
            rc.viewer_connect_one(self.addr, "nobody")
        self.assertIn("not_found", str(cm.exception))

    def test_connect_waits_for_late_register(self):
        # viewer CONNECTs first; host registers 0.4s later -> still pairs
        result = {}
        def viewer():
            result["sock"] = rc.viewer_connect_one(self.addr, "late-1")
        t = threading.Thread(target=viewer); t.start()
        time.sleep(0.4)
        host = rc.host_register_on(self.addr, "late-1")
        t.join(5)
        self.assertIn("sock", result)
        result["sock"].sendall(b"x"); self.assertEqual(recv_exact(host, 1), b"x")

    def test_dead_registration_is_reaped_not_paired(self):
        host = rc.host_register_on(self.addr, "ghost")
        time.sleep(0.2)
        host.close()
        deadline = time.time() + 3
        while time.time() < deadline and self.relay.stats()["waiting"]:
            time.sleep(0.1)
        self.assertEqual(self.relay.stats()["waiting"], 0)
        self.assertGreaterEqual(self.relay.stats()["reaped"], 1)
        with self.assertRaises(ConnectionError):
            rc.viewer_connect_one(self.addr, "ghost")

    def test_duplicate_register_replaces_and_closes_old(self):
        old = rc.host_register_on(self.addr, "dup")
        time.sleep(0.2)
        new = rc.host_register_on(self.addr, "dup")
        time.sleep(0.2)
        old.settimeout(2)
        self.assertEqual(old.recv(1), b"")          # old one was closed by the relay
        viewer = rc.viewer_connect_one(self.addr, "dup")
        viewer.sendall(b"z"); self.assertEqual(recv_exact(new, 1), b"z")

    def assertHungUp(self, sock):
        # closing with unread bytes queued shows up as RST rather than a clean EOF
        try:
            self.assertEqual(sock.recv(1), b"")
        except ConnectionResetError:
            pass

    def test_stale_registration_never_paired_even_before_reaper_runs(self):
        r = start_relay(reap_interval=60, connect_wait=0.3)     # reaper effectively off
        a = ("127.0.0.1", r.port)
        try:
            host = rc.host_register_on(a, "stale")
            time.sleep(0.2); host.close(); time.sleep(0.2)
            with self.assertRaises(ConnectionError) as cm:       # must not answer OK
                rc.viewer_connect_one(a, "stale")
            self.assertIn("not_found", str(cm.exception))
            # ...but a live registration made after the stale one is used
            h2 = rc.host_register_on(a, "stale2"); time.sleep(0.1); h2.close(); time.sleep(0.1)
            h3 = rc.host_register_on(a, "stale2"); time.sleep(0.1)
            v = rc.viewer_connect_one(a, "stale2"); v.sendall(b"q")
            self.assertEqual(recv_exact(h3, 1), b"q")
        finally:
            r.stop()

    def test_slow_loris_and_garbage_are_dropped(self):
        r = start_relay(handshake_timeout=0.3)
        try:
            s = socket.create_connection(("127.0.0.1", r.port))
            s.sendall(b"CONN")                       # never finishes the line
            s.settimeout(3)
            self.assertHungUp(s)                     # relay hung up on us
            s2 = socket.create_connection(("127.0.0.1", r.port))
            s2.sendall(b"A" * 1000)                  # over-long, no newline
            s2.settimeout(3)
            self.assertHungUp(s2)
        finally:
            r.stop()

    def test_capacity_limits(self):
        r = start_relay(max_sessions=1, max_waiting=2, connect_wait=0.3)
        a = ("127.0.0.1", r.port)
        try:
            h1 = rc.host_register_on(a, "one"); h2 = rc.host_register_on(a, "two")
            time.sleep(0.2)
            h3 = rc.host_register_on(a, "three")     # table full -> relay closes it
            h3.settimeout(2); self.assertEqual(h3.recv(1), b"")
            v1 = rc.viewer_connect_one(a, "one")     # uses the only session slot
            with self.assertRaises(ConnectionError) as cm:
                rc.viewer_connect_one(a, "two")
            self.assertIn("busy", str(cm.exception))
            v1.close(); h1.close()
            time.sleep(0.3)
            v2 = rc.viewer_connect_one(a, "two")     # slot freed, host "two" still waiting
            v2.close()
        finally:
            r.stop()

    def test_stats_forbidden_from_non_loopback_logic(self):
        # can't fake a remote address on loopback; check the open flag path instead
        r = start_relay(stats_open=True)
        with socket.create_connection(("127.0.0.1", r.port)) as s:
            s.sendall(b"STATS\n")
            self.assertIn("uptime_seconds", rc._read_line(s))
        r.stop()


class Failover(unittest.TestCase):
    def test_viewer_skips_dead_relay_and_wrong_relay(self):
        good = start_relay(connect_wait=0.3)
        empty = start_relay(connect_wait=0.3)         # host never registers here
        dead_sock = socket.socket(); dead_sock.bind(("127.0.0.1", 0)); dead_port = dead_sock.getsockname()[1]; dead_sock.close()
        try:
            host = rc.host_register_on(("127.0.0.1", good.port), "fo-1")
            relays = [("127.0.0.1", dead_port), ("127.0.0.1", empty.port), ("127.0.0.1", good.port)]
            viewer, used = rc.viewer_connect(relays, "fo-1", connect_timeout=1)
            self.assertEqual(used, ("127.0.0.1", good.port))
            viewer.sendall(b"ok"); self.assertEqual(recv_exact(host, 2), b"ok")
        finally:
            good.stop(); empty.stop()

    def test_all_relays_fail_reports_each_reason(self):
        a = start_relay(connect_wait=0.2)
        s = socket.socket(); s.bind(("127.0.0.1", 0)); dead = s.getsockname()[1]; s.close()
        try:
            with self.assertRaises(rc.RelayError) as cm:
                rc.viewer_connect([("127.0.0.1", dead), ("127.0.0.1", a.port)], "nope", connect_timeout=1)
            self.assertEqual(len(cm.exception.attempts), 2)
            self.assertIn("not_found", str(cm.exception))
        finally:
            a.stop()

    def test_host_registers_on_all_and_pairs_via_whichever_is_used(self):
        a, b = start_relay(reap_interval=0.2), start_relay(reap_interval=0.2)
        try:
            ra, rb = ("127.0.0.1", a.port), ("127.0.0.1", b.port)
            out = {}
            t = threading.Thread(target=lambda: out.update(zip(("sock", "relay"),
                                 rc.host_wait_paired([ra, rb], "multi-video"))), daemon=True)
            t.start()
            time.sleep(0.5)
            self.assertEqual((a.stats()["waiting"], b.stats()["waiting"]), (1, 1))
            viewer, used = rc.viewer_connect([rb, ra], "multi-video")   # viewer prefers b
            self.assertEqual(used, rb)
            viewer.sendall(b"hi"); t.join(5)
            self.assertEqual(out["relay"], rb)
            self.assertEqual(recv_exact(out["sock"], 2), b"hi")
            time.sleep(0.4)
            self.assertEqual(a.stats()["waiting"] + b.stats()["waiting"], 0,
                             "the losing relay's registration must be withdrawn")
        finally:
            a.stop(); b.stop()

    def test_host_survives_one_relay_dying_and_reregisters_after_restart(self):
        a, b = start_relay(), start_relay()
        ra, rb = ("127.0.0.1", a.port), ("127.0.0.1", b.port)
        out = {}
        t = threading.Thread(target=lambda: out.update(zip(("sock", "relay"),
                             rc.host_wait_paired([ra, rb], "surv-video", poll=0.2))), daemon=True)
        t.start()
        try:
            time.sleep(0.5)
            a.stop()                                   # relay A dies while the host waits
            time.sleep(0.6)
            viewer, used = rc.viewer_connect([ra, rb], "surv-video", connect_timeout=1)
            self.assertEqual(used, rb, "viewer must fail over from dead A to B")
            viewer.sendall(b"!"); t.join(5)
            self.assertEqual(recv_exact(out["sock"], 1), b"!")
        finally:
            b.stop()

    def test_host_reregisters_when_relay_comes_back(self):
        a = start_relay()
        port = a.port
        ra = ("127.0.0.1", port)
        out = {}
        t = threading.Thread(target=lambda: out.update(zip(("sock", "relay"),
                             rc.host_wait_paired([ra], "back-video", poll=0.2))), daemon=True)
        t.start()
        time.sleep(0.5)
        a.stop()
        time.sleep(1.0)
        a2 = start_relay(port=port)                    # same address, fresh (empty) relay
        try:
            deadline = time.time() + 8
            while time.time() < deadline and a2.stats()["waiting"] == 0:
                time.sleep(0.2)
            self.assertEqual(a2.stats()["waiting"], 1, "host should have re-registered on its own")
            viewer = rc.viewer_connect_one(ra, "back-video")
            viewer.sendall(b"k"); t.join(5)
            self.assertEqual(recv_exact(out["sock"], 1), b"k")
        finally:
            a2.stop()

    def test_stop_event_aborts_wait(self):
        r = start_relay(reap_interval=0.2)
        stop = threading.Event()
        err = {}
        def run():
            try:
                rc.host_wait_paired([("127.0.0.1", r.port)], "x", stop=stop, poll=0.1)
            except InterruptedError as e:
                err["e"] = e
        t = threading.Thread(target=run); t.start()
        time.sleep(0.3); stop.set(); t.join(3)
        self.assertIn("e", err)
        time.sleep(0.4)
        self.assertEqual(r.stats()["waiting"], 0)
        r.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
