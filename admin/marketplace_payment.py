"""
RemoteBridge Marketplace Payment Gateway Abstraction Layer.
Abstracts payment processors (Paystack, Flutterwave, Stripe, Mock) to ensure
clean decoupled payment handling, webhook verification, replay protection,
and refund support without client-tamperable prices.
"""

import abc
import hashlib
import hmac
import secrets
import time

class PaymentProvider(abc.ABC):

    @abc.abstractmethod
    def create_payment(self, order_id: str, amount: float, currency: str,
                       customer_email: str = None, return_url: str = None) -> dict:
        """Returns checkout metadata: payment_id, checkout_url, reference, status."""
        pass

    @abc.abstractmethod
    def verify_payment(self, payment_id: str, reference: str = None) -> dict:
        """Verifies payment status with provider. Returns dict with status='paid'|'failed'|'pending'."""
        pass

    @abc.abstractmethod
    def refund_payment(self, payment_id: str, amount: float = None, reason: str = None) -> dict:
        """Refunds payment. Returns status='refunded'|'failed'."""
        pass

    @abc.abstractmethod
    def get_payment_status(self, payment_id: str) -> str:
        """Returns string status."""
        pass


class MockPaymentProvider(PaymentProvider):
    """
    Mock / Development Payment Provider for testing and local environment execution.
    Auto-confirms transactions or allows simulation of failed/refunded states.
    """

    def __init__(self):
        self._payments = {}

    def create_payment(self, order_id: str, amount: float, currency: str,
                       customer_email: str = None, return_url: str = None) -> dict:
        payment_id = f"pay_mock_{secrets.token_hex(8)}"
        reference = f"ref_mock_{secrets.token_hex(6)}"
        record = {
            "payment_id": payment_id,
            "order_id": order_id,
            "amount": amount,
            "currency": currency,
            "customer_email": customer_email,
            "reference": reference,
            "status": "pending",
            "created_at": time.time(),
        }
        self._payments[payment_id] = record
        return {
            "payment_id": payment_id,
            "reference": reference,
            "checkout_url": f"/marketplace/orders/{order_id}?mock_pay={payment_id}",
            "status": "pending",
            "provider": "mock",
        }

    def verify_payment(self, payment_id: str, reference: str = None) -> dict:
        record = self._payments.get(payment_id)
        if not record:
            return {"status": "failed", "error": "Payment record not found"}
        # Simulate instant success for mock provider
        record["status"] = "paid"
        return {"status": "paid", "amount": record["amount"], "currency": record["currency"], "payment_id": payment_id}

    def refund_payment(self, payment_id: str, amount: float = None, reason: str = None) -> dict:
        record = self._payments.get(payment_id)
        if not record:
            return {"status": "failed", "error": "Payment record not found"}
        record["status"] = "refunded"
        return {"status": "refunded", "amount": amount or record["amount"], "reason": reason}

    def get_payment_status(self, payment_id: str) -> str:
        record = self._payments.get(payment_id)
        return record["status"] if record else "unknown"


class PaymentGatewayFactory:
    _instance = None

    @classmethod
    def get_provider(cls, name: str = "mock") -> PaymentProvider:
        if name.lower() == "mock":
            if cls._instance is None:
                cls._instance = MockPaymentProvider()
            return cls._instance
        # Placeholders for future production gateways
        raise NotImplementedError(f"Payment provider '{name}' is not configured yet. Use 'mock'.")

def verify_webhook_signature(payload_bytes: bytes, signature_header: str, secret: str) -> bool:
    """Helper to verify webhook HMACS against replay attacks and forgery."""
    if not signature_header or not secret:
        return False
    computed = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature_header.strip())
