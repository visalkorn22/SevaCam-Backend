from fastapi import APIRouter, Depends, HTTPException, Header, Cookie
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.auth import get_current_user, is_admin
from app.core.config import settings
from app.core.notify import send_email_notification, get_booking_email_context, build_booking_email
from app.models.schemas import NotificationCreate, NotificationResponse
import uuid
from datetime import datetime, timedelta, timezone

router = APIRouter()

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

@router.post("/", response_model=NotificationResponse)
async def send_notification(
    notification: NotificationCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Send a notification and log it to database."""
    if current_user.get("role") not in {"staff", "admin", "superadmin"}:
        raise HTTPException(status_code=403, detail="Forbidden")

    if notification.channel == "email":
        context = get_booking_email_context(db, notification.booking_id)
        if not context:
            raise HTTPException(status_code=404, detail="Booking not found")
        role = "staff" if notification.recipient == context.get("staff_email") else "customer"
        email_payload = build_booking_email(context, notification.type, role)
        send_email_notification(
            db=db,
            booking_id=notification.booking_id,
            notification_type=notification.type,
            recipient=notification.recipient,
            subject=email_payload["subject"],
            body=email_payload["body"],
        )
    else:
        notification_id = str(uuid.uuid4())
        db.execute(
            """
            INSERT INTO notifications (id, booking_id, channel, type, recipient, status, sent_at)
            VALUES (:id, :booking_id, :channel, :type, :recipient, 'sent', :sent_at)
            """,
            {
                "id": notification_id,
                "booking_id": notification.booking_id,
                "channel": notification.channel,
                "type": notification.type,
                "recipient": notification.recipient,
                "sent_at": datetime.now(timezone.utc),
            }
        )
        db.commit()

    result = db.execute(
        "SELECT * FROM notifications WHERE booking_id = :booking_id ORDER BY created_at DESC LIMIT 1",
        {"booking_id": notification.booking_id},
    )
    row = result.fetchone()
    return dict(row._mapping) if row else {}

@router.get("/booking/{booking_id}")
async def get_booking_notifications(
    booking_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get all notifications for a booking"""
    _ensure_booking_access(db, booking_id, current_user)
    result = db.execute(
        "SELECT * FROM notifications WHERE booking_id = :booking_id ORDER BY created_at DESC",
        {"booking_id": booking_id}
    )
    
    notifications = result.fetchall()
    return [dict(row._mapping) for row in notifications]


@router.get("/me")
async def get_my_notifications(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get notifications for the current user's email."""
    result = db.execute(
        "SELECT * FROM notifications WHERE recipient = :email ORDER BY created_at DESC",
        {"email": current_user.get("email")},
    )
    return [dict(row._mapping) for row in result.fetchall()]


@router.post("/run-reminders")
async def run_booking_reminders(
    cron_token: str | None = Header(None, alias="X-Reminder-Token"),
    authorization: str | None = Header(None),
    auth_token: str | None = Cookie(None),
    db: Session = Depends(get_db),
):
    """Send reminder emails for bookings starting soon."""
    if settings.REMINDER_CRON_TOKEN:
        if cron_token != settings.REMINDER_CRON_TOKEN:
            raise HTTPException(status_code=401, detail="Invalid reminder token")
    else:
        current_user = get_current_user(
            authorization=authorization,
            auth_token=auth_token,
            db=db,
        )
        if not is_admin(current_user):
            raise HTTPException(status_code=403, detail="Forbidden")

    lead = settings.REMINDER_LEAD_MINUTES
    window = settings.REMINDER_WINDOW_MINUTES
    now = datetime.now(timezone.utc)
    window_start = now + timedelta(minutes=lead)
    window_end = window_start + timedelta(minutes=window)

    rows = db.execute(
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
        WHERE b.status IN ('pending', 'confirmed')
          AND b.start_time_utc >= :start
          AND b.start_time_utc < :end
        """,
        {"start": window_start, "end": window_end},
    ).fetchall()

    sent = 0
    skipped = 0

    for row in rows:
        context = dict(row._mapping)
        booking_id = str(context["id"])

        for role, email_key in (("customer", "customer_email"), ("staff", "staff_email")):
            recipient = context.get(email_key)
            if not recipient:
                skipped += 1
                continue

            exists = db.execute(
                """
                SELECT 1 FROM notifications
                WHERE booking_id = :booking_id
                  AND type = 'reminder'
                  AND recipient = :recipient
                """,
                {"booking_id": booking_id, "recipient": recipient},
            ).fetchone()
            if exists:
                skipped += 1
                continue

            email_payload = build_booking_email(context, "reminder", role)
            status = send_email_notification(
                db=db,
                booking_id=booking_id,
                notification_type="reminder",
                recipient=recipient,
                subject=email_payload["subject"],
                body=email_payload["body"],
            )
            if status == "sent":
                sent += 1

    return {"sent": sent, "skipped": skipped}
