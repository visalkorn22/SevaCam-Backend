"""
KHQRService — public facade used by payments.py.

Usage:
    from app.services.khqr.service import KHQRService, KHQRPaymentRecord
    from app.core.config import settings

    svc = KHQRService(settings)
    record = svc.create_payment(bill_number="BKG-001", amount=25.00, currency="USD")
    # persist record fields to the payments table
    # then poll:
    result = await svc.check_status(record.md5)
"""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass

from .payload import build_khqr_payload, payload_md5
from .qr_image import generate_qr_png
from .status import KHQRPaymentStatus, KHQRStatusResult, check_by_md5

logger = logging.getLogger(__name__)


@dataclass
class KHQRPaymentRecord:
    """
    Everything needed to persist a KHQR payment row and display the QR to
    the customer.  Maps directly onto columns in the payments table plus
    a few extra fields for the API response.

    Suggested DB / metadata storage:
        payments.provider           = "bakong_khqr"
        payments.provider_reference = khqr_md5   (unique, used for status poll)
        payments.status             = "pending"
        payments.metadata JSONB     = {
            "khqr_payload":   payload,
            "khqr_md5":       md5,
            "khqr_bill_number": bill_number,
            "checkout_created_at": unix_ts,
        }
    The QR image is returned to the frontend as a base64 data URL; it is NOT
    stored in the DB (regenerate from payload if needed).
    """
    bill_number: str
    payload: str          # Full KHQR string (ready for qrcode.make)
    md5: str              # MD5 of payload — Bakong transaction lookup key
    amount: float | None  # None = open-amount
    currency: str         # "USD" or "KHR"
    qr_image_b64: str     # PNG as base64 string (no data: prefix)


class KHQRService:
    """
    Thin wrapper that wires config → payload builder → QR renderer → status checker.

    Accepts a settings object (from app.core.config) so it integrates cleanly
    with the existing DI pattern in the payments router.
    """

    def __init__(self, settings) -> None:  # type: ignore[annotation]
        self._settings = settings

    # ── Create ────────────────────────────────────────────────────────────────

    def create_payment(
        self,
        *,
        bill_number: str,
        amount: float | None = None,
        currency: str | None = None,
    ) -> KHQRPaymentRecord:
        """
        Generate a KHQR payload and QR PNG for one payment.

        Args:
            bill_number:  Your booking/payment reference (≤25 chars).  Shown
                          inside the Bakong app transaction detail.
            amount:       Exact charge.  Pass None for open-amount QR — the
                          customer types the amount in the Bakong app.
            currency:     "USD" or "KHR".  Defaults to KHQR_DEFAULT_CURRENCY.

        Returns:
            KHQRPaymentRecord — store .md5 as provider_reference and the
            other fields in payments.metadata.
        """
        s = self._settings
        resolved_currency = (currency or s.KHQR_DEFAULT_CURRENCY).upper()

        payload = build_khqr_payload(
            jwt_token=s.KHQR_JWT_TOKEN,
            bakong_account_id=s.KHQR_BAKONG_ACCOUNT_ID,
            phone_number=s.KHQR_ACCOUNT_INFORMATION,
            merchant_name=s.KHQR_MERCHANT_NAME,
            merchant_city=s.KHQR_MERCHANT_CITY,
            currency=resolved_currency,
            amount=amount,
            bill_number=bill_number,
            store_label=s.KHQR_STORE_LABEL or None,
            terminal_label=s.KHQR_TERMINAL_LABEL or None,
        )

        md5 = payload_md5(payload)
        qr_bytes = generate_qr_png(payload)
        qr_b64 = base64.b64encode(qr_bytes).decode("ascii")

        logger.info(
            "KHQR created: bill=%s amount=%s %s md5=%s",
            bill_number, amount, resolved_currency, md5,
        )

        return KHQRPaymentRecord(
            bill_number=bill_number,
            payload=payload,
            md5=md5,
            amount=amount,
            currency=resolved_currency,
            qr_image_b64=qr_b64,
        )

    # ── Status poll ───────────────────────────────────────────────────────────

    async def check_status(self, md5: str) -> KHQRStatusResult:
        """
        Async poll of Bakong for transaction status by MD5.

        Call every ~3 s after showing the QR, stop when COMPLETED or the
        session TTL is exceeded.  This method never writes to the DB —
        that is the responsibility of _sync_khqr_payment_status() in payments.py.
        """
        return await check_by_md5(
            md5,
            jwt_token=self._settings.KHQR_JWT_TOKEN,
            check_endpoint=self._settings.KHQR_CHECK_ENDPOINT,
            proxy_secret=getattr(self._settings, "KHQR_PROXY_SECRET", None) or None,
        )
