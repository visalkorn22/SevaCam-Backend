from datetime import datetime, timezone
from typing import Optional, Dict, Any
from zoneinfo import ZoneInfo
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.email import send_email


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _format_time(value: datetime, tz_name: Optional[str]) -> str:
    value = _ensure_aware(value)
    tz = timezone.utc
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def _insert_notification(
    db: Session,
    booking_id: Optional[str],
    channel: str,
    notification_type: str,
    recipient: str,
    status: str,
    sent_at: Optional[datetime],
) -> str:
    notification_id = str(uuid.uuid4())
    db.execute(
        text(
            """
            INSERT INTO notifications
                (id, booking_id, channel, type, recipient, status, sent_at)
            VALUES
                (:id, :booking_id, :channel, :type, :recipient, :status, :sent_at)
            """
        ),
        {
            "id": notification_id,
            "booking_id": booking_id,
            "channel": channel,
            "type": notification_type,
            "recipient": recipient,
            "status": status,
            "sent_at": sent_at,
        },
    )
    return notification_id


def _update_notification_status(
    db: Session,
    notification_id: str,
    status: str,
    sent_at: Optional[datetime],
) -> None:
    db.execute(
        text(
            """
            UPDATE notifications
            SET status = :status, sent_at = :sent_at
            WHERE id = :id
            """
        ),
        {"id": notification_id, "status": status, "sent_at": sent_at},
    )


def send_email_notification(
    db: Session,
    booking_id: Optional[str],
    notification_type: str,
    recipient: str,
    subject: str,
    body: str,
) -> str:
    notification_id = _insert_notification(
        db=db,
        booking_id=booking_id,
        channel="email",
        notification_type=notification_type,
        recipient=recipient,
        status="pending",
        sent_at=None,
    )
    db.commit()

    try:
        send_email(recipient, subject, body)
    except Exception:
        _update_notification_status(db, notification_id, "failed", None)
        db.commit()
        return "failed"

    _update_notification_status(db, notification_id, "sent", datetime.now(timezone.utc))
    db.commit()
    return "sent"


def get_booking_email_context(db: Session, booking_id: str) -> Optional[Dict[str, Any]]:
    row = db.execute(
        text(
            """
            SELECT b.id,
                   b.start_time_utc,
                   b.end_time_utc,
                   b.customer_timezone,
                   s.name AS service_name,
                   u.full_name AS staff_name,
                   u.email AS staff_email,
                   u.timezone AS staff_timezone,
                   c.full_name AS customer_name,
                   c.email AS customer_email
            FROM bookings b
            LEFT JOIN services s ON b.service_id = s.id
            LEFT JOIN users u ON b.staff_id = u.id
            LEFT JOIN customers c ON b.customer_id = c.id
            WHERE b.id = :id
            """
        ),
        {"id": booking_id},
    ).fetchone()

    if not row:
        return None

    return dict(row._mapping)


def build_booking_email(
    context: Dict[str, Any],
    notification_type: str,
    recipient_role: str,
) -> Dict[str, str]:
    service_name = context.get("service_name") or "Service"
    start_time = context.get("start_time_utc")
    end_time = context.get("end_time_utc")

    if recipient_role == "staff":
        tz_name = context.get("staff_timezone")
        recipient_name = context.get("staff_name") or "Staff"
    else:
        tz_name = context.get("customer_timezone")
        recipient_name = context.get("customer_name") or "Customer"

    start_text = _format_time(start_time, tz_name) if start_time else "N/A"
    end_text = _format_time(end_time, tz_name) if end_time else "N/A"

    if notification_type == "confirmation":
        subject = "Booking confirmation"
        header = "Your booking is confirmed."
    elif notification_type == "cancellation":
        subject = "Booking cancellation"
        header = "Your booking has been cancelled."
    elif notification_type == "reminder":
        subject = "Booking reminder"
        header = "Reminder: your booking starts in 1 hour."
    else:
        subject = "Booking update"
        header = "There is an update to your booking."

    body = (
        f"Hello {recipient_name},\n\n"
        f"{header}\n\n"
        f"Service: {service_name}\n"
        f"Start: {start_text}\n"
        f"End: {end_text}\n\n"
        "Thank you."
    )

    return {"subject": subject, "body": body}
