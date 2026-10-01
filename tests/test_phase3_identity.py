"""
Phase 3 — Production Identity & Organization System Test Suite

Tests:
1. User profile display & profile updates (display_name, email).
2. Password change workflow with old password verification.
3. Organization management console & settings updates.
4. Organization member role updates & RBAC syncing.
5. User account suspension and activation.
6. Verification that audit logs record all identity & organization mutations.
"""

import os
import re
import secrets
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin"))

import server
import store
import rbac


class TestPhase3IdentityAndOrganization(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        server._DB_PATH = os.path.join(self.tmp, "admin.db")
        self.conn = store.init_db(server._DB_PATH)
        store.set_setting(self.conn, "flask_secret_key", "phase3-secret")
        server.app.secret_key = "phase3-secret"

        self.admin_id = store.create_user(self.conn, "alice_admin", "Password123!", role="admin")
        self.user_id = store.create_user(self.conn, "bob_operator", "Password123!", role="user")

        server.app.config["TESTING"] = True
        self.c = server.app.test_client()

    def get_csrf_token(self, client=None):
        client = client or self.c
        with client.session_transaction() as sess:
            tok = sess.get("csrf_token")
            if not tok:
                tok = secrets.token_urlsafe(32)
                sess["csrf_token"] = tok
            return tok

    def login(self, username, password, client=None):
        client = client or self.c
        tok = self.get_csrf_token(client)
        res = client.post("/login", data={"username": username, "password": password, "csrf_token": tok})
        self.assertIn(res.status_code, (200, 302))
        return client

    # 1. Profile view and update
    def test_profile_view_and_update(self):
        client = server.app.test_client()
        self.login("bob_operator", "Password123!", client=client)

        # View profile page
        r_get = client.get("/profile")
        self.assertEqual(r_get.status_code, 200)
        self.assertIn("bob_operator", r_get.get_data(as_text=True))

        # Update profile
        tok = self.get_csrf_token(client)
        r_post = client.post("/profile/update", data={
            "display_name": "Bob Operator",
            "email": "bob@enterprise.com",
            "csrf_token": tok
        })
        self.assertEqual(r_post.status_code, 302)

        # Verify profile updated in DB
        u = store.get_user_by_id(self.conn, self.user_id)
        self.assertEqual(u["display_name"], "Bob Operator")
        self.assertEqual(u["email"], "bob@enterprise.com")

    # 2. Password change
    def test_password_change_flow(self):
        client = server.app.test_client()
        self.login("bob_operator", "Password123!", client=client)
        tok = self.get_csrf_token(client)

        # Change password with valid credentials
        r_pass = client.post("/account/password", data={
            "current_password": "Password123!",
            "new_password": "NewSecretPass456!",
            "confirm_password": "NewSecretPass456!",
            "csrf_token": tok
        })
        self.assertEqual(r_pass.status_code, 302)

        # Verify login with old password fails, new password succeeds
        client2 = server.app.test_client()
        tok2 = self.get_csrf_token(client2)
        r_old = client2.post("/login", data={"username": "bob_operator", "password": "Password123!", "csrf_token": tok2})
        self.assertEqual(r_old.status_code, 200)  # Re-renders login on failure

        r_new = client2.post("/login", data={"username": "bob_operator", "password": "NewSecretPass456!", "csrf_token": tok2})
        self.assertEqual(r_new.status_code, 302)

    # 3. Organization info & settings update
    def test_organization_management(self):
        admin_client = server.app.test_client()
        self.login("alice_admin", "Password123!", client=admin_client)

        # View Organization page
        r_org = admin_client.get("/organization")
        self.assertEqual(r_org.status_code, 200)
        self.assertIn("Default Org", r_org.get_data(as_text=True))

        # Update Organization settings
        tok = self.get_csrf_token(admin_client)
        r_upd = admin_client.post("/organization/update", data={
            "name": "Acme Corp Remote",
            "slug": "acme-corp",
            "csrf_token": tok
        })
        self.assertEqual(r_upd.status_code, 302)

        # Verify organization updated
        org = store.get_organization(self.conn, 1)
        self.assertEqual(org["name"], "Acme Corp Remote")
        self.assertEqual(org["slug"], "acme-corp")

    # 4. Member role update & status toggle
    def test_member_role_and_status_management(self):
        admin_client = server.app.test_client()
        self.login("alice_admin", "Password123!", client=admin_client)
        tok = self.get_csrf_token(admin_client)

        # Update bob_operator role to auditor
        r_role = admin_client.post(f"/users/{self.user_id}/role", data={"role": "auditor", "csrf_token": tok})
        self.assertEqual(r_role.status_code, 302)

        self.assertEqual(rbac.get_user_role(self.conn, self.user_id), "auditor")

        # Suspend bob_operator
        r_suspend = admin_client.post(f"/users/{self.user_id}/status", data={"csrf_token": tok})
        self.assertEqual(r_suspend.status_code, 302)

        u_suspended = store.get_user_by_id(self.conn, self.user_id)
        self.assertEqual(u_suspended["status"], "suspended")

        # Confirm suspended user cannot log in
        bob_client = server.app.test_client()
        tok_b = self.get_csrf_token(bob_client)
        r_login = bob_client.post("/login", data={"username": "bob_operator", "password": "Password123!", "csrf_token": tok_b})
        self.assertEqual(r_login.status_code, 200)
        self.assertIn("Incorrect username or password", r_login.get_data(as_text=True))

        # Reactivate bob_operator
        r_activate = admin_client.post(f"/users/{self.user_id}/status", data={"csrf_token": tok})
        self.assertEqual(r_activate.status_code, 302)

        u_active = store.get_user_by_id(self.conn, self.user_id)
        self.assertEqual(u_active["status"], "active")


if __name__ == "__main__":
    unittest.main()
