"""
KHQR payload generator — uses the official bakong-khqr SDK so the
payload is always spec-compliant and accepted by all Bakong-compatible apps.

The old manual TLV/CRC16 builder is replaced entirely by this thin wrapper.
"""
from __future__ import annotations

import hashlib

from bakong_khqr import KHQR as _SDK


def build_khqr_payload(
    *,
    jwt_token: str,
    bakong_account_id: str,
    phone_number: str,
    merchant_name: str,
    merchant_city: str,
    currency: str,
    amount: float | None = None,
    bill_number: str | None = None,
    store_label: str | None = None,
    terminal_label: str | None = None,
) -> str:
    """
    Generate a valid KHQR payload string using the official Bakong SDK.

    Returns:
        Complete KHQR string ready for QR rendering and MD5 hashing.
    """
    sdk = _SDK(jwt_token)
    return sdk.create_qr(
        bank_account=bakong_account_id,
        merchant_name=merchant_name[:25],
        merchant_city=merchant_city[:15],
        amount=amount,
        currency=currency.upper(),
        store_label=store_label or "",
        phone_number=phone_number,
        bill_number=(bill_number or "")[:25],
        terminal_label=terminal_label or "",
        static=False,
    )


def payload_md5(payload: str) -> str:
    """
    MD5 of the KHQR payload string — used as the Bakong transaction lookup key.
    Always returns lowercase hex (32 chars).
    """
    return hashlib.md5(payload.encode("utf-8")).hexdigest()
