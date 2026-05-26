"""
Bakong transaction status checker.

Calls POST /v1/check_transaction_by_md5 with the stored MD5 hash.

Bakong response codes
─────────────────────
  0  → Transaction found and complete (money received)
  1  → Transaction not found — still pending, poll again
  other → Unexpected — treat as pending, log and retry later
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10


class KHQRPaymentStatus(str, Enum):
    PENDING = "pending"      # Not found yet — keep polling
    COMPLETED = "completed"  # responseCode == 0, money received
    ERROR = "error"          # Network / decode error — retry later
    GEO_BLOCKED = "geo_blocked"  # Bakong API blocked our server IP (403) — stop polling


@dataclass
class KHQRStatusResult:
    """Structured result from a single status-check call."""

    status: KHQRPaymentStatus
    response_code: int | None = None
    response_message: str | None = None

    # Populated only when status == COMPLETED
    from_account_id: str | None = None
    to_account_id: str | None = None
    amount: float | None = None
    currency: str | None = None
    description: str | None = None
    created_date_ms: int | None = None

    # Full raw response kept for debug logging / metadata storage
    raw: dict = field(default_factory=dict)


async def check_by_md5(
    md5: str,
    *,
    jwt_token: str,
    check_endpoint: str,
) -> KHQRStatusResult:
    """
    Async check of a KHQR transaction status using its MD5 hash.

    This function is deliberately side-effect-free: it does NOT write to the
    database.  The caller (_sync_khqr_payment_status in payments.py) owns
    the DB update so it can follow the same pattern as ABA PayWay and Stripe.

    Args:
        md5:             Lowercase hex MD5 of the original KHQR payload.
        jwt_token:       Bakong JWT (KHQR_JWT_TOKEN from .env).
        check_endpoint:  Full URL (KHQR_CHECK_ENDPOINT from .env).

    Returns:
        KHQRStatusResult — never raises; network errors map to status=ERROR.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {jwt_token}",
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                check_endpoint,
                json={"md5": md5},
                headers=headers,
            )
    except httpx.TimeoutException:
        logger.warning("Bakong status check timed out (md5=%s)", md5)
        return KHQRStatusResult(status=KHQRPaymentStatus.ERROR)
    except httpx.NetworkError as exc:
        logger.error("Bakong API network error: %s", exc)
        return KHQRStatusResult(status=KHQRPaymentStatus.ERROR)

    if response.status_code == 401:
        logger.error(
            "Bakong JWT rejected (401) — rotate KHQR_JWT_TOKEN. md5=%s", md5
        )
        return KHQRStatusResult(
            status=KHQRPaymentStatus.ERROR,
            response_code=401,
            response_message="Unauthorised — JWT may be expired or invalid",
        )

    if response.status_code == 403:
        logger.error(
            "Bakong API IP-blocked (403) — this server's IP is not whitelisted by NBC. "
            "Manual payment confirmation required. md5=%s",
            md5,
        )
        return KHQRStatusResult(
            status=KHQRPaymentStatus.GEO_BLOCKED,
            response_code=403,
            response_message="Bakong API blocked this server's IP address (403). Manual confirmation needed.",
        )

    if not response.is_success:
        logger.warning(
            "Bakong API HTTP %s for md5=%s: %s",
            response.status_code, md5, response.text[:200],
        )
        return KHQRStatusResult(
            status=KHQRPaymentStatus.ERROR,
            response_code=response.status_code,
        )

    try:
        data = response.json()
    except ValueError:
        logger.error("Bakong returned non-JSON: %s", response.text[:200])
        return KHQRStatusResult(status=KHQRPaymentStatus.ERROR)

    response_code: int = data.get("responseCode", -1)
    response_message: str = data.get("responseMessage", "")
    transaction: dict | None = data.get("data")

    if response_code == 0 and isinstance(transaction, dict):
        return KHQRStatusResult(
            status=KHQRPaymentStatus.COMPLETED,
            response_code=response_code,
            response_message=response_message,
            from_account_id=transaction.get("fromAccountId"),
            to_account_id=transaction.get("toAccountId"),
            amount=transaction.get("amount"),
            currency=transaction.get("currency"),
            description=transaction.get("description"),
            created_date_ms=transaction.get("createdDateMs"),
            raw=data,
        )

    # responseCode 1 = "transaction not found" = still pending
    logger.debug(
        "Bakong: md5=%s code=%s msg=%s", md5, response_code, response_message
    )
    return KHQRStatusResult(
        status=KHQRPaymentStatus.PENDING,
        response_code=response_code,
        response_message=response_message,
        raw=data,
    )
