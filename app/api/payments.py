from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from typing import List
from app.core.database import get_db
from app.core.auth import get_current_user, is_admin, require_roles
from app.core.config import settings
from app.core.notify import (
    build_booking_email,
    get_booking_email_context,
    send_email_notification,
)
from app.models.schemas import PaymentCreate, PaymentResponse, PaymentIntent
from app.services.khqr import KHQRService, KHQRPaymentStatus
import uuid
import hashlib
import time
import hmac
import json
import httpx
import base64
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape

router = APIRouter()
logger = logging.getLogger(__name__)

PAYWAY_STATUS_MAP = {
    "success": "completed",
    "successful": "completed",
    "completed": "completed",
    "paid": "completed",
    "approved": "completed",
    "ok": "completed",
    "00": "completed",
    "0": "completed",
    "failed": "failed",
    "fail": "failed",
    "error": "failed",
    "declined": "failed",
    "cancelled": "failed",
    "canceled": "failed",
    "refunded": "refunded",
}

STRIPE_STATUS_MAP = {
    "paid": "completed",
    "no_payment_required": "completed",
    "expired": "failed",
}


def _map_provider_status(raw_status: str | None) -> str:
    if not raw_status:
        return "pending"

    normalized = raw_status.strip().lower()
    if normalized in PAYWAY_STATUS_MAP:
        return PAYWAY_STATUS_MAP[normalized]

    if normalized in {
        "pending",
        "processing",
        "in_progress",
        "in progress",
        "created",
        "initiated",
        "waiting",
        "awaiting",
    }:
        return "pending"

    return "failed"


def _split_name(full_name: str | None) -> tuple[str, str]:
    parts = (full_name or "").strip().split(None, 1)
    if not parts:
        return "Customer", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _encode_base64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("utf-8")


def _encode_base64_json(value: object) -> str:
    return _encode_base64(json.dumps(value, separators=(",", ":"), ensure_ascii=False))


def _encode_base64_json_ascii(value: object) -> str:
    return _encode_base64(json.dumps(value, separators=(",", ":"), ensure_ascii=True))


def _build_payway_hash(values: list[object], key: str) -> str:
    hash_input = "".join("" if value is None else str(value) for value in values)
    digest = hmac.new(
        key.encode("utf-8"),
        hash_input.encode("utf-8"),
        hashlib.sha512,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _extract_payway_redirect_url(payload: dict) -> str | None:
    for key in ("checkout_url", "payment_url", "redirect_url", "url", "checkoutUrl"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value

    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("checkout_url", "payment_url", "redirect_url", "url"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def _extract_provider_reference(payload: dict) -> str | None:
    for key in ("transaction_id", "transactionId", "payment_id", "provider_reference", "reference"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value

    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("transaction_id", "transactionId", "payment_id", "provider_reference", "reference"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def _verify_signature(raw_body: bytes, provided_signature: str | None) -> bool:
    secret = settings.ABA_PAYWAY_WEBHOOK_SECRET
    if not secret:
        # Allow unsigned webhooks only in local debug mode.
        return bool(settings.DEBUG)

    if not provided_signature:
        return False

    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    candidate = provided_signature.strip()

    # Support values like "sha256=<hash>".
    if "=" in candidate:
        _, candidate = candidate.split("=", 1)

    return hmac.compare_digest(expected, candidate)


def _verify_stripe_signature(raw_body: bytes, provided_signature: str | None) -> bool:
    secret = settings.STRIPE_WEBHOOK_SECRET
    if not secret:
        return bool(settings.DEBUG)

    if not provided_signature:
        return False

    timestamp: str | None = None
    signatures: list[str] = []
    for part in provided_signature.split(","):
        key, _, value = part.partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1" and value:
            signatures.append(value)

    if not timestamp or not signatures:
        return False

    try:
        signed_at = int(timestamp)
    except ValueError:
        return False

    if abs(int(time.time()) - signed_at) > 300:
        return False

    try:
        payload = f"{timestamp}.{raw_body.decode('utf-8')}"
    except UnicodeDecodeError:
        return False

    expected = hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return any(hmac.compare_digest(expected, candidate) for candidate in signatures)


def _to_decimal(value: object | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _send_booking_emails(db: Session, booking_id: str, notification_type: str) -> None:
    context = get_booking_email_context(db, booking_id)
    if not context:
        return

    for role, key in (("customer", "customer_email"), ("staff", "staff_email")):
        recipient = context.get(key)
        if not recipient:
            continue
        email_payload = build_booking_email(context, notification_type, role)
        send_email_notification(
            db=db,
            booking_id=booking_id,
            notification_type=notification_type,
            recipient=recipient,
            subject=email_payload["subject"],
            body=email_payload["body"],
        )


def _to_stripe_minor_units(amount: Decimal) -> int:
    return int((amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _map_stripe_status(session_payload: dict) -> str:
    payment_status = str(session_payload.get("payment_status") or "").strip().lower()
    session_status = str(session_payload.get("status") or "").strip().lower()

    if payment_status in STRIPE_STATUS_MAP:
        return STRIPE_STATUS_MAP[payment_status]
    if session_status in STRIPE_STATUS_MAP:
        return STRIPE_STATUS_MAP[session_status]
    return "pending"


def _load_payment_record(db: Session, payment_id: str):
    return db.execute(
        text("SELECT * FROM payments WHERE id = :id"),
        {"id": payment_id},
    ).mappings().first()


def _resolve_payment_record(
    db: Session,
    *,
    payment_id: str | None = None,
    provider_reference: str | None = None,
):
    if payment_id:
        payment = db.execute(
            text(
                "SELECT * FROM payments WHERE id = :id ORDER BY created_at DESC LIMIT 1"
            ),
            {"id": payment_id},
        ).mappings().first()
        if payment:
            return payment

    if provider_reference:
        return db.execute(
            text(
                """
                SELECT * FROM payments
                WHERE provider_reference = :provider_reference
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"provider_reference": provider_reference},
        ).mappings().first()

    return None


def _coerce_metadata(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _is_missing_payments_metadata_column_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "payments" in msg
        and "metadata" in msg
        and ("undefinedcolumn" in msg or "does not exist" in msg)
    )


def _insert_payment_row(
    db: Session,
    *,
    payment_id: str,
    booking_id: str,
    provider: str,
    provider_reference: str | None,
    amount,
    currency: str,
    metadata: dict,
) -> None:
    params = {
        "id": payment_id,
        "booking_id": booking_id,
        "provider": provider,
        "provider_reference": provider_reference,
        "amount": amount,
        "currency": currency,
        "metadata": json.dumps(metadata),
    }

    try:
        db.execute(
            text(
                """
                INSERT INTO payments (id, booking_id, provider, provider_reference,
                                      amount, currency, status, metadata)
                VALUES (:id, :booking_id, :provider, :provider_reference,
                        :amount, :currency, 'pending', CAST(:metadata AS JSONB))
                """
            ),
            params,
        )
        return
    except SQLAlchemyError as exc:
        db.rollback()
        if not _is_missing_payments_metadata_column_error(exc):
            raise

    logger.warning(
        "payments.metadata column missing; inserting payment row without metadata",
    )
    db.execute(
        text(
            """
            INSERT INTO payments (id, booking_id, provider, provider_reference,
                                  amount, currency, status)
            VALUES (:id, :booking_id, :provider, :provider_reference,
                    :amount, :currency, 'pending')
            """
        ),
        {
            "id": payment_id,
            "booking_id": booking_id,
            "provider": provider,
            "provider_reference": provider_reference,
            "amount": amount,
            "currency": currency,
        },
    )


def _serialize_payment_response(payment: dict) -> dict:
    serialized = dict(payment)
    # FastAPI response_model expects string IDs; DB driver can return UUID objects.
    for key in ("id", "booking_id", "provider_reference"):
        value = serialized.get(key)
        if value is not None:
            serialized[key] = str(value)
    return serialized


def _resolve_payway_checkout_endpoint() -> str:
    api_url = settings.ABA_PAYWAY_API_URL.strip()
    checkout_path = settings.ABA_PAYWAY_CHECKOUT_PATH.strip()

    if not api_url:
        raise HTTPException(status_code=500, detail="ABA PayWay API URL is not configured")

    normalized_api_url = api_url.rstrip("/")
    normalized_checkout_path = checkout_path.strip("/")

    if normalized_checkout_path and normalized_api_url.endswith(f"/{normalized_checkout_path}"):
        return normalized_api_url

    if normalized_checkout_path:
        return f"{normalized_api_url}/{normalized_checkout_path}"

    return normalized_api_url


def _resolve_payway_qr_callback_url() -> str:
    configured = (settings.ABA_PAYWAY_CALLBACK_URL or "").strip()
    if configured:
        if not configured.lower().startswith("https://"):
            raise HTTPException(
                status_code=500,
                detail="ABA_PAYWAY_CALLBACK_URL must start with https:// for PayWay QR",
            )
        return configured

    app_url = settings.APP_URL.strip()
    if "localhost" in app_url or "127.0.0.1" in app_url:
        # Sandbox QR generation still needs a syntactically valid HTTPS callback value
        # even when local development cannot receive real webhooks.
        return "https://api.callback.com/notify"

    resolved = f"{app_url.rstrip('/')}{settings.ABA_PAYWAY_WEBHOOK_PATH}"
    if not resolved.lower().startswith("https://"):
        # Use a safe fallback for non-HTTPS app URLs. PayWay may reject QR requests
        # when callback_url is not syntactically valid HTTPS.
        return "https://api.callback.com/notify"

    return resolved


def _render_checkout_form_html(action: str, fields: dict[str, str], booking_id: str) -> str:
    inputs = "\n".join(
        f'<input type="hidden" name="{escape(name, quote=True)}" value="{escape(str(value), quote=True)}" />'
        for name, value in fields.items()
    )
    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Redirecting to ABA PayWay</title>
    <style>
      body {{
        margin: 0;
        font-family: Arial, sans-serif;
        background: #0b1220;
        color: #e5e7eb;
      }}
      .wrap {{
        min-height: 100vh;
        display: grid;
        place-items: center;
        padding: 24px;
      }}
      .card {{
        width: min(480px, 100%);
        background: #111827;
        border: 1px solid rgba(148, 163, 184, 0.2);
        border-radius: 20px;
        padding: 28px;
        box-shadow: 0 20px 60px rgba(0, 0, 0, 0.35);
      }}
      h1 {{
        margin: 0 0 12px;
        font-size: 1.5rem;
      }}
      p {{
        margin: 0 0 16px;
        color: #cbd5e1;
        line-height: 1.5;
      }}
      .actions {{
        display: flex;
        gap: 12px;
        flex-wrap: wrap;
        margin-top: 20px;
      }}
      .button {{
        appearance: none;
        border: 0;
        border-radius: 12px;
        padding: 12px 16px;
        font-weight: 600;
        cursor: pointer;
        text-decoration: none;
      }}
      .button-primary {{
        background: #fbbf24;
        color: #111827;
      }}
      .button-secondary {{
        background: transparent;
        color: #e5e7eb;
        border: 1px solid rgba(148, 163, 184, 0.3);
      }}
    </style>
  </head>
  <body>
    <div class="wrap">
      <div class="card">
        <h1>Redirecting to ABA PayWay</h1>
        <p>
          Your payment session is ready. We are opening ABA PayWay checkout in a secure form post.
        </p>
        <p>
          If the redirect does not start automatically, continue manually.
        </p>
        <form id="payway-checkout-form" method="POST" action="{escape(action, quote=True)}">
          {inputs}
          <div class="actions">
            <button class="button button-primary" type="submit">Continue to ABA PayWay</button>
            <a class="button button-secondary" href="/payment/{escape(booking_id, quote=True)}">Back to payment</a>
          </div>
        </form>
      </div>
    </div>
    <script>
      window.addEventListener("load", function () {{
        const form = document.getElementById("payway-checkout-form");
        if (form) {{
          form.submit();
        }}
      }});
    </script>
  </body>
</html>"""


def _apply_payment_status(
    db: Session,
    *,
    payment: dict,
    normalized_status: str,
    provider_reference: str | None = None,
    metadata_patch: dict | None = None,
) -> str:
    current_status = str(payment["status"])
    if current_status == normalized_status:
        return current_status

    if current_status in ("completed", "refunded") and normalized_status == "failed":
        return current_status

    try:
        db.execute(
            text(
                """
                UPDATE payments
                SET status = :status,
                    provider_reference = COALESCE(:provider_reference, provider_reference),
                    metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:metadata_patch AS JSONB)
                WHERE id = :id
                """
            ),
            {
                "id": payment["id"],
                "status": normalized_status,
                "provider_reference": provider_reference,
                "metadata_patch": json.dumps(metadata_patch or {}),
            },
        )
    except SQLAlchemyError as exc:
        db.rollback()
        if not _is_missing_payments_metadata_column_error(exc):
            raise
        logger.warning(
            "payments.metadata column missing; updating payment without metadata patch",
        )
        db.execute(
            text(
                """
                UPDATE payments
                SET status = :status,
                    provider_reference = COALESCE(:provider_reference, provider_reference)
                WHERE id = :id
                """
            ),
            {
                "id": payment["id"],
                "status": normalized_status,
                "provider_reference": provider_reference,
            },
        )

    if normalized_status == "completed":
        db.execute(
            text(
                """
                UPDATE bookings
                SET payment_status = 'paid', status = 'confirmed'
                WHERE id = :booking_id
                """
            ),
            {"booking_id": payment["booking_id"]},
        )
    elif normalized_status == "failed":
        db.execute(
            text(
                """
                UPDATE bookings
                SET payment_status = 'failed'
                WHERE id = :booking_id
                """
            ),
            {"booking_id": payment["booking_id"]},
        )
    elif normalized_status == "refunded":
        db.execute(
            text(
                """
                UPDATE bookings
                SET payment_status = 'refunded', status = 'cancelled'
                WHERE id = :booking_id
                """
            ),
            {"booking_id": payment["booking_id"]},
        )

    return normalized_status


async def _create_payway_checkout(
    payment_id: str,
    transaction_id: str,
    payment: PaymentCreate,
    db: Session,
) -> dict:
    endpoint = _resolve_payway_checkout_endpoint()

    booking = db.execute(
        text(
            """
            SELECT b.id,
                   s.name AS service_name,
                   c.full_name AS customer_name,
                   c.email AS customer_email,
                   c.phone AS customer_phone
            FROM bookings b
            LEFT JOIN services s ON s.id = b.service_id
            LEFT JOIN customers c ON c.id = b.customer_id
            WHERE b.id = :id
            """
        ),
        {"id": payment.booking_id},
    ).mappings().first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    req_time = str(int(time.time()))
    amount_str = f"{Decimal(str(payment.amount)):.2f}"
    first_name, last_name = _split_name(booking.get("customer_name"))
    email = (booking.get("customer_email") or "").strip()
    phone = (booking.get("customer_phone") or "").strip()
    return_params = payment_id

    # This follows the sample form post provided by PayWay support.
    payload = {
        "req_time": req_time,
        "merchant_id": settings.ABA_PAYWAY_MERCHANT_ID,
        "tran_id": transaction_id,
        "amount": amount_str,
        "firstname": first_name,
        "lastname": last_name,
        "phone": phone,
        "email": email,
        "return_params": return_params,
    }
    payload["hash"] = _build_payway_hash(
        [
            req_time,
            settings.ABA_PAYWAY_MERCHANT_ID,
            transaction_id,
            amount_str,
            first_name,
            last_name,
            email,
            phone,
            return_params,
        ],
        settings.ABA_PAYWAY_API_KEY,
    )

    return {
        "payment_url": f"/api/payments/{payment_id}/checkout",
        "provider_reference": transaction_id,
        "checkout_action": endpoint,
        "checkout_fields": payload,
    }


async def _create_payway_qr(
    payment_id: str,
    transaction_id: str,
    payment: PaymentCreate,
    db: Session,
) -> dict:
    booking = db.execute(
        text(
            """
            SELECT b.id,
                   s.name AS service_name,
                   c.full_name AS customer_name,
                   c.email AS customer_email,
                   c.phone AS customer_phone
            FROM bookings b
            LEFT JOIN services s ON s.id = b.service_id
            LEFT JOIN customers c ON c.id = b.customer_id
            WHERE b.id = :id
            """
        ),
        {"id": payment.booking_id},
    ).mappings().first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    api_url = settings.ABA_PAYWAY_API_URL.rstrip("/")
    endpoint = f"{api_url}{settings.ABA_PAYWAY_QR_PATH}"
    callback_url = _resolve_payway_qr_callback_url()

    now_utc = datetime.now(timezone.utc)
    req_time = now_utc.strftime("%Y%m%d%H%M%S")
    amount_decimal = Decimal(str(payment.amount)).quantize(Decimal("0.01"))
    amount_value = float(amount_decimal)
    # Keep the QR hash input deterministic and ASCII-only. PayWay is sensitive to
    # payload differences here, and the standalone sample that succeeded used
    # fixed ASCII contact values rather than booking-derived customer fields.
    first_name = "ABA"
    last_name = "Bank"
    email = "aba.bank@gmail.com"
    phone = "012345678"
    item_name = f"Booking {str(booking.get('id') or payment.booking_id)[:12]}"
    items_value = _encode_base64_json_ascii(
        [
            {
                "name": item_name,
                "quantity": 1,
                "price": float(amount_decimal),
            }
        ]
    )
    callback_value = _encode_base64(callback_url)

    payload = {
        "req_time": req_time,
        "merchant_id": settings.ABA_PAYWAY_MERCHANT_ID,
        "tran_id": transaction_id,
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "phone": phone,
        "amount": amount_value,
        "purchase_type": "purchase",
        "payment_option": "abapay_khqr",
        "items": items_value,
        "currency": payment.currency.upper(),
        "callback_url": callback_value,
        "return_deeplink": None,
        "custom_fields": None,
        "return_params": None,
        "payout": None,
        "lifetime": settings.ABA_PAYWAY_QR_LIFETIME_MINUTES,
        "qr_image_template": settings.ABA_PAYWAY_QR_IMAGE_TEMPLATE,
    }
    amount_hash_value = format(amount_decimal, "f")
    hash_values = [
        req_time,
        settings.ABA_PAYWAY_MERCHANT_ID,
        transaction_id,
        amount_hash_value,
        items_value,
        first_name,
        last_name,
        email,
        phone,
        "purchase",
        "abapay_khqr",
        callback_value,
        "",
        payment.currency.upper(),
        "",
        "",
        "",
        settings.ABA_PAYWAY_QR_LIFETIME_MINUTES,
        settings.ABA_PAYWAY_QR_IMAGE_TEMPLATE,
    ]
    payload["hash"] = _build_payway_hash(hash_values, settings.ABA_PAYWAY_API_KEY)

    if settings.DEBUG:
        logger.warning(
            "PayWay QR request prepared",
            extra={
                "payment_id": payment_id,
                "transaction_id": transaction_id,
                "endpoint": endpoint,
                "payload_preview": {
                    "req_time": req_time,
                    "merchant_id": settings.ABA_PAYWAY_MERCHANT_ID,
                    "tran_id": transaction_id,
                    "amount": amount_hash_value,
                    "currency": payment.currency.upper(),
                    "callback_url": callback_url,
                    "item_name": item_name,
                },
                "hash_input": "".join(str(value) for value in hash_values),
            },
        )

    timeout = httpx.Timeout(settings.ABA_PAYWAY_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(endpoint, json=payload)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Payway QR request failed: {exc}") from exc

    if response.status_code < 200 or response.status_code >= 300:
        raise HTTPException(
            status_code=502,
            detail=f"Payway QR rejected request ({response.status_code}): {response.text[:300]}",
        )

    try:
        response_payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Payway QR returned non-JSON response: {response.text[:300]}",
        ) from exc

    status = response_payload.get("status") or {}
    status_code = str(status.get("code") or "").strip()
    if status_code not in {"0", "00"}:
        raise HTTPException(
            status_code=502,
            detail=f"Payway QR rejected request: {json.dumps(response_payload)[:300]}",
        )

    qr_image = response_payload.get("qrImage")
    qr_string = response_payload.get("qrString")
    has_qr_image = isinstance(qr_image, str) and bool(qr_image.strip())
    has_qr_string = isinstance(qr_string, str) and bool(qr_string.strip())
    if not has_qr_image and not has_qr_string:
        raise HTTPException(
            status_code=502,
            detail="PayWay QR response missing both qrImage and qrString",
        )
    qr_image = qr_image if has_qr_image else None
    qr_string = qr_string if has_qr_string else None

    return {
        "provider": "aba_payway",
        "payment_url": None,
        "payment_id": payment_id,
        "transaction_id": transaction_id,
        "merchant_id": settings.ABA_PAYWAY_MERCHANT_ID,
        "gateway_mode": (
            "sandbox"
            if "sandbox" in settings.ABA_PAYWAY_API_URL.lower()
            else "production"
        ),
        "settlement_destination": (
            "Funds are routed to the settlement account configured under "
            f"PayWay merchant profile '{settings.ABA_PAYWAY_MERCHANT_ID}'"
        ),
        "qr_image": qr_image,
        "qr_string": qr_string,
        "deeplink": response_payload.get("abapay_deeplink"),
        "app_store": response_payload.get("app_store"),
        "play_store": response_payload.get("play_store"),
        "payment_status": "pending",
        "expires_at": now_utc + timedelta(minutes=settings.ABA_PAYWAY_QR_LIFETIME_MINUTES),
    }


async def _fetch_payway_transaction_detail(provider_transaction_id: str) -> dict:
    api_url = settings.ABA_PAYWAY_API_URL.rstrip("/")
    endpoint = f"{api_url}{settings.ABA_PAYWAY_TRANSACTION_DETAIL_PATH}"
    req_time = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    payload = {
        "req_time": req_time,
        "merchant_id": settings.ABA_PAYWAY_MERCHANT_ID,
        "tran_id": provider_transaction_id,
    }
    payload["hash"] = _build_payway_hash(
        [payload["req_time"], payload["merchant_id"], payload["tran_id"]],
        settings.ABA_PAYWAY_API_KEY,
    )

    timeout = httpx.Timeout(settings.ABA_PAYWAY_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(endpoint, json=payload)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Payway transaction detail request failed: {exc}",
        ) from exc

    if response.status_code < 200 or response.status_code >= 300:
        raise HTTPException(
            status_code=502,
            detail=(
                "Payway transaction detail rejected request "
                f"({response.status_code}): {response.text[:300]}"
            ),
        )

    try:
        response_payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Payway transaction detail returned non-JSON response: {response.text[:300]}",
        ) from exc

    status = response_payload.get("status") or {}
    status_code = str(status.get("code") or "").strip()
    if status_code not in {"0", "00"}:
        _NOT_FOUND_PHRASES = ("transaction not found", "no transaction", "not found")
        status_message = str(status.get("message") or "").strip().lower()
        if any(phrase in status_message for phrase in _NOT_FOUND_PHRASES):
            return {"_not_found": True}
        raise HTTPException(
            status_code=502,
            detail=f"Payway transaction detail failed: {json.dumps(response_payload)[:300]}",
        )

    return response_payload


async def _create_stripe_checkout(
    payment_id: str,
    payment: PaymentCreate,
    db: Session,
) -> tuple[str, str]:
    if not settings.STRIPE_API_KEY:
        raise HTTPException(status_code=503, detail="Stripe is not configured")

    booking = db.execute(
        text(
            """
            SELECT b.id, s.name AS service_name
            FROM bookings b
            LEFT JOIN services s ON s.id = b.service_id
            WHERE b.id = :id
            """
        ),
        {"id": payment.booking_id},
    ).mappings().first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    amount_decimal = Decimal(str(payment.amount))
    if amount_decimal <= 0:
        raise HTTPException(status_code=400, detail="Stripe amount must be greater than zero")

    endpoint = f"{settings.STRIPE_API_URL.rstrip('/')}/checkout/sessions"
    success_url = settings.STRIPE_RETURN_URL or (
        f"{settings.APP_URL.rstrip('/')}/payment/{payment.booking_id}"
        f"?payment_id={payment_id}&stripe_session_id={{CHECKOUT_SESSION_ID}}"
    )
    cancel_url = settings.STRIPE_CANCEL_URL or (
        f"{settings.APP_URL.rstrip('/')}/payment/{payment.booking_id}"
    )

    data = {
        "mode": "payment",
        "payment_method_types[0]": "card",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": payment_id,
        "metadata[payment_id]": payment_id,
        "metadata[booking_id]": payment.booking_id,
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": payment.currency.lower(),
        "line_items[0][price_data][unit_amount]": str(
            _to_stripe_minor_units(amount_decimal)
        ),
        "line_items[0][price_data][product_data][name]": (
            booking.get("service_name") or "Booking payment"
        ),
    }

    _stripe_headers = {
        "Authorization": f"Bearer {settings.STRIPE_API_KEY}",
        "Stripe-Version": "2026-02-25.clover",
    }
    timeout = httpx.Timeout(settings.STRIPE_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                endpoint,
                data=data,
                headers=_stripe_headers,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Stripe request failed: {exc}") from exc

    if response.status_code < 200 or response.status_code >= 300:
        raise HTTPException(
            status_code=502,
            detail=f"Stripe rejected request ({response.status_code}): {response.text[:300]}",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Stripe returned non-JSON response: {response.text[:300]}",
        ) from exc

    payment_url = payload.get("url")
    session_id = payload.get("id")
    if not isinstance(payment_url, str) or not payment_url.strip():
        raise HTTPException(status_code=502, detail="Stripe response missing checkout URL")
    if not isinstance(session_id, str) or not session_id.strip():
        raise HTTPException(status_code=502, detail="Stripe response missing session ID")

    return payment_url, session_id


async def _fetch_stripe_session(session_id: str) -> dict:
    if not settings.STRIPE_API_KEY:
        raise HTTPException(status_code=503, detail="Stripe is not configured")

    endpoint = f"{settings.STRIPE_API_URL.rstrip('/')}/checkout/sessions/{session_id}"
    timeout = httpx.Timeout(settings.STRIPE_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                endpoint,
                params={"expand[]": "payment_intent"},
                headers={
                    "Authorization": f"Bearer {settings.STRIPE_API_KEY}",
                    "Stripe-Version": "2026-02-25.clover",
                },
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Stripe lookup failed: {exc}") from exc

    if response.status_code < 200 or response.status_code >= 300:
        raise HTTPException(
            status_code=502,
            detail=f"Stripe session lookup failed ({response.status_code}): {response.text[:300]}",
        )

    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Stripe returned non-JSON session data: {response.text[:300]}",
        ) from exc


async def _sync_stripe_payment_status(
    db: Session,
    *,
    payment: dict,
    stripe_session_id: str | None,
) -> dict:
    session_id = (stripe_session_id or payment.get("provider_reference") or "").strip()
    if not session_id:
        return payment
    previous_status = str(payment["status"])

    if payment.get("provider_reference") and payment["provider_reference"] != session_id:
        raise HTTPException(status_code=400, detail="Stripe session mismatch")

    session = await _fetch_stripe_session(session_id)
    normalized_status = _map_stripe_status(session)

    amount_total = session.get("amount_total")
    if amount_total is not None:
        expected = _to_stripe_minor_units(Decimal(str(payment["amount"])))
        if int(amount_total) != expected:
            raise HTTPException(status_code=400, detail="Stripe amount mismatch")

    payload_currency = session.get("currency")
    if isinstance(payload_currency, str) and payload_currency.strip():
        if payload_currency.strip().upper() != str(payment["currency"]).upper():
            raise HTTPException(status_code=400, detail="Stripe currency mismatch")

    _apply_payment_status(
        db,
        payment=payment,
        normalized_status=normalized_status,
        provider_reference=session.get("id"),
        metadata_patch={
            "stripe_session_status": session.get("status"),
            "stripe_payment_status": session.get("payment_status"),
            "stripe_payment_intent": session.get("payment_intent"),
            "stripe_checked_at": int(time.time()),
        },
    )
    db.commit()
    updated_payment = _load_payment_record(db, payment["id"])
    if previous_status != "completed" and updated_payment and updated_payment["status"] == "completed":
        _send_booking_emails(db, updated_payment["booking_id"], "confirmation")
    return updated_payment


async def _sync_payway_payment_status(db: Session, *, payment: dict) -> dict:
    provider_transaction_id = str(payment.get("provider_reference") or "").strip()
    if not provider_transaction_id:
        return payment

    # -----------------------------------------------------------------------
    # Grace window: skip PayWay status lookup for newly created payments.
    # PayWay sandbox is not reliably queryable immediately after QR creation,
    # so we avoid hammering the API and producing "transaction not found" noise.
    # -----------------------------------------------------------------------
    created_at = payment.get("created_at")
    if created_at is not None:
        if created_at.tzinfo is None:
            created_at_utc = created_at.replace(tzinfo=timezone.utc)
        else:
            created_at_utc = created_at.astimezone(timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - created_at_utc).total_seconds()
        if age_seconds < settings.ABA_PAYWAY_SYNC_GRACE_SECONDS:
            logger.debug(
                "PayWay sync skipped — within grace window",
                extra={
                    "payment_id": str(payment.get("id")),
                    "age_seconds": round(age_seconds, 1),
                },
            )
            return payment

    previous_status = str(payment["status"])

    try:
        detail_payload = await _fetch_payway_transaction_detail(provider_transaction_id)
    except HTTPException as exc:
        # Network/server errors: keep pending, allow frontend to retry.
        logger.warning(
            "PayWay status sync skipped — provider error",
            extra={
                "payment_id": str(payment.get("id")),
                "provider_reference": provider_transaction_id,
                "error": str(exc.detail),
            },
        )
        return payment

    # -----------------------------------------------------------------------
    # Sentinel: PayWay returned "transaction not found" — not a hard failure.
    # After the grace window this is logged as a warning so it stays visible.
    # -----------------------------------------------------------------------
    if detail_payload.get("_not_found"):
        logger.warning(
            "PayWay transaction not found after grace window — staying pending",
            extra={
                "payment_id": str(payment.get("id")),
                "provider_reference": provider_transaction_id,
            },
        )
        return payment

    data = detail_payload.get("data") if isinstance(detail_payload.get("data"), dict) else {}
    operations = data.get("transaction_operations") if isinstance(data.get("transaction_operations"), list) else []
    latest_operation = operations[-1] if operations and isinstance(operations[-1], dict) else {}

    raw_status = (
        latest_operation.get("status")
        or data.get("payment_status")
        or data.get("status")
    )
    normalized_status = _map_provider_status(raw_status)

    payload_amount = _to_decimal(
        data.get("total_amount")
        or data.get("payment_amount")
        or data.get("original_amount")
    )
    if payload_amount is not None:
        expected_amount = _to_decimal(payment.get("amount"))
        if expected_amount is not None and payload_amount != expected_amount:
            raise HTTPException(status_code=400, detail="PayWay amount mismatch")

    payload_currency = (
        data.get("payment_currency")
        or data.get("original_currency")
        or data.get("currency")
    )
    if isinstance(payload_currency, str) and payload_currency.strip():
        if payload_currency.strip().upper() != str(payment["currency"]).upper():
            raise HTTPException(status_code=400, detail="PayWay currency mismatch")

    _apply_payment_status(
        db,
        payment=payment,
        normalized_status=normalized_status,
        provider_reference=provider_transaction_id,
        metadata_patch={
            "payway_checked_at": int(time.time()),
            "payway_provider_status": raw_status,
        },
    )
    db.commit()
    updated_payment = _load_payment_record(db, payment["id"])
    if previous_status != "completed" and updated_payment and updated_payment["status"] == "completed":
        _send_booking_emails(db, updated_payment["booking_id"], "confirmation")
    return updated_payment

async def _sync_khqr_payment_status(db: Session, *, payment: dict) -> dict:
    """
    Poll Bakong for the KHQR transaction status and persist any change.
    Mirrors the PayWay/Stripe sync pattern — never raises, network errors
    return the payment unchanged so the frontend can retry.
    """
    md5 = str(payment.get("provider_reference") or "").strip()
    if not md5:
        return payment

    # Skip if already flagged as geo-blocked — Bakong API won't answer from this server
    metadata = _coerce_metadata(payment.get("metadata"))
    if metadata.get("khqr_geo_blocked"):
        return payment

    # Skip if the QR session TTL has expired and payment is still pending
    created_at = payment.get("created_at")
    if created_at is not None:
        if created_at.tzinfo is None:
            created_at_utc = created_at.replace(tzinfo=timezone.utc)
        else:
            created_at_utc = created_at.astimezone(timezone.utc)
        age_minutes = (datetime.now(timezone.utc) - created_at_utc).total_seconds() / 60
        ttl = settings.KHQR_QR_LIFETIME_MINUTES
        if age_minutes > ttl:
            logger.info(
                "KHQR QR expired — marking payment failed: payment_id=%s",
                str(payment.get("id")),
            )
            _apply_payment_status(
                db,
                payment=payment,
                normalized_status="failed",
                metadata_patch={"khqr_expired_at": int(time.time())},
            )
            db.commit()
            return _load_payment_record(db, payment["id"])

    previous_status = str(payment["status"])

    svc = KHQRService(settings)
    try:
        result = await svc.check_status(md5)
    except Exception as exc:
        logger.warning("KHQR status check failed unexpectedly: %s", exc)
        return payment

    if result.status == KHQRPaymentStatus.GEO_BLOCKED:
        # Bakong blocks our server IP — stop polling, flag for manual confirmation
        _apply_payment_status(
            db,
            payment=payment,
            normalized_status="pending",
            metadata_patch={
                "khqr_geo_blocked": True,
                "khqr_geo_blocked_at": int(time.time()),
                "khqr_manual_confirmation_required": True,
            },
        )
        db.commit()
        return _load_payment_record(db, payment["id"])

    if result.status == KHQRPaymentStatus.ERROR:
        # Network / auth error — don't change DB, let frontend retry
        return payment

    if result.status == KHQRPaymentStatus.PENDING:
        return payment

    # COMPLETED
    _apply_payment_status(
        db,
        payment=payment,
        normalized_status="completed",
        provider_reference=md5,
        metadata_patch={
            "khqr_checked_at": int(time.time()),
            "khqr_from_account": result.from_account_id,
            "khqr_to_account": result.to_account_id,
            "khqr_confirmed_amount": result.amount,
            "khqr_confirmed_currency": result.currency,
            "khqr_description": result.description,
            "khqr_created_date_ms": result.created_date_ms,
        },
    )
    db.commit()
    updated_payment = _load_payment_record(db, payment["id"])
    if previous_status != "completed" and updated_payment and updated_payment["status"] == "completed":
        _send_booking_emails(db, updated_payment["booking_id"], "confirmation")
    return updated_payment


def _get_customer_id(db: Session, user_id: str) -> str | None:
    record = db.execute(
        "SELECT id FROM customers WHERE user_id = :user_id",
        {"user_id": user_id},
    ).fetchone()
    return record[0] if record else None

def _ensure_booking_access(db: Session, booking_id: str, current_user: dict) -> None:
    record = db.execute(
        "SELECT staff_id, customer_id FROM bookings WHERE id = :id",
        {"id": booking_id},
    ).fetchone()

    if not record:
        raise HTTPException(status_code=404, detail="Booking not found")

    if is_admin(current_user):
        return

    role = current_user.get("role")
    if role == "staff":
        if record[0] != current_user.get("id"):
            raise HTTPException(status_code=403, detail="Forbidden")
        return

    if role == "customer":
        customer_id = _get_customer_id(db, current_user.get("id"))
        if not customer_id or record[1] != customer_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        return

    raise HTTPException(status_code=403, detail="Forbidden")

@router.post("/create-intent", response_model=PaymentIntent)
async def create_payment_intent(
    payment: PaymentCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create a payment intent with the selected provider."""
    _ensure_booking_access(db, payment.booking_id, current_user)
    payment_id = str(uuid.uuid4())
    provider = (payment.provider or "aba_payway").strip().lower()

    if provider == "aba_payway":
        transaction_id = f"{int(time.time())}{uuid.uuid4().int % 1000000:06d}"
        payway_intent = await _create_payway_qr(
            payment_id=payment_id,
            transaction_id=transaction_id,
            payment=payment,
            db=db,
        )
        provider_reference = transaction_id
    elif provider == "stripe":
        transaction_id = f"st{uuid.uuid4().hex[:18]}"
        payment_url, provider_reference = await _create_stripe_checkout(
            payment_id=payment_id,
            payment=payment,
            db=db,
        )
    elif provider == "bakong_khqr":
        if not settings.KHQR_JWT_TOKEN:
            raise HTTPException(status_code=503, detail="Bakong KHQR is not configured")
        bill_number = f"BKG-{payment.booking_id[:12].upper()}"
        svc = KHQRService(settings)
        khqr_record = svc.create_payment(
            bill_number=bill_number,
            amount=float(payment.amount),
            currency=payment.currency,
        )
        transaction_id = khqr_record.md5
        provider_reference = khqr_record.md5
    else:
        raise HTTPException(status_code=400, detail="Unsupported payment provider")

    metadata = {
        "provider": provider,
        "checkout_created_at": int(time.time()),
    }
    if provider == "aba_payway":
        metadata["sandbox"] = "sandbox" in settings.ABA_PAYWAY_API_URL.lower()
        metadata["qr_mode"] = True
    if provider == "stripe":
        metadata["mode"] = (
            "test"
            if settings.STRIPE_API_KEY and settings.STRIPE_API_KEY.startswith("sk_test_")
            else "live"
        )
        metadata["stripe_session_id"] = provider_reference
    if provider == "bakong_khqr":
        metadata["khqr_payload"] = khqr_record.payload
        metadata["khqr_md5"] = khqr_record.md5
        metadata["khqr_bill_number"] = khqr_record.bill_number

    _insert_payment_row(
        db,
        payment_id=payment_id,
        booking_id=payment.booking_id,
        provider=provider,
        provider_reference=provider_reference,
        amount=payment.amount,
        currency=payment.currency,
        metadata=metadata,
    )
    db.commit()

    if provider == "aba_payway":
        return payway_intent

    if provider == "bakong_khqr":
        return {
            "provider": provider,
            "payment_id": payment_id,
            "transaction_id": transaction_id,
            "qr_image": khqr_record.qr_image_b64,
            "qr_string": khqr_record.payload,
        }

    return {
        "provider": provider,
        "payment_url": payment_url,
        "payment_id": payment_id,
        "transaction_id": transaction_id,
    }


@router.get("/{payment_id}/checkout", response_class=HTMLResponse)
async def render_payway_checkout(
    payment_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    payment = _load_payment_record(db, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    if payment["provider"] != "aba_payway":
        raise HTTPException(status_code=400, detail="Checkout provider mismatch")

    _ensure_booking_access(db, payment["booking_id"], current_user)
    metadata = _coerce_metadata(payment.get("metadata"))
    action = metadata.get("payway_checkout_action")
    fields = metadata.get("payway_checkout_fields")

    if not isinstance(action, str) or not action.strip():
        raise HTTPException(status_code=409, detail="Missing PayWay checkout action")
    if not isinstance(fields, dict) or not fields:
        raise HTTPException(status_code=409, detail="Missing PayWay checkout form fields")

    normalized_fields = {
        str(key): "" if value is None else str(value)
        for key, value in fields.items()
    }
    return HTMLResponse(
        content=_render_checkout_form_html(
            action=action,
            fields=normalized_fields,
            booking_id=str(payment["booking_id"]),
        ),
        headers={"Cache-Control": "no-store"},
    )

@router.post("/{payment_id}/confirm")
async def confirm_payment(
    payment_id: str,
    transaction_status: str = "success",
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Admin/customer-confirmation fallback for manual testing."""
    if not settings.DEBUG and not is_admin(current_user):
        raise HTTPException(status_code=403, detail="Manual confirmation is disabled")

    payment = _load_payment_record(db, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    _ensure_booking_access(db, payment["booking_id"], current_user)
    status = "completed" if transaction_status == "success" else "failed"
    previous_status = str(payment["status"])

    _apply_payment_status(
        db,
        payment=payment,
        normalized_status=status,
        metadata_patch={
            "manual_confirmation_at": int(time.time()),
            "manual_confirmation_by": current_user.get("id"),
        },
    )
    db.commit()
    if previous_status != "completed" and status == "completed":
        _send_booking_emails(db, payment["booking_id"], "confirmation")

    return {"message": "Payment status updated", "status": status}


@router.post("/webhook/payway")
async def payway_webhook(
    request: Request,
    x_signature: str | None = Header(default=None, alias="X-Signature"),
    x_payway_signature: str | None = Header(default=None, alias="X-Payway-Signature"),
    db: Session = Depends(get_db),
):
    """Process Payway payment webhook and update payment idempotently."""
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    signature = x_signature or x_payway_signature
    is_qr_callback = bool(payload.get("merchant_ref_no") and payload.get("tran_id"))
    if signature:
        if not _verify_signature(raw_body, signature):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
    elif settings.ABA_PAYWAY_WEBHOOK_SECRET and not settings.DEBUG and not is_qr_callback:
        raise HTTPException(status_code=401, detail="Missing webhook signature")

    payment_id = payload.get("order_id") or payload.get("payment_id")
    merchant_ref_no = payload.get("merchant_ref_no")
    provider_transaction_id = payload.get("tran_id") or payload.get("transaction_id")
    provider_reference = (
        merchant_ref_no
        or payload.get("transaction_id")
        or payload.get("provider_reference")
        or payload.get("reference")
    )

    if not payment_id and not provider_reference:
        raise HTTPException(status_code=400, detail="Missing payment reference")

    payment = _resolve_payment_record(
        db,
        payment_id=payment_id,
        provider_reference=provider_reference,
    )
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")

    if payment["provider"] != "aba_payway":
        raise HTTPException(status_code=400, detail="Webhook provider mismatch")

    verified_payload = payload
    provider_status = payload.get("status") or payload.get("transaction_status")
    if is_qr_callback and provider_transaction_id:
        verified_payload = await _fetch_payway_transaction_detail(provider_transaction_id)
        detail_data = verified_payload.get("data") or {}
        provider_status = (
            detail_data.get("payment_status")
            or (detail_data.get("transaction_operations") or [{}])[-1].get("status")
            or provider_status
        )
        payload_amount = _to_decimal(
            detail_data.get("total_amount")
            or detail_data.get("payment_amount")
            or detail_data.get("original_amount")
        )
        payload_currency = (
            detail_data.get("payment_currency")
            or detail_data.get("original_currency")
            or payload.get("currency")
        )
    else:
        payload_amount = _to_decimal(payload.get("amount") or payload.get("total_amount"))
        payload_currency = payload.get("currency")

    if payload_amount is not None:
        expected_amount = _to_decimal(payment["amount"])
        if expected_amount is not None and payload_amount != expected_amount:
            raise HTTPException(status_code=400, detail="Webhook amount mismatch")

    if isinstance(payload_currency, str) and payload_currency.strip():
        if payload_currency.strip().upper() != str(payment["currency"]).upper():
            raise HTTPException(status_code=400, detail="Webhook currency mismatch")

    previous_status = str(payment["status"])
    normalized_status = _map_provider_status(provider_status)
    final_status = _apply_payment_status(
        db,
        payment=payment,
        normalized_status=normalized_status,
        provider_reference=provider_reference,
        metadata_patch={
            "webhook_received_at": int(time.time()),
            "provider_status": provider_status,
            "provider_reference": provider_reference,
            "provider_transaction_id": provider_transaction_id,
            "merchant_ref_no": merchant_ref_no,
            "verified_transaction": verified_payload.get("data") if isinstance(verified_payload, dict) else None,
        },
    )
    db.commit()
    if previous_status != "completed" and final_status == "completed":
        _send_booking_emails(db, payment["booking_id"], "confirmation")
    return {"message": "Webhook processed", "status": final_status}


@router.post("/webhook/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    db: Session = Depends(get_db),
):
    raw_body = await request.body()
    if not _verify_stripe_signature(raw_body, stripe_signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    event_type = payload.get("type")
    data = payload.get("data") or {}
    session = data.get("object") or {}

    session_id = session.get("id")
    metadata = session.get("metadata") or {}
    payment_id = session.get("client_reference_id") or metadata.get("payment_id")
    payment = _resolve_payment_record(
        db,
        payment_id=payment_id,
        provider_reference=session_id,
    )
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    if payment["provider"] != "stripe":
        raise HTTPException(status_code=400, detail="Webhook provider mismatch")

    amount_total = session.get("amount_total")
    if amount_total is not None:
        expected = _to_stripe_minor_units(Decimal(str(payment["amount"])))
        if int(amount_total) != expected:
            raise HTTPException(status_code=400, detail="Stripe amount mismatch")

    payload_currency = session.get("currency")
    if isinstance(payload_currency, str) and payload_currency.strip():
        if payload_currency.strip().upper() != str(payment["currency"]).upper():
            raise HTTPException(status_code=400, detail="Stripe currency mismatch")

    previous_status = str(payment["status"])
    normalized_status = _map_stripe_status(session)
    if event_type in {"checkout.session.expired", "checkout.session.async_payment_failed"}:
        normalized_status = "failed"

    final_status = _apply_payment_status(
        db,
        payment=payment,
        normalized_status=normalized_status,
        provider_reference=session_id,
        metadata_patch={
            "stripe_event_type": event_type,
            "stripe_session_status": session.get("status"),
            "stripe_payment_status": session.get("payment_status"),
            "stripe_payment_intent": session.get("payment_intent"),
            "stripe_webhook_received_at": int(time.time()),
        },
    )
    db.commit()
    if previous_status != "completed" and final_status == "completed":
        _send_booking_emails(db, payment["booking_id"], "confirmation")
    return {"message": "Stripe webhook processed", "status": final_status}

@router.get("/{payment_id}", response_model=PaymentResponse)
async def get_payment(
    payment_id: str,
    stripe_session_id: str | None = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get payment by ID"""
    payment = _load_payment_record(db, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")

    _ensure_booking_access(db, payment["booking_id"], current_user)
    if payment["provider"] == "stripe" and payment["status"] == "pending":
        payment = await _sync_stripe_payment_status(
            db,
            payment=payment,
            stripe_session_id=stripe_session_id,
        )
    if payment["provider"] == "aba_payway" and payment["status"] == "pending":
        payment = await _sync_payway_payment_status(db, payment=payment)
    if payment["provider"] == "bakong_khqr" and payment["status"] == "pending":
        payment = await _sync_khqr_payment_status(db, payment=payment)
    return _serialize_payment_response(payment)

@router.get("/booking/{booking_id}", response_model=List[PaymentResponse])
async def get_booking_payments(
    booking_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get all payments for a booking"""
    _ensure_booking_access(db, booking_id, current_user)
    result = db.execute(
        "SELECT * FROM payments WHERE booking_id = :booking_id ORDER BY created_at DESC",
        {"booking_id": booking_id}
    )
    
    payments = result.fetchall()
    return [_serialize_payment_response(dict(row._mapping)) for row in payments]

@router.post("/admin/sweep-khqr")
async def sweep_khqr_payments(
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db),
):
    """
    Admin: check all pending KHQR payments and update their status.
    Expires sessions older than KHQR_QR_LIFETIME_MINUTES and confirms
    any that Bakong reports as completed.
    """
    rows = db.execute(
        text(
            """
            SELECT * FROM payments
            WHERE provider = 'bakong_khqr' AND status = 'pending'
            ORDER BY created_at ASC
            """
        )
    ).mappings().all()

    results = {"expired": 0, "completed": 0, "still_pending": 0, "errors": 0}
    for row in rows:
        payment = dict(row)
        try:
            updated = await _sync_khqr_payment_status(db, payment=payment)
            final = str(updated.get("status", "pending"))
            if final == "failed":
                results["expired"] += 1
            elif final == "completed":
                results["completed"] += 1
            else:
                results["still_pending"] += 1
        except Exception as exc:
            logger.warning("sweep_khqr_payments error for payment %s: %s", payment.get("id"), exc)
            results["errors"] += 1

    return {"message": "KHQR sweep complete", "results": results}


@router.post("/admin/{payment_id}/confirm-khqr")
async def admin_confirm_khqr(
    payment_id: str,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db),
):
    """
    Admin: manually mark a KHQR payment as completed.
    Use when the Bakong API is geo-blocked and automatic confirmation is impossible.
    The admin should verify receipt of funds in the Bakong/ACLEDA app before confirming.
    """
    payment = _load_payment_record(db, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    if payment["provider"] != "bakong_khqr":
        raise HTTPException(status_code=400, detail="Payment is not a KHQR payment")
    if payment["status"] == "completed":
        return {"message": "Already completed", "status": "completed"}

    previous_status = str(payment["status"])
    _apply_payment_status(
        db,
        payment=payment,
        normalized_status="completed",
        metadata_patch={
            "khqr_manual_confirmed_at": int(time.time()),
            "khqr_manual_confirmed_by": str(current_user.get("id")),
        },
    )
    db.commit()
    if previous_status != "completed":
        _send_booking_emails(db, payment["booking_id"], "confirmation")
    return {"message": "KHQR payment manually confirmed", "status": "completed"}


@router.post("/{payment_id}/refund")
async def refund_payment(
    payment_id: str,
    amount: float,
    reason: str = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Process refund (Mock)"""
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="Forbidden")
    refund_id = str(uuid.uuid4())
    provider_refund_id = hashlib.md5(f"{refund_id}{time.time()}".encode()).hexdigest()
    
    db.execute(
        """
        INSERT INTO refunds (id, payment_id, amount, reason, provider_refund_id, status)
        VALUES (:id, :payment_id, :amount, :reason, :provider_refund_id, 'completed')
        """,
        {
            "id": refund_id,
            "payment_id": payment_id,
            "amount": amount,
            "reason": reason,
            "provider_refund_id": provider_refund_id,
        }
    )
    
    # Update payment status
    db.execute(
        "UPDATE payments SET status = 'refunded' WHERE id = :id",
        {"id": payment_id}
    )
    
    # Update booking
    db.execute(
        """
        UPDATE bookings SET payment_status = 'refunded', status = 'cancelled'
        WHERE id = (SELECT booking_id FROM payments WHERE id = :payment_id)
        """,
        {"payment_id": payment_id}
    )
    
    db.commit()
    
    return {
        "message": "Refund processed",
        "refund_id": refund_id,
        "provider_refund_id": provider_refund_id,
    }
