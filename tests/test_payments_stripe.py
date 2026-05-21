"""
Unit tests for Stripe payment integration.
All tests are isolated — no database, no real HTTP calls.
"""
import hashlib
import hmac
import json
import os
import sys
import time
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

_mock_settings = MagicMock()
_mock_settings.STRIPE_API_KEY = "sk_test_abc123"
_mock_settings.STRIPE_API_URL = "https://api.stripe.com/v1"
_mock_settings.STRIPE_WEBHOOK_SECRET = "whsec_testsecret"
_mock_settings.STRIPE_TIMEOUT_SECONDS = 20
_mock_settings.STRIPE_RETURN_URL = None
_mock_settings.STRIPE_CANCEL_URL = None
_mock_settings.APP_URL = "http://localhost:3000"
_mock_settings.DEBUG = False
# ABA fields referenced in module-level guards
_mock_settings.ABA_PAYWAY_MERCHANT_ID = "test_merchant"
_mock_settings.ABA_PAYWAY_API_KEY = "test_api_key"
_mock_settings.ABA_PAYWAY_API_URL = "https://checkout-sandbox.payway.com.kh/api/payment-gateway/v1"
_mock_settings.ABA_PAYWAY_QR_PATH = "/payments/generate-qr"
_mock_settings.ABA_PAYWAY_TRANSACTION_DETAIL_PATH = "/payments/transaction-detail"
_mock_settings.ABA_PAYWAY_QR_LIFETIME_MINUTES = 6
_mock_settings.ABA_PAYWAY_QR_IMAGE_TEMPLATE = "template3_color"
_mock_settings.ABA_PAYWAY_TIMEOUT_SECONDS = 20
_mock_settings.ABA_PAYWAY_SYNC_GRACE_SECONDS = 60
_mock_settings.ABA_PAYWAY_CALLBACK_URL = ""
_mock_settings.ABA_PAYWAY_WEBHOOK_PATH = "/api/payments/webhook/payway"
_mock_settings.ABA_PAYWAY_WEBHOOK_SECRET = None

import app.api.payments as payments_module  # noqa: E402
payments_module.settings = _mock_settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stripe_payment(status: str = "pending") -> dict:
    return {
        "id": "pay_stripe_001",
        "booking_id": "book_001",
        "provider": "stripe",
        "provider_reference": "cs_test_abc123",
        "amount": Decimal("50.00"),
        "currency": "USD",
        "status": status,
        "metadata": {},
    }


def _make_stripe_session(
    payment_status: str = "paid",
    session_status: str = "complete",
    amount_total: int = 5000,
    currency: str = "usd",
    session_id: str = "cs_test_abc123",
) -> dict:
    return {
        "id": session_id,
        "object": "checkout.session",
        "status": session_status,
        "payment_status": payment_status,
        "amount_total": amount_total,
        "currency": currency,
        "payment_intent": "pi_test_xyz",
        "client_reference_id": "pay_stripe_001",
        "metadata": {"payment_id": "pay_stripe_001"},
    }


def _make_stripe_sig(secret: str, body: bytes, timestamp: int | None = None) -> str:
    """Build a valid Stripe-Signature header value."""
    ts = timestamp if timestamp is not None else int(time.time())
    payload = f"{ts}.{body.decode('utf-8')}"
    sig = hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"t={ts},v1={sig}"


# ---------------------------------------------------------------------------
# _to_stripe_minor_units
# ---------------------------------------------------------------------------

def test_minor_units_whole_dollar():
    assert payments_module._to_stripe_minor_units(Decimal("10.00")) == 1000


def test_minor_units_cents():
    assert payments_module._to_stripe_minor_units(Decimal("1.50")) == 150


def test_minor_units_zero_cents():
    assert payments_module._to_stripe_minor_units(Decimal("5.00")) == 500


# ---------------------------------------------------------------------------
# _map_stripe_status
# ---------------------------------------------------------------------------

def test_map_stripe_status_paid():
    assert payments_module._map_stripe_status({"payment_status": "paid"}) == "completed"


def test_map_stripe_status_unpaid():
    assert payments_module._map_stripe_status({"payment_status": "unpaid"}) == "pending"


def test_map_stripe_status_expired():
    assert payments_module._map_stripe_status({"payment_status": "expired"}) == "failed"


def test_map_stripe_status_session_expired():
    assert payments_module._map_stripe_status({"status": "expired", "payment_status": ""}) == "failed"


def test_map_stripe_status_no_payment_required():
    assert payments_module._map_stripe_status({"payment_status": "no_payment_required"}) == "completed"


# ---------------------------------------------------------------------------
# _verify_stripe_signature
# ---------------------------------------------------------------------------

def test_stripe_signature_valid():
    body = b'{"type":"checkout.session.completed"}'
    sig = _make_stripe_sig("whsec_testsecret", body)
    with patch.object(payments_module, "settings", _mock_settings):
        assert payments_module._verify_stripe_signature(body, sig) is True


def test_stripe_signature_wrong_secret():
    body = b'{"type":"checkout.session.completed"}'
    sig = _make_stripe_sig("whsec_WRONGSECRET", body)
    with patch.object(payments_module, "settings", _mock_settings):
        assert payments_module._verify_stripe_signature(body, sig) is False


def test_stripe_signature_missing_rejects():
    body = b'{"type":"checkout.session.completed"}'
    with patch.object(payments_module, "settings", _mock_settings):
        assert payments_module._verify_stripe_signature(body, None) is False


def test_stripe_signature_replayed_rejects():
    """Timestamp older than 300s must be rejected."""
    body = b'{"type":"checkout.session.completed"}'
    old_ts = int(time.time()) - 400  # 400s ago — outside 300s window
    sig = _make_stripe_sig("whsec_testsecret", body, timestamp=old_ts)
    with patch.object(payments_module, "settings", _mock_settings):
        assert payments_module._verify_stripe_signature(body, sig) is False


def test_stripe_signature_no_secret_debug_mode():
    """When no webhook secret is configured and DEBUG=True, signature is bypassed."""
    mock = MagicMock()
    mock.STRIPE_WEBHOOK_SECRET = None
    mock.DEBUG = True
    with patch.object(payments_module, "settings", mock):
        assert payments_module._verify_stripe_signature(b"body", None) is True


def test_stripe_signature_no_secret_prod_mode():
    """When no webhook secret is configured and DEBUG=False, reject."""
    mock = MagicMock()
    mock.STRIPE_WEBHOOK_SECRET = None
    mock.DEBUG = False
    with patch.object(payments_module, "settings", mock):
        assert payments_module._verify_stripe_signature(b"body", None) is False


# ---------------------------------------------------------------------------
# _sync_stripe_payment_status — amount / currency mismatch guards
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_stripe_amount_mismatch_raises():
    """If Stripe session amount does not match payment record, raise 400."""
    payment = _make_stripe_payment()
    session = _make_stripe_session(amount_total=9999)  # $99.99 ≠ $50.00

    with patch.object(
        payments_module,
        "_fetch_stripe_session",
        new_callable=AsyncMock,
        return_value=session,
    ):
        with pytest.raises(Exception) as exc_info:
            await payments_module._sync_stripe_payment_status(
                db=MagicMock(), payment=payment, stripe_session_id="cs_test_abc123"
            )
    assert exc_info.value.status_code == 400
    assert "amount" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_sync_stripe_currency_mismatch_raises():
    """If Stripe session currency does not match payment record, raise 400."""
    payment = _make_stripe_payment()
    session = _make_stripe_session(amount_total=5000, currency="eur")

    with patch.object(
        payments_module,
        "_fetch_stripe_session",
        new_callable=AsyncMock,
        return_value=session,
    ):
        with pytest.raises(Exception) as exc_info:
            await payments_module._sync_stripe_payment_status(
                db=MagicMock(), payment=payment, stripe_session_id="cs_test_abc123"
            )
    assert exc_info.value.status_code == 400
    assert "currency" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_sync_stripe_session_id_mismatch_raises():
    """If the supplied stripe_session_id conflicts with provider_reference, raise 400."""
    payment = _make_stripe_payment()  # provider_reference = cs_test_abc123
    with pytest.raises(Exception) as exc_info:
        await payments_module._sync_stripe_payment_status(
            db=MagicMock(), payment=payment, stripe_session_id="cs_test_DIFFERENT"
        )
    assert exc_info.value.status_code == 400
    assert "mismatch" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_sync_stripe_completes_payment_on_paid():
    """Paid Stripe session must update payment status to completed."""
    payment = _make_stripe_payment(status="pending")
    session = _make_stripe_session(payment_status="paid", amount_total=5000, currency="usd")

    updated = {**payment, "status": "completed"}

    with (
        patch.object(
            payments_module,
            "_fetch_stripe_session",
            new_callable=AsyncMock,
            return_value=session,
        ),
        patch.object(payments_module, "_apply_payment_status", return_value="completed"),
        patch.object(payments_module, "_load_payment_record", return_value=updated),
        patch.object(payments_module, "_send_booking_emails"),
    ):
        result = await payments_module._sync_stripe_payment_status(
            db=MagicMock(), payment=payment, stripe_session_id=None
        )
    assert result["status"] == "completed"


# ---------------------------------------------------------------------------
# API version header — smoke test via mock HTTP
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_stripe_checkout_sends_api_version_header():
    """
    _create_stripe_checkout must send Stripe-Version: 2026-02-25.clover
    and use automatic_payment_methods, not a hardcoded card method.
    """
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "id": "cs_test_new",
        "url": "https://checkout.stripe.com/pay/cs_test_new",
    }

    payment_create = MagicMock()
    payment_create.booking_id = "book_001"
    payment_create.amount = Decimal("50.00")
    payment_create.currency = "USD"

    captured_headers = {}
    captured_data = {}

    class MockAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, data=None, headers=None):
            captured_headers.update(headers or {})
            captured_data.update(data or {})
            return mock_response

    db = MagicMock()
    db.execute.return_value.mappings.return_value.first.return_value = {
        "id": "book_001",
        "service_name": "Test Service",
    }

    with (
        patch("httpx.AsyncClient", MockAsyncClient),
        patch.object(payments_module, "settings", _mock_settings),
    ):
        url, session_id = await payments_module._create_stripe_checkout(
            payment_id="pay_001",
            payment=payment_create,
            db=db,
        )

    assert captured_headers.get("Stripe-Version") == "2026-02-25.clover"
    assert captured_data.get("payment_method_types[0]") == "card"
    assert "automatic_payment_methods[enabled]" not in captured_data
    assert url == "https://checkout.stripe.com/pay/cs_test_new"
    assert session_id == "cs_test_new"
