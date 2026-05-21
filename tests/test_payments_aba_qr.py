"""
Unit tests for ABA PayWay QR stabilisation changes.
All tests are isolated — no database, no real HTTP calls.
"""
import json
import os
import sys
import pytest
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Ensure backend/ is on sys.path so `import app.*` works when pytest is run
# from the backend/ directory.
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Provide a dummy DATABASE_URL so pydantic-settings does not raise when
# app.core.config.Settings is instantiated during import of payments module.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

# ---------------------------------------------------------------------------
# Patch the module-level `settings` object in payments AFTER import so all
# internal references (settings.ABA_PAYWAY_SYNC_GRACE_SECONDS, etc.) resolve
# to our mock values without touching a real .env or database.
# ---------------------------------------------------------------------------
_mock_settings = MagicMock()
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
_mock_settings.APP_URL = "http://localhost:3000"
_mock_settings.DEBUG = False

import app.api.payments as payments_module  # noqa: E402
payments_module.settings = _mock_settings


# ---------------------------------------------------------------------------
# Task 2 — amount hash format
# ---------------------------------------------------------------------------

def test_amount_hash_value_uses_fixed_point_format():
    """
    The hash input for amount must be "1.00", not "1.0".
    format(Decimal("1.00"), "f") gives "1.00".
    str(float(Decimal("1.00"))) gives "1.0" — the bug we are fixing.
    """
    amount_decimal = Decimal("1.00").quantize(Decimal("0.01"))
    # Old (buggy) approach
    buggy = str(float(amount_decimal))
    assert buggy == "1.0", f"Pre-condition: buggy approach gives {buggy!r}"
    # New (correct) approach
    correct = format(amount_decimal, "f")
    assert correct == "1.00", f"Expected '1.00', got {correct!r}"


def test_amount_hash_value_whole_number():
    """$10.00 must hash as "10.00" not "10.0"."""
    amount_decimal = Decimal("10.00").quantize(Decimal("0.01"))
    assert format(amount_decimal, "f") == "10.00"


def test_amount_hash_value_cents():
    """$1.50 must hash as "1.50" not "1.5"."""
    amount_decimal = Decimal("1.50").quantize(Decimal("0.01"))
    assert format(amount_decimal, "f") == "1.50"


# ---------------------------------------------------------------------------
# Task 3 — QR response validation
# ---------------------------------------------------------------------------

def _qr_has_image_only():
    return {
        "qrImage": "iVBORw0KGgoAAAA==",  # non-empty base64-like string
        "qrString": None,
    }


def _qr_has_string_only():
    return {
        "qrImage": None,
        "qrString": "00020101021229370016A000000677010111011300855561234560208TESTAPP5303840540110.005802KH5910Test Shop6010Phnom Penh63043D5A",
    }


def _qr_has_both():
    return {
        "qrImage": "iVBORw0KGgoAAAA==",
        "qrString": "00020101...",
    }


def _qr_has_neither():
    return {
        "qrImage": None,
        "qrString": None,
    }


def _eval_qr_condition(r: dict) -> bool:
    """Returns True if at least one of qrImage/qrString is valid (no error)."""
    has_qr_image = isinstance(r.get("qrImage"), str) and bool(r["qrImage"].strip())
    has_qr_string = isinstance(r.get("qrString"), str) and bool(r["qrString"].strip())
    return has_qr_image or has_qr_string


def test_qr_validation_passes_with_image_only():
    """If qrImage is present and qrString is absent, validation must pass."""
    assert _eval_qr_condition(_qr_has_image_only()) is True


def test_qr_validation_passes_with_string_only():
    """If qrString is present and qrImage is absent, validation must pass."""
    assert _eval_qr_condition(_qr_has_string_only()) is True


def test_qr_validation_passes_with_both():
    """If both present, validation must pass."""
    assert _eval_qr_condition(_qr_has_both()) is True


def test_qr_validation_fails_with_neither():
    """If both absent, validation must not pass (we would raise 502)."""
    assert _eval_qr_condition(_qr_has_neither()) is False


# ---------------------------------------------------------------------------
# Task 4 — transaction-detail "not found" sentinel
# ---------------------------------------------------------------------------

from fastapi import HTTPException  # noqa: E402


def _make_detail_response(code: str, message: str, data: dict | None = None) -> dict:
    """Build a PayWay transaction-detail response dict."""
    return {
        "status": {"code": code, "message": message},
        "data": data or {},
    }


def _check_sentinel_logic(response_payload: dict) -> dict | None:
    """
    Mirrors the sentinel logic added to _fetch_payway_transaction_detail.
    Returns {"_not_found": True} when a not-found message is detected,
    or None when the response is a success (code 00) or a real error.
    """
    status = response_payload.get("status") or {}
    status_code = str(status.get("code") or "").strip()
    if status_code in {"0", "00"}:
        return None  # success — sentinel never triggered on success

    _NOT_FOUND_PHRASES = ("transaction not found", "no transaction", "not found")
    status_message = str(status.get("message") or "").strip().lower()
    if any(phrase in status_message for phrase in _NOT_FOUND_PHRASES):
        return {"_not_found": True}

    return None  # real error — would raise, sentinel not triggered


def test_sentinel_triggered_on_transaction_not_found_message():
    response = _make_detail_response("01", "Transaction not found")
    assert _check_sentinel_logic(response) == {"_not_found": True}


def test_sentinel_triggered_on_no_transaction_message():
    response = _make_detail_response("99", "No transaction record")
    assert _check_sentinel_logic(response) == {"_not_found": True}


def test_sentinel_triggered_case_insensitive():
    response = _make_detail_response("01", "TRANSACTION NOT FOUND")
    assert _check_sentinel_logic(response) == {"_not_found": True}


def test_sentinel_not_triggered_on_other_errors():
    """Real errors (e.g. invalid merchant) must NOT produce the sentinel."""
    response = _make_detail_response("05", "Invalid merchant credentials")
    assert _check_sentinel_logic(response) is None  # would raise — correct


def test_sentinel_not_triggered_on_success():
    """Status 00 never goes through the sentinel path."""
    response = _make_detail_response("00", "Success", data={"payment_status": "SUCCESS"})
    assert _check_sentinel_logic(response) is None  # success path


# ---------------------------------------------------------------------------
# Task 6 — grace window + sentinel handling in _sync_payway_payment_status
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402 (already available but explicit for clarity)


def _make_payment(age_seconds: float, status: str = "pending") -> dict:
    """
    Build a minimal fake payment dict.
    age_seconds: how old the payment is (positive = created that many seconds ago).
    """
    created_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return {
        "id": "pay_test_001",
        "booking_id": "book_test_001",
        "provider": "aba_payway",
        "provider_reference": "txn_123456",
        "amount": Decimal("10.00"),
        "currency": "USD",
        "status": status,
        "created_at": created_at,
    }


@pytest.mark.asyncio
async def test_grace_window_skips_payway_call_for_fresh_payment():
    """
    Payment created 10 seconds ago (within default 60s grace window).
    _fetch_payway_transaction_detail must NOT be called.
    """
    payment = _make_payment(age_seconds=10)

    with patch.object(
        payments_module,
        "_fetch_payway_transaction_detail",
        new_callable=AsyncMock,
    ) as mock_fetch:
        result = await payments_module._sync_payway_payment_status(
            db=MagicMock(), payment=payment
        )

    mock_fetch.assert_not_called()
    assert result["status"] == "pending"


@pytest.mark.asyncio
async def test_grace_window_allows_payway_call_for_old_payment():
    """
    Payment created 90 seconds ago (outside default 60s grace window).
    _fetch_payway_transaction_detail IS called.
    """
    payment = _make_payment(age_seconds=90)

    with patch.object(
        payments_module,
        "_fetch_payway_transaction_detail",
        new_callable=AsyncMock,
        return_value={"_not_found": True},
    ) as mock_fetch:
        result = await payments_module._sync_payway_payment_status(
            db=MagicMock(), payment=payment
        )

    mock_fetch.assert_called_once_with("txn_123456")
    assert result["status"] == "pending"


@pytest.mark.asyncio
async def test_sentinel_not_found_after_grace_window_stays_pending():
    """
    After the grace window, if PayWay returns the _not_found sentinel,
    the payment stays pending — no 502 surfaced to the caller.
    """
    payment = _make_payment(age_seconds=120)

    with patch.object(
        payments_module,
        "_fetch_payway_transaction_detail",
        new_callable=AsyncMock,
        return_value={"_not_found": True},
    ):
        result = await payments_module._sync_payway_payment_status(
            db=MagicMock(), payment=payment
        )

    assert result["status"] == "pending"


@pytest.mark.asyncio
async def test_grace_window_with_naive_datetime():
    """
    created_at with no tzinfo (naive UTC, as returned by some DB drivers)
    must be handled safely — must not raise TypeError.
    """
    payment = _make_payment(age_seconds=5)
    # Strip timezone info to simulate naive datetime from DB
    payment["created_at"] = payment["created_at"].replace(tzinfo=None)

    with patch.object(
        payments_module,
        "_fetch_payway_transaction_detail",
        new_callable=AsyncMock,
    ) as mock_fetch:
        result = await payments_module._sync_payway_payment_status(
            db=MagicMock(), payment=payment
        )

    mock_fetch.assert_not_called()  # still within grace window
    assert result["status"] == "pending"


@pytest.mark.asyncio
async def test_real_payway_error_after_grace_window_stays_pending():
    """
    A real PayWay error (HTTPException, not the sentinel) after the grace
    window must still be caught and return the payment as pending.
    This preserves the existing behaviour for network/server errors.
    """
    payment = _make_payment(age_seconds=120)

    with patch.object(
        payments_module,
        "_fetch_payway_transaction_detail",
        new_callable=AsyncMock,
        side_effect=HTTPException(status_code=502, detail="PayWay server error"),
    ):
        result = await payments_module._sync_payway_payment_status(
            db=MagicMock(), payment=payment
        )

    assert result["status"] == "pending"
