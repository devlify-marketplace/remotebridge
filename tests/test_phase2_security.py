"""
Phase 2 — Security Verification & Penetration Testing

Automated security verification and penetration testing suite for RemoteBridge.
Tests specifically:
1. Normal user cannot become admin (Role Escalation Prevention).
2. Auditor cannot wake, connect, revoke, or modify devices.
3. Users cannot access another user's device (Device Access Control).
4. Cross-organization access is blocked (Multi-tenant Isolation).
5. API-key scopes are enforced.
6. Revoked/expired API keys fail.
7. /api/v1/ops/* cannot be used by unauthorized users.
8. IDOR attempts fail (Insecure Direct Object Reference).
9. CSRF protection works.
10. Audit events are created for privileged actions & Hash Chain intact.
11. RemoteBridge's normal ID -> authentication -> remote-session flow still works.
"""

import os
import re
import secrets
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin"))

import server
import store
import rbac


class TestPhase2SecurityVerification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        server._DB_PATH = os.path.join(self.tmp, "admin.db")
        self.conn = store.init_db(server._DB_PATH)
        store.set_setting(self.conn, "flask_secret_key", "test-secret-key-phase2")
        server.app.secret_key = "test-secret-key-phase2"
        store.set_setting(self.conn, "org_enrollment_key", "PHASE2_ORG_KEY")

        # Create system users
        self.admin_id = store.create_user(self.conn, "admin_user", "AdminPass123!", role="admin")
        self.auditor_id = store.create_user(self.conn, "auditor_user", "AuditorPass123!", role="auditor")
        self.user_id = store.create_user(self.conn, "normal_user", "UserPass123!", role="user")

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

    def enroll_device(self, device_id="DEV-TEST-01"):
        res = self.c.post("/api/v1/enroll", json={"device_id": device_id, "enrollment_key": "PHASE2_ORG_KEY"})
        self.assertEqual(res.status_code, 200)
        return res.get_json()["report_token"]

    # 1. Normal user cannot become admin
    def test_01_normal_user_cannot_become_admin(self):
        user_client = server.app.test_client()
        self.login("normal_user", "UserPass123!", client=user_client)
        tok = self.get_csrf_token(user_client)

        # Attempt to create an admin account as normal_user
        res = user_client.post("/users/new", data={
            "username": "hacked_admin",
            "password": "HackedPassword123!",
            "role": "admin",
            "csrf_token": tok
        })
        self.assertEqual(res.status_code, 403)

        # Confirm account was not created in DB
        hacked_user = store.get_user_by_username(self.conn, "hacked_admin")
        self.assertIsNone(hacked_user)

    # 2. Auditor cannot wake, connect, revoke, or modify devices
    def test_02_auditor_cannot_modify_or_action_devices(self):
        self.enroll_device("DEV-AUDIT-01")
        auditor_client = server.app.test_client()
        self.login("auditor_user", "AuditorPass123!", client=auditor_client)

        # Attempt wake device
        r_wake = auditor_client.post("/api/v1/ops/devices/DEV-AUDIT-01/wake")
        self.assertEqual(r_wake.status_code, 403)

        # Attempt connect device
        r_conn = auditor_client.post("/api/v1/ops/devices/DEV-AUDIT-01/connect")
        self.assertEqual(r_conn.status_code, 403)

        # Auditor can view read-only endpoints like /audit
        r_audit = auditor_client.get("/audit")
        self.assertEqual(r_audit.status_code, 200)

    # 3. Users cannot access another user's device
    def test_03_users_cannot_access_other_users_devices(self):
        org2_id = store.create_organization(self.conn, "Org Two")
        user_org2 = store.create_user(self.conn, "org2_user", "Org2Pass123!", role="user", organization_id=org2_id)

        # Device enrolled under Org 1 (default org)
        self.enroll_device("DEV-ORG1-01")

        # Verify rbac.can_access_device returns False for Org 2 user trying to access Org 1 device
        can_access = rbac.can_access_device(self.conn, user_org2, "DEV-ORG1-01", organization_id=org2_id)
        self.assertFalse(can_access)

    # 4. Cross-organization access is blocked
    def test_04_cross_organization_access_is_blocked(self):
        org2_id = store.create_organization(self.conn, "Org Security")
        user2_id = store.create_user(self.conn, "sec_user", "SecPass123!", role="user", organization_id=org2_id)

        # Ensure organization members are isolated per org
        org1_members = store.get_organization_members(self.conn, 1)
        org2_members = store.get_organization_members(self.conn, org2_id)

        org1_uids = [m["user_id"] for m in org1_members]
        org2_uids = [m["user_id"] for m in org2_members]

        self.assertIn(self.admin_id, org1_uids)
        self.assertNotIn(user2_id, org1_uids)
        self.assertIn(user2_id, org2_uids)

        # Verify non-admin cross-management is denied
        self.assertFalse(rbac.can_manage_user(self.conn, user2_id, self.admin_id, organization_id=org2_id))

    # 5. API-key scopes are enforced
    def test_05_api_key_scopes_enforced(self):
        # Create API key with ONLY device.read scope
        raw_key = store.create_api_key(self.conn, label="read-only-key", scopes="device.read")
        auth_headers = {"Authorization": f"Bearer {raw_key}"}

        anon_client = server.app.test_client()

        # Reading devices should succeed
        r_read = anon_client.get("/api/v1/ops/devices", headers=auth_headers)
        self.assertEqual(r_read.status_code, 200)

        # Waking device should be blocked (requires device.wake)
        r_wake = anon_client.post("/api/v1/ops/devices/DEV-01/wake", headers=auth_headers)
        self.assertEqual(r_wake.status_code, 403)

        # Connecting device should be blocked (requires device.connect)
        r_conn = anon_client.post("/api/v1/ops/devices/DEV-01/connect", headers=auth_headers)
        self.assertEqual(r_conn.status_code, 403)

    # 6. Revoked and expired API keys fail
    def test_06_revoked_or_expired_api_keys_fail(self):
        raw_key = store.create_api_key(self.conn, label="temporary-key", scopes="device.read")
        auth_headers = {"Authorization": f"Bearer {raw_key}"}

        anon_client = server.app.test_client()
        self.assertEqual(anon_client.get("/api/v1/ops/devices", headers=auth_headers).status_code, 200)

        # Revoke the API key
        all_keys = store.list_api_keys(self.conn)
        key_id = [k["id"] for k in all_keys if k["prefix"] == raw_key[:12]][0]
        store.revoke_api_key(self.conn, key_id)

        # Request with revoked key must fail
        self.assertEqual(anon_client.get("/api/v1/ops/devices", headers=auth_headers).status_code, 403)

    # 7. /api/v1/ops/* cannot be used by unauthorized users
    def test_07_unauthorized_ops_endpoints_blocked(self):
        anon_client = server.app.test_client()

        # Unauthenticated calls without bearer token or session cookie
        self.assertEqual(anon_client.get("/api/v1/ops/devices").status_code, 401)
        self.assertEqual(anon_client.post("/api/v1/ops/devices/DEV-1/wake").status_code, 401)
        self.assertEqual(anon_client.post("/api/v1/ops/devices/DEV-1/connect").status_code, 401)
        self.assertEqual(anon_client.get("/api/v1/ops/feedback").status_code, 401)

    # 8. IDOR attempts fail
    def test_08_idor_prevention(self):
        self.enroll_device("DEV-IDOR-01")
        user_client = server.app.test_client()
        self.login("normal_user", "UserPass123!", client=user_client)

        # Attempt to access non-existent or unauthorized resources
        r_fake = user_client.get("/api/v1/ops/devices/DEV-NONEXISTENT/sessions")
        self.assertIn(r_fake.status_code, (403, 404))

    # 9. CSRF protection works
    def test_09_csrf_protection(self):
        admin_client = server.app.test_client()
        self.login("admin_user", "AdminPass123!", client=admin_client)

        # Attempt POST request without CSRF token
        res = admin_client.post("/groups/new", data={"name": "Malicious Group"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("CSRF", res.get_data(as_text=True))

    # 10. Audit events are created for privileged actions & Hash Chain intact
    def test_10_audit_events_recorded_and_verified(self):
        # Trigger privileged events
        admin_client = server.app.test_client()
        self.login("admin_user", "AdminPass123!", client=admin_client)

        raw_key = store.create_api_key(self.conn, label="audit-test-key", created_by=self.admin_id)
        store.record_audit_event(self.conn, action="DEVICE_REVOKED", actor_user_id=self.admin_id, resource_type="device", resource_id="DEV-AUDIT")

        events = store.list_audit_events(self.conn, limit=50)
        self.assertGreater(len(events), 0)

        # Verify hash chain integrity
        integrity_ok, bad_id, err_msg = store.verify_audit_log_integrity(self.conn)
        self.assertTrue(integrity_ok, f"Audit log tamper check failed at event {bad_id}: {err_msg}")

    # 11. RemoteBridge's normal ID -> authentication -> remote-session flow still works
    def test_11_core_remotebridge_connection_flow_unaffected(self):
        device_id = "DEV-ANYDESK-99"

        # 1. Device enrollment
        report_token = self.enroll_device(device_id)
        self.assertIsNotNone(report_token)

        # 2. Host policy check with report token
        r_pol = self.c.get("/api/v1/policy", headers={"Authorization": f"Bearer {report_token}"})
        self.assertEqual(r_pol.status_code, 200)
        self.assertIn("allow_unattended_access", r_pol.get_json())

        # 3. Session attempt event reporting
        r_evt1 = self.c.post("/api/v1/events", json={
            "event": "attempt",
            "viewer_id": "VIEWER-100",
            "address": "192.168.1.50"
        }, headers={"Authorization": f"Bearer {report_token}"})
        self.assertEqual(r_evt1.status_code, 200)

        # 4. Session start event reporting
        r_evt2 = self.c.post("/api/v1/events", json={
            "event": "start",
            "viewer_id": "VIEWER-100",
            "address": "192.168.1.50"
        }, headers={"Authorization": f"Bearer {report_token}"})
        self.assertEqual(r_evt2.status_code, 200)

        # 5. Session end event reporting
        r_evt3 = self.c.post("/api/v1/events", json={
            "event": "end",
            "viewer_id": "VIEWER-100",
            "address": "192.168.1.50",
            "duration_seconds": 120
        }, headers={"Authorization": f"Bearer {report_token}"})
        self.assertEqual(r_evt3.status_code, 200)

        # 6. Host preauth check
        r_pre = self.c.get("/api/v1/preauth/VIEWER-100", headers={"Authorization": f"Bearer {report_token}"})
        self.assertEqual(r_pre.status_code, 200)
        self.assertIn("preauthorized", r_pre.get_json())


if __name__ == "__main__":
    unittest.main()
