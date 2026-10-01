"""Tests for the login throttle (desktop/auth.py) and monitor-aware input mapping."""
import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "desktop"))
import auth


class FakeClock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


def cfg(password="correct horse"):
    salt, h = auth.hash_password(password)
    return {"unattended_password_hash": h, "unattended_password_salt": salt,
            "totp_enabled": False, "whitelist": [], "confirmation_timeout_seconds": 1}


class ThrottleTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.th = auth.AuthThrottle(free_attempts=3, base_delay=10, max_delay=100,
                                    global_free_attempts=20, clock=self.clock)
        self.cfg = cfg()

    def attempt(self, vid="alice", pw="wrong", addr="1.2.3.4:5000", **kw):
        return auth.decide_host_auth(vid, pw, "", self.cfg, addr, throttle=self.th, **kw)

    def test_locks_after_free_attempts_and_skips_verification(self):
        for _ in range(3):
            self.assertEqual(self.attempt()["decision"], "rejected_password")
        r = self.attempt(pw="correct horse")      # right password, but locked
        self.assertEqual(r["decision"], "rejected_throttled")
        self.assertFalse(r["approved"])

    def test_lock_expires_then_backoff_doubles(self):
        for _ in range(3): self.attempt()
        self.clock.t += 11                        # 10s base delay passed
        self.assertEqual(self.attempt()["decision"], "rejected_password")  # 4th failure
        self.clock.t += 11                        # now needs 20s
        self.assertEqual(self.attempt()["decision"], "rejected_throttled")
        self.clock.t += 10
        self.assertEqual(self.attempt(pw="correct horse")["decision"], "auto_unattended")

    def test_backoff_capped(self):
        for _ in range(30): self.th.record_failure("a", "9.9.9.9")
        self.assertLessEqual(self.th.retry_after("a", "9.9.9.9"), 100)

    def test_success_clears_counters(self):
        for _ in range(2): self.attempt()
        self.assertEqual(self.attempt(pw="correct horse")["decision"], "auto_unattended")
        for _ in range(2): self.attempt()         # would be locked if not cleared
        self.assertEqual(self.attempt()["decision"], "rejected_password")

    def test_rotating_ids_from_one_address_still_locked(self):
        for i in range(3): self.attempt(vid=f"v{i}")
        self.assertEqual(self.attempt(vid="fresh")["decision"], "rejected_throttled")

    def test_rotating_addresses_same_id_still_locked(self):
        for i in range(3): self.attempt(addr=f"10.0.0.{i}:1")
        self.assertEqual(self.attempt(addr="10.0.0.99:1")["decision"], "rejected_throttled")

    def test_relay_mode_does_not_lock_other_viewers(self):
        for i in range(3):
            self.attempt(vid=f"attacker{i}", addr="relay:6000", throttle_addr=None)
        r = self.attempt(vid="bob", pw="correct horse", addr="relay:6000", throttle_addr=None)
        self.assertEqual(r["decision"], "auto_unattended")

    def test_global_cap_bounds_rotation_everywhere(self):
        for i in range(20):
            self.attempt(vid=f"v{i}", addr=f"10.1.{i}.1:1")
        r = self.attempt(vid="new", addr="10.2.0.1:1")
        self.assertEqual(r["decision"], "rejected_throttled")

    def test_whitelist_miss_not_counted(self):
        self.cfg["whitelist"] = ["bob"]
        for _ in range(10):
            self.assertEqual(self.attempt(vid="mallory")["decision"], "rejected_whitelist")
        self.assertEqual(self.th.retry_after("mallory", "1.2.3.4:5000"), 0)

    def test_host_part_parsing(self):
        hp = auth.AuthThrottle._host_part
        self.assertEqual(hp("1.2.3.4:5000"), "1.2.3.4")
        self.assertEqual(hp("[::1]:5000"), "::1")
        self.assertEqual(hp("::1"), "::1")


class MonitorMappingTests(unittest.TestCase):
    def test_offset_and_size_follow_the_monitor(self):
        try:
            import host_p12
        except ImportError as e:
            self.skipTest(f"host deps missing: {e}")
        f = host_p12.normalized_to_screen
        self.assertEqual(f(0.5, 0.5, (0, 0, 1920, 1080)), (960, 540))
        # second monitor, 2560x1440, placed right of a 1920-wide primary
        self.assertEqual(f(0.0, 0.0, (1920, 0, 2560, 1440)), (1920, 0))
        self.assertEqual(f(1.0, 1.0, (1920, 0, 2560, 1440)), (4480, 1440))
        # monitor above the primary (negative top)
        self.assertEqual(f(0.5, 0.5, (0, -1080, 1920, 1080)), (960, -540))


if __name__ == "__main__":
    unittest.main()
