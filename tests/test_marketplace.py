"""
Automated unit & integration test suite for RemoteBridge IT Services Marketplace.
Tests schema creation, database operations, business logic, payment abstraction,
order lifecycles, reviews, fee calculations, session tokens, and REST API endpoints.
"""

import json
import os
import sys
import unittest

# Ensure admin directory is in sys.path
admin_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "admin"))
if admin_dir not in sys.path:
    sys.path.insert(0, admin_dir)

import store
import marketplace_store
from marketplace_payment import PaymentGatewayFactory, MockPaymentProvider
from server import app

import tempfile

class TestMarketplaceStore(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()
        store.init_db(self.db_path)
        self.conn = store.get_conn(self.db_path)
        # Create test users
        self.cust_id = store.create_admin_user(self.conn, "customer_user", "pass123", role="auditor")
        self.prov_id = store.create_admin_user(self.conn, "provider_user", "pass123", role="auditor")
        self.admin_id = store.create_admin_user(self.conn, "admin_user", "pass123", role="admin")

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)


    def test_categories_seeded_and_crud(self):
        cats = marketplace_store.list_categories(self.conn)
        self.assertGreaterEqual(len(cats), 5)
        
        new_cat_id = marketplace_store.create_category(self.conn, "Cloud Architecture", "cloud-arch", "Cloud design", "server")
        fetched = marketplace_store.get_category_by_id(self.conn, new_cat_id)
        self.assertEqual(fetched["name"], "Cloud Architecture")
        self.assertEqual(fetched["slug"], "cloud-arch")

    def test_provider_profile_lifecycle(self):
        prof_id = marketplace_store.create_provider_profile(
            self.conn, self.prov_id, display_name="Alex Systems", username="alexsys",
            headline="Cloud & Linux Admin", country="United States", city="Seattle",
            skills=["Linux", "Docker", "VAAPI"]
        )
        self.assertIsNotNone(prof_id)

        prof = marketplace_store.get_provider_profile(self.conn, prof_id)
        self.assertEqual(prof["display_name"], "Alex Systems")
        self.assertEqual(prof["username"], "alexsys")
        self.assertIn("Linux", prof["skills"])

        marketplace_store.set_provider_verification(self.conn, prof_id, "verified")
        prof_updated = marketplace_store.get_provider_profile(self.conn, prof_id)
        self.assertEqual(prof_updated["verification_status"], "verified")

    def test_service_creation_and_search(self):
        prof_id = marketplace_store.create_provider_profile(
            self.conn, self.prov_id, display_name="DevOps Guru", username="devopsguru"
        )
        cat = marketplace_store.get_category_by_slug(self.conn, "remote-desktop-support")
        srv_id = marketplace_store.create_service(
            self.conn, provider_id=prof_id, category_id=cat["id"],
            title="Instant Active Directory Fix", description="Fix domain controller & DNS issues",
            price=99.00, skills=["Active Directory", "DNS"]
        )
        
        srv = marketplace_store.get_service(self.conn, srv_id)
        self.assertEqual(srv["title"], "Instant Active Directory Fix")
        self.assertEqual(srv["price"], 99.00)
        self.assertIn("DNS", srv["skills"])

        # Test Search
        results, total = marketplace_store.search_services(self.conn, query="Active Directory")
        self.assertEqual(total, 1)
        self.assertEqual(results[0]["id"], srv_id)

    def test_fee_calculations(self):
        marketplace_store.set_platform_fee_config(self.conn, percentage=10.0, fixed_fee=0.0, min_fee=5.0, max_fee=100.0)
        fee, provider_amt = marketplace_store.calculate_fees(self.conn, 200.0)
        self.assertEqual(fee, 20.0)
        self.assertEqual(provider_amt, 180.0)

    def test_order_lifecycle(self):
        prof_id = marketplace_store.create_provider_profile(
            self.conn, self.prov_id, display_name="Support Tech", username="supporttech"
        )
        cat = marketplace_store.list_categories(self.conn)[0]
        srv_id = marketplace_store.create_service(
            self.conn, provider_id=prof_id, category_id=cat["id"],
            title="Fix Firewall Rules", description="Configure pfSense / iptables",
            price=150.00
        )

        order = marketplace_store.create_order(
            self.conn, customer_id=self.cust_id, provider_id=prof_id, service_id=srv_id,
            title="Fix Firewall Rules", description="Allow port 443 for relay", amount=150.00
        )
        self.assertEqual(order["status"], "pending")
        self.assertEqual(order["payment_status"], "unpaid")

        # Accept order
        accepted_order = marketplace_store.update_order_status(self.conn, order["id"], "accepted", actor_id=self.prov_id)
        self.assertEqual(accepted_order["status"], "accepted")

        # Complete order
        completed_order = marketplace_store.update_order_status(self.conn, order["id"], "completed", actor_id=self.prov_id)
        self.assertEqual(completed_order["status"], "completed")

        # Verify payout generated
        payouts = marketplace_store.list_payouts(self.conn, provider_id=prof_id)
        self.assertEqual(len(payouts), 1)

    def test_order_messaging_and_reviews(self):
        prof_id = marketplace_store.create_provider_profile(
            self.conn, self.prov_id, display_name="Support Tech 2", username="supporttech2"
        )
        cat = marketplace_store.list_categories(self.conn)[0]
        srv_id = marketplace_store.create_service(
            self.conn, provider_id=prof_id, category_id=cat["id"],
            title="VPN Setup", description="Setup WireGuard", price=50.00
        )
        order = marketplace_store.create_order(
            self.conn, customer_id=self.cust_id, provider_id=prof_id, service_id=srv_id,
            title="VPN Setup", description="Setup WireGuard server", amount=50.00
        )
        marketplace_store.update_order_status(self.conn, order["id"], "completed", actor_id=self.prov_id)

        # Message thread
        msg_id = marketplace_store.add_order_message(self.conn, order["id"], self.cust_id, "Hello, system is ready!")
        msgs = marketplace_store.list_order_messages(self.conn, order["id"])
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["message"], "Hello, system is ready!")

        # Review
        rev_id = marketplace_store.create_review(self.conn, order["id"], self.cust_id, 5, "Excellent work!")
        prof = marketplace_store.get_provider_profile(self.conn, prof_id)
        self.assertEqual(prof["average_rating"], 5.0)
        self.assertEqual(prof["review_count"], 1)

    def test_single_use_session_tokens(self):
        prof_id = marketplace_store.create_provider_profile(
            self.conn, self.prov_id, display_name="Support Tech 3", username="supporttech3"
        )
        cat = marketplace_store.list_categories(self.conn)[0]
        srv_id = marketplace_store.create_service(
            self.conn, provider_id=prof_id, category_id=cat["id"],
            title="Remote Desktop Fix", description="Fix display driver", price=40.00
        )
        order = marketplace_store.create_order(
            self.conn, customer_id=self.cust_id, provider_id=prof_id, service_id=srv_id,
            title="Remote Desktop Fix", description="Fix display driver", amount=40.00, device_id="test-device-123"
        )

        token = marketplace_store.create_marketplace_session_token(
            self.conn, order_id=order["id"], customer_id=self.cust_id, provider_id=prof_id, device_id="test-device-123"
        )
        self.assertIsNotNone(token)

        # Verify and burn token
        burnt = marketplace_store.verify_and_burn_session_token(self.conn, token)
        self.assertIsNotNone(burnt)
        self.assertEqual(burnt["device_id"], "test-device-123")

        # Second verification should fail (burned token protection)
        second_verify = marketplace_store.verify_and_burn_session_token(self.conn, token)
        self.assertIsNone(second_verify)

class TestMarketplaceAPI(unittest.TestCase):

    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.db_path = store.DB_PATH
        store.init_db(self.db_path)

    def test_get_categories_api(self):
        response = self.client.get("/api/v1/marketplace/categories")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn("categories", data)
        self.assertGreaterEqual(len(data["categories"]), 5)

    def test_search_services_api(self):
        response = self.client.get("/api/v1/marketplace/services")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn("services", data)
        self.assertIn("total", data)

if __name__ == "__main__":
    unittest.main()
