"""Admin console sign-in hardening: DB-backed lockout, TOTP 2FA, recovery codes, forced enrollment.
Through Flask's real request pipeline, like test_admin_p12.py."""
import os, re, sys, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pyotp
from test_admin_p12 import AdminBase, audit
import server, store


def code_for(secret, offset=0):
    return pyotp.TOTP(secret).at(time.time() + 30 * offset)


class SignInBase(AdminBase):
    def post_login(self, user="root", pw="pw-root", client=None, addr="10.0.0.1", path="/login"):
        client = client or self.c
        return client.post(path, data={"username": user, "password": pw, "csrf_token": self.token(client)},
                           environ_overrides={"REMOTE_ADDR": addr})

    def post_form(self, path, data, client=None, addr="10.0.0.1"):
        client = client or self.c
        data = dict(data, csrf_token=self.token(client))
        return client.post(path, data=data, environ_overrides={"REMOTE_ADDR": addr})

    def enroll_2fa(self, user="root", pw="pw-root"):
        """Sign in, turn 2FA on through the real pages, sign out. Returns (secret, recovery_codes)."""
        c = server.app.test_client()
        self.assertEqual(self.post_login(user, pw, client=c).status_code, 302)
        self.post_form("/account/security/2fa/start", {}, client=c)
        html = c.get("/account/security").get_data(as_text=True)
        secret = re.search(r'class="mono" style="font-size:1.1em;">([A-Z2-7 ]+)<', html).group(1).replace(" ", "")
        r = self.post_form("/account/security/2fa/confirm", {"code": code_for(secret)}, client=c)
        self.assertEqual(r.status_code, 302)
        page = c.get("/account/security").get_data(as_text=True)
        codes = re.findall(r"<li>([a-z2-9]{5}-[a-z2-9]{5})</li>", page)
        self.assertEqual(len(codes), store.RECOVERY_CODE_COUNT)
        # A second view must not show them again.
        self.assertNotIn(codes[0], c.get("/account/security").get_data(as_text=True))
        self.post_form("/logout", {}, client=c)
        uid = [u for u in store.list_admin_users(self.conn) if u["username"] == user][0]["id"]
        return secret, codes, uid

    def fresh_step(self, uid):
        """Forget the last accepted TOTP step so the test can reuse the current code window."""
        self.conn.execute("UPDATE admin_users SET totp_last_step = 0 WHERE id = ?", (uid,)); self.conn.commit()


class Lockout(SignInBase):
    def test_locks_after_free_attempts_even_for_the_right_password(self):
        for _ in range(store.FREE_ATTEMPTS - 1):
            self.assertEqual(self.post_login(pw="bad").status_code, 200)
        self.assertEqual(self.post_login(pw="bad").status_code, 200)    # this one trips the lock
        r = self.post_login(pw="pw-root")                   # correct, but locked
        self.assertEqual(r.status_code, 429)
        self.assertGreater(int(r.headers["Retry-After"]), 0)
        self.assertIn("Too many failed attempts", r.get_data(as_text=True))

    def test_lock_is_in_the_database_so_a_new_process_still_sees_it(self):
        for _ in range(store.FREE_ATTEMPTS): self.post_login(pw="bad")
        self.assertEqual(self.post_login(pw="pw-root", client=server.app.test_client()).status_code, 429)

    def test_lock_expires(self):
        for _ in range(store.FREE_ATTEMPTS): self.post_login(pw="bad")
        self.conn.execute("UPDATE login_throttle SET locked_until = ?", (int(time.time()) - 1,)); self.conn.commit()
        self.assertEqual(self.post_login(pw="pw-root").status_code, 302)

    def test_one_address_cannot_spray_many_usernames(self):
        for i in range(store.FREE_ATTEMPTS):
            self.post_login(user=f"nobody{i}", pw="x", addr="6.6.6.6")
        r = self.post_login(user="root", pw="pw-root", addr="6.6.6.6")   # a different, real account
        self.assertEqual(r.status_code, 429)
        self.assertEqual(self.post_login(user="root", pw="pw-root", addr="10.9.9.9").status_code, 302)

    def test_unknown_usernames_are_throttled_and_look_the_same(self):
        for _ in range(store.FREE_ATTEMPTS):
            r = self.post_login(user="ghost", pw="x")
            self.assertEqual(r.status_code, 200)
            self.assertIn("Incorrect username or password", r.get_data(as_text=True))
        self.assertEqual(self.post_login(user="ghost", pw="x").status_code, 429)

    def test_username_case_does_not_dodge_the_lock(self):
        for u in ["root", "ROOT", "Root", "rOOt", "root "]: self.post_login(user=u, pw="bad", addr="1.1.1.%d" % len(u))
        for u in ["rOOT", "ROOt"]: self.post_login(user=u, pw="bad", addr="2.2.2.2")
        self.assertEqual(self.post_login(user="root", pw="pw-root", addr="3.3.3.3").status_code, 429)

    def test_success_clears_the_account_counter(self):
        for _ in range(store.FREE_ATTEMPTS - 1): self.post_login(pw="bad")
        self.assertEqual(self.post_login().status_code, 302)
        row = self.conn.execute("SELECT * FROM login_throttle WHERE key = 'user:root'").fetchone()
        self.assertIsNone(row)

    def test_backoff_doubles_and_caps(self):
        keys = store.throttle_keys("zed", "9.9.9.9"); t = 10_000
        for _ in range(store.FREE_ATTEMPTS - 1): store.throttle_record_failure(self.conn, keys, now=t)
        self.assertEqual(store.throttle_retry_after(self.conn, keys, now=t), 0)       # still free
        store.throttle_record_failure(self.conn, keys, now=t)
        self.assertEqual(store.throttle_retry_after(self.conn, keys, now=t), store.BASE_DELAY)
        store.throttle_record_failure(self.conn, keys, now=t)
        self.assertEqual(store.throttle_retry_after(self.conn, keys, now=t), store.BASE_DELAY * 2)
        for _ in range(30): store.throttle_record_failure(self.conn, keys, now=t)
        self.assertEqual(store.throttle_retry_after(self.conn, keys, now=t), store.MAX_DELAY)

    def test_old_failures_are_forgotten(self):
        keys = store.throttle_keys("zed", "9.9.9.9"); t = 10_000
        for _ in range(store.FREE_ATTEMPTS - 1): store.throttle_record_failure(self.conn, keys, now=t)
        later = t + store.FAILURE_MEMORY + 1
        store.throttle_record_failure(self.conn, keys, now=later)
        row = self.conn.execute("SELECT failures FROM login_throttle WHERE key = ?", (keys[0],)).fetchone()
        self.assertEqual(row["failures"], 1)

    def test_next_parameter_cannot_redirect_off_site(self):
        for target in ["https://evil.example/", "//evil.example", "/\\evil.example"]:
            c = server.app.test_client()
            r = c.post("/login?next=" + target, data={"username": "root", "password": "pw-root",
                                                      "csrf_token": self.token(c)})
            self.assertEqual(r.status_code, 302)
            self.assertNotIn("evil", r.headers["Location"], target)


class TwoFactor(SignInBase):
    def test_enrollment_then_login_needs_the_code(self):
        secret, codes, uid = self.enroll_2fa()
        self.fresh_step(uid)
        c = server.app.test_client()
        r = self.post_login(client=c)
        self.assertEqual(r.status_code, 302); self.assertTrue(r.headers["Location"].endswith("/login/2fa"))
        # Password alone has not signed anyone in.
        self.assertEqual(c.get("/").status_code, 302)
        self.assertIn("/login", c.get("/devices").headers["Location"])
        r = self.post_form("/login/2fa", {"code": code_for(secret)}, client=c)
        self.assertEqual(r.status_code, 302); self.assertEqual(c.get("/").status_code, 200)

    def test_wrong_code_rejected_and_pending_session_ends_after_too_many(self):
        secret, codes, uid = self.enroll_2fa(); self.fresh_step(uid)
        c = server.app.test_client(); self.post_login(client=c)
        for _ in range(4):
            r = self.post_form("/login/2fa", {"code": "000000"}, client=c)
            self.assertEqual(r.status_code, 200); self.assertIn("Incorrect code", r.get_data(as_text=True))
        r = self.post_form("/login/2fa", {"code": "000000"}, client=c)
        self.assertEqual(r.status_code, 302); self.assertTrue(r.headers["Location"].endswith("/login"))
        # ...and the right code no longer works without the password again.
        r = self.post_form("/login/2fa", {"code": code_for(secret)}, client=c)
        self.assertTrue(r.headers["Location"].endswith("/login"))

    def test_wrong_codes_count_toward_the_lockout(self):
        secret, codes, uid = self.enroll_2fa(); self.fresh_step(uid)
        for _ in range(2):                                   # 2 sessions x 3 bad codes > free attempts
            c = server.app.test_client(); self.post_login(client=c)
            for _ in range(3): self.post_form("/login/2fa", {"code": "000000"}, client=c)
        c = server.app.test_client()
        self.assertEqual(self.post_login(client=c).status_code, 429)

    def test_a_totp_code_cannot_be_replayed(self):
        secret, codes, uid = self.enroll_2fa(); self.fresh_step(uid)
        code = code_for(secret)
        c1 = server.app.test_client(); self.post_login(client=c1)
        self.assertEqual(self.post_form("/login/2fa", {"code": code}, client=c1).status_code, 302)
        self.assertEqual(c1.get("/").status_code, 200)
        c2 = server.app.test_client(); self.post_login(client=c2)
        r = self.post_form("/login/2fa", {"code": code}, client=c2)
        self.assertEqual(r.status_code, 200); self.assertIn("Incorrect code", r.get_data(as_text=True))

    def test_recovery_code_works_once(self):
        secret, codes, uid = self.enroll_2fa()
        c = server.app.test_client(); self.post_login(client=c)
        r = self.post_form("/login/2fa", {"code": codes[0].upper()}, client=c)   # case/format forgiving
        self.assertEqual(r.status_code, 302); self.assertEqual(c.get("/").status_code, 200)
        self.assertEqual(store.recovery_codes_left(self.conn, uid), store.RECOVERY_CODE_COUNT - 1)
        c2 = server.app.test_client(); self.post_login(client=c2)
        r = self.post_form("/login/2fa", {"code": codes[0]}, client=c2)
        self.assertEqual(r.status_code, 200)

    def test_pending_login_expires(self):
        secret, codes, uid = self.enroll_2fa(); self.fresh_step(uid)
        c = server.app.test_client(); self.post_login(client=c)
        with c.session_transaction() as s:
            p = dict(s["pending_2fa"]); p["ts"] -= server.PENDING_2FA_SECONDS + 1; s["pending_2fa"] = p
        r = self.post_form("/login/2fa", {"code": code_for(secret)}, client=c)
        self.assertTrue(r.headers["Location"].endswith("/login"))
        self.assertEqual(c.get("/").status_code, 302)

    def test_2fa_page_without_a_password_step_goes_to_login(self):
        self.assertTrue(self.c.get("/login/2fa").headers["Location"].endswith("/login"))

    def test_bad_confirmation_code_leaves_2fa_off(self):
        c = server.app.test_client(); self.post_login(client=c)
        self.post_form("/account/security/2fa/start", {}, client=c)
        self.post_form("/account/security/2fa/confirm", {"code": "123456"}, client=c)
        self.assertFalse(store.get_admin_user(self.conn, 1)["totp_enabled"])

    def test_secret_is_not_stored_until_confirmed(self):
        c = server.app.test_client(); self.post_login(client=c)
        self.post_form("/account/security/2fa/start", {}, client=c)
        self.assertIsNone(store.get_admin_user(self.conn, 1)["totp_secret"])

    def test_disable_needs_password_and_code(self):
        secret, codes, uid = self.enroll_2fa(); self.fresh_step(uid)
        c = server.app.test_client(); self.post_login(client=c)
        self.post_form("/login/2fa", {"code": code_for(secret)}, client=c)
        self.post_form("/account/security/2fa/disable", {"password": "wrong", "code": code_for(secret, 1)}, client=c)
        self.assertTrue(store.get_admin_user(self.conn, uid)["totp_enabled"])
        self.post_form("/account/security/2fa/disable", {"password": "pw-root", "code": code_for(secret, 1)}, client=c)
        self.assertFalse(store.get_admin_user(self.conn, uid)["totp_enabled"])
        self.assertIsNone(store.get_admin_user(self.conn, uid)["totp_secret"])

    def test_admin_can_reset_another_operators_2fa_but_auditor_cannot(self):
        secret, codes, uid = self.enroll_2fa("eve", "pw-eve")
        self.fresh_step(uid)
        eve = server.app.test_client()
        self.post_login("eve", "pw-eve", client=eve)
        self.post_form("/login/2fa", {"code": code_for(secret)}, client=eve)
        self.assertEqual(self.post_form(f"/users/{uid}/reset-2fa", {}, client=eve).status_code, 403)
        self.assertTrue(store.get_admin_user(self.conn, uid)["totp_enabled"])
        root = server.app.test_client(); self.post_login(client=root)
        self.assertEqual(self.post_form(f"/users/{uid}/reset-2fa", {}, client=root).status_code, 302)
        self.assertFalse(store.get_admin_user(self.conn, uid)["totp_enabled"])
        r = self.post_login("eve", "pw-eve", client=server.app.test_client(), addr="10.0.0.9")
        self.assertEqual(r.status_code, 302); self.assertFalse(r.headers["Location"].endswith("/login/2fa"))

    def test_admin_cannot_reset_their_own_through_the_operators_page(self):
        c = server.app.test_client(); self.post_login(client=c)
        self.assertEqual(self.post_form("/users/1/reset-2fa", {}, client=c).status_code, 404)

    def test_new_routes_need_a_session_and_csrf(self):
        for path in ["/account/security", "/account/security/2fa/start", "/users/2/reset-2fa"]:
            r = self.c.get(path) if path == "/account/security" else self.c.post(path)
            self.assertIn(r.status_code, (302, 400), path)
        c = server.app.test_client(); self.post_login(client=c)
        self.assertEqual(c.post("/account/security/2fa/start").status_code, 400)

    def test_operators_list_shows_2fa_state(self):
        self.enroll_2fa("eve", "pw-eve")
        root = server.app.test_client(); self.post_login(client=root)
        html = root.get("/users").get_data(as_text=True)
        self.assertIn("Reset 2FA", html)

    def test_recovery_codes_are_hashed_at_rest(self):
        secret, codes, uid = self.enroll_2fa()
        stored = store.get_admin_user(self.conn, uid)["totp_recovery"]
        for c in codes: self.assertNotIn(c.replace("-", ""), stored)


class RequiredMode(SignInBase):
    def setUp(self):
        super().setUp(); self._old = server.REQUIRE_2FA; server.REQUIRE_2FA = True

    def tearDown(self):
        server.REQUIRE_2FA = self._old

    def test_operator_without_2fa_is_confined_to_enrollment(self):
        c = server.app.test_client()
        r = self.post_login(client=c)
        self.assertTrue(r.headers["Location"].endswith("/account/security"))
        for path in ["/", "/devices", "/users", "/groups"]:
            self.assertTrue(c.get(path).headers["Location"].endswith("/account/security"), path)
        self.assertEqual(c.get("/account/security").status_code, 200)

    def test_finishing_enrollment_unlocks_the_console_and_it_cannot_be_turned_off(self):
        c = server.app.test_client(); self.post_login(client=c)
        self.post_form("/account/security/2fa/start", {}, client=c)
        html = c.get("/account/security").get_data(as_text=True)
        secret = re.search(r'font-size:1.1em;">([A-Z2-7 ]+)<', html).group(1).replace(" ", "")
        self.post_form("/account/security/2fa/confirm", {"code": code_for(secret)}, client=c)
        self.assertEqual(c.get("/").status_code, 200)
        self.post_form("/account/security/2fa/disable", {"password": "pw-root", "code": code_for(secret, 1)}, client=c)
        self.assertTrue(store.get_admin_user(self.conn, 1)["totp_enabled"])


class Pages(SignInBase):
    def test_new_pages_are_labelled_and_translated(self):
        secret, codes, uid = self.enroll_2fa(); self.fresh_step(uid)
        c = server.app.test_client(); self.post_login(client=c)
        pages = {"2fa": c.get("/login/2fa").get_data(as_text=True)}
        self.post_form("/login/2fa", {"code": code_for(secret)}, client=c)
        pages["security-on"] = c.get("/account/security").get_data(as_text=True)
        c.set_cookie("lang", "en-XA")
        pseudo = c.get("/account/security").get_data(as_text=True)
        self.assertIn("[", pseudo)
        for name, html in pages.items():
            p = audit(html)
            ids = {a.get("id") for t, a, _ in p.controls}
            for tag, a, in_label in p.controls:
                self.assertTrue(a.get("aria-label") or in_label or a.get("id") in p.labels_for, (name, a))
            self.assertEqual(p.mains, 1, name); self.assertTrue(p.skip_link, name); self.assertTrue(p.lang, name)


if __name__ == "__main__":
    unittest.main(verbosity=1)
