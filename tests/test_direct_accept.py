"""direct_accept.ChannelPairer: real loopback sockets, no TLS, no display, no capture/input libraries."""
import os, socket, sys, threading, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "desktop"))

import direct_accept


def listener():
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", 0)); s.listen(8)
    return s


def closed_by_host(sock) -> bool:
    """True if the host end closed it (a FIN, or a RST because unread data was still queued)."""
    sock.settimeout(2)
    try:
        return sock.recv(1) == b""
    except ConnectionResetError:
        return True


def dial(server, source="127.0.0.1"):
    c = socket.socket()
    c.bind((source, 0))
    c.connect(("127.0.0.1", server.getsockname()[1]))
    return c


class PairerTests(unittest.TestCase):
    def setUp(self):
        self.servers = {k: listener() for k in ("input", "control", "audio")}
        self.pairer = direct_accept.ChannelPairer(self.servers, log=lambda m: None)
        self.clients = []

    def tearDown(self):
        self.pairer.close()
        for s in self.servers.values(): s.close()
        for c in self.clients:
            try: c.close()
            except OSError: pass

    def dial(self, label, source="127.0.0.1"):
        c = dial(self.servers[label], source); self.clients.append(c); return c

    def test_take_returns_the_socket_from_that_address(self):
        a = self.dial("input", "127.0.0.1"); b = self.dial("input", "127.0.0.2")
        b.sendall(b"B"); a.sendall(b"A")
        got_b = self.pairer.take("input", "127.0.0.2", 3)
        got_a = self.pairer.take("input", "127.0.0.1", 3)
        self.assertEqual(got_b.recv(1), b"B")
        self.assertEqual(got_a.recv(1), b"A")
        got_a.close(); got_b.close()

    def test_take_waits_for_a_late_arrival(self):
        threading.Timer(0.4, lambda: self.dial("audio").sendall(b"x")).start()
        t0 = time.monotonic()
        got = self.pairer.take("audio", "127.0.0.1", 5)
        self.assertEqual(got.recv(1), b"x"); got.close()
        self.assertLess(time.monotonic() - t0, 3)

    def test_take_times_out_with_socket_timeout(self):
        with self.assertRaises(socket.timeout):
            self.pairer.take("control", "127.0.0.1", 0.5)

    def test_take_gives_up_at_once_when_the_viewer_closes_its_video_channel(self):
        host_side, viewer_side = socket.socketpair()
        threading.Timer(0.3, viewer_side.close).start()
        t0 = time.monotonic()
        with self.assertRaises(ConnectionAbortedError):
            self.pairer.take("input", "127.0.0.1", 10, watch=host_side)
        self.assertLess(time.monotonic() - t0, 2, "must not sit out the 10 s timeout")
        host_side.close()

    def test_a_quiet_video_channel_does_not_end_the_wait_early(self):
        host_side, viewer_side = socket.socketpair()
        threading.Timer(0.6, lambda: self.dial("input").sendall(b"k")).start()
        got = self.pairer.take("input", "127.0.0.1", 5, watch=host_side)
        self.assertEqual(got.recv(1), b"k"); got.close()
        host_side.close(); viewer_side.close()

    def test_one_addresses_wait_does_not_hold_up_another(self):
        """The bug: a viewer that stalls mid-setup made every later viewer wait."""
        entered = threading.Event(); release = threading.Event(); other_done = threading.Event()

        def stalled():
            with self.pairer.gate("10.0.0.1"):
                entered.set(); release.wait(10)

        def other():
            with self.pairer.gate("10.0.0.2"):
                other_done.set()

        t1 = threading.Thread(target=stalled); t1.start(); entered.wait(2)
        t2 = threading.Thread(target=other); t2.start()
        self.assertTrue(other_done.wait(1), "a different address must not wait for the stalled one")
        release.set(); t1.join(2); t2.join(2)

    def test_same_address_setups_take_turns(self):
        order = []; first_in = threading.Event(); release = threading.Event()

        def first():
            with self.pairer.gate("10.0.0.1"):
                order.append("first-in"); first_in.set(); release.wait(5); order.append("first-out")

        def second():
            with self.pairer.gate("10.0.0.1"):
                order.append("second-in")

        t1 = threading.Thread(target=first); t1.start(); first_in.wait(2)
        t2 = threading.Thread(target=second); t2.start()
        time.sleep(0.3)
        self.assertEqual(order, ["first-in"], "second setup from the same address must wait")
        release.set(); t1.join(2); t2.join(2)
        self.assertEqual(order, ["first-in", "first-out", "second-in"])

    def test_gate_bookkeeping_is_cleaned_up(self):
        with self.pairer.gate("10.0.0.9"):
            pass
        self.assertEqual(self.pairer._gates, {})

    def test_leftovers_from_an_earlier_setup_are_not_handed_to_the_next(self):
        stale = self.dial("input"); stale.sendall(b"S")
        deadline = time.monotonic() + 2
        while not self.pairer._queues["input"] and time.monotonic() < deadline: time.sleep(0.02)
        with self.pairer.gate("127.0.0.1"):
            with self.assertRaises(socket.timeout):
                self.pairer.take("input", "127.0.0.1", 0.4)
        self.assertTrue(closed_by_host(stale), "the leftover socket was closed, not kept")

    def test_unclaimed_sockets_are_dropped_after_the_stale_window(self):
        self.pairer.close()
        self.pairer = direct_accept.ChannelPairer(self.servers, log=lambda m: None, stale_seconds=0.3)
        c = self.dial("input")
        time.sleep(0.6); self.pairer._drop_stale()
        self.assertTrue(closed_by_host(c))

    def test_queue_per_address_is_capped(self):
        extra = [self.dial("input") for _ in range(direct_accept.MAX_QUEUED_PER_ADDRESS + 3)]
        time.sleep(0.5)
        self.assertEqual(len(self.pairer._queues["input"]["127.0.0.1"]), direct_accept.MAX_QUEUED_PER_ADDRESS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
