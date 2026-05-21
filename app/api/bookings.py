from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timedelta, time, date, timezone as dt_timezone
import calendar
import json
import logging
from decimal import Decimal
from zoneinfo import ZoneInfo
from app.core.database import get_db
from app.core.auth import get_current_user, is_admin
from app.core.config import settings
from app.core.notify import send_email_notification, get_booking_email_context, build_booking_email
from app.api.availability import _compute_slots_for_date
from app.models.schemas import (
    BookingCreate, BookingUpdate, BookingResponse, BookingWithDetails,
    BookingLogResponse, BookingChangeResponse
)
import uuid

router = APIRouter()
logger = logging.getLogger(__name__)

ALLOWED_BOOKING_SOURCES = {"web", "social", "admin", "api"}
DEFAULT_APP_TIMEZONE = "Asia/Phnom_Penh"

def _normalize_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt_timezone.utc)
    return value.astimezone(dt_timezone.utc)

def _resolve_customer_timezone(
    booking_timezone: Optional[str] = None,
    user_timezone: Optional[str] = None,
) -> str:
    for candidate in (user_timezone, booking_timezone):
        if candidate and candidate != "UTC":
            return candidate
    return DEFAULT_APP_TIMEZONE

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

def _get_staff_schedule(db: Session, staff_id: str, target_date: date):
    row = db.execute(
        """
        SELECT id, timezone, max_slots_per_day, max_bookings_per_day, max_bookings_per_customer
        FROM staff_weekly_schedules
        WHERE staff_id = :staff_id
          AND (effective_from IS NULL OR effective_from <= :date)
          AND (effective_to IS NULL OR effective_to >= :date)
        ORDER BY is_default DESC, effective_from DESC NULLS LAST
        LIMIT 1
        """,
        {
            "staff_id": staff_id,
            "date": target_date,
        },
    ).fetchone()
    return dict(row._mapping) if row else None

def _enforce_daily_booking_limit(
    db: Session,
    staff_id: str,
    schedule: dict,
    local_date: date,
    schedule_tz: ZoneInfo,
    exclude_booking_id: Optional[str] = None,
) -> None:
    limit = schedule.get("max_bookings_per_day")
    if limit is None:
        return
    limit = int(limit)
    if limit <= 0:
        raise HTTPException(status_code=400, detail="Staff daily booking limit reached")

    day_start_local = datetime.combine(local_date, time(0, 0), tzinfo=schedule_tz)
    day_end_local = day_start_local + timedelta(days=1)
    day_start_utc = day_start_local.astimezone(dt_timezone.utc)
    day_end_utc = day_end_local.astimezone(dt_timezone.utc)

    query = """
        SELECT COUNT(*)
        FROM bookings
        WHERE staff_id = :staff_id
          AND start_time_utc < :day_end
          AND end_time_utc > :day_start
          AND status NOT IN ('cancelled', 'no-show')
    """
    params: Dict[str, object] = {
        "staff_id": staff_id,
        "day_start": day_start_utc,
        "day_end": day_end_utc,
    }
    if exclude_booking_id:
        query += " AND id <> :exclude_booking_id"
        params["exclude_booking_id"] = exclude_booking_id

    count = db.execute(query, params).scalar()
    if count is not None and int(count) >= limit:
        raise HTTPException(status_code=400, detail="Staff daily booking limit reached")

def _enforce_per_customer_limit(
    db: Session,
    staff_id: str,
    customer_id: str,
    schedule: dict,
    exclude_booking_id: Optional[str] = None,
) -> None:
    limit = schedule.get("max_bookings_per_customer")
    if limit is None:
        return
    limit = int(limit)
    if limit <= 0:
        raise HTTPException(
            status_code=400,
            detail="Customer booking limit reached for this staff member",
        )

    query = """
        SELECT COUNT(*)
        FROM bookings
        WHERE staff_id = :staff_id
          AND customer_id = :customer_id
          AND status NOT IN ('cancelled', 'no-show', 'completed')
          AND end_time_utc > NOW()
    """
    params: Dict[str, object] = {
        "staff_id": staff_id,
        "customer_id": customer_id,
    }
    if exclude_booking_id:
        query += " AND id <> :exclude_booking_id"
        params["exclude_booking_id"] = exclude_booking_id

    count = db.execute(query, params).scalar()
    if count is not None and int(count) >= limit:
        raise HTTPException(
            status_code=400,
            detail="Customer booking limit reached for this staff member",
        )

def _enforce_daily_slot_limit(
    db: Session,
    booking: BookingCreate,
    schedule: dict,
    local_date: date,
    ignore_booking_limits: bool = False,
):
    max_slots = schedule.get("max_slots_per_day")
    if max_slots is None:
        return
    max_slots = int(max_slots)
    if max_slots <= 0:
        raise HTTPException(status_code=400, detail="Selected time is not available")

    slots = _compute_slots_for_date(
        db=db,
        service_id=booking.service_id,
        target_date=local_date,
        timezone=booking.customer_timezone or "UTC",
        staff_id=booking.staff_id,
        location_id=None,
        granularity_minutes=settings.SLOT_GRANULARITY_MINUTES,
        window_start=None,
        window_end=None,
        min_notice_minutes=settings.MIN_NOTICE_MINUTES,
        max_booking_days=settings.MAX_BOOKING_DAYS,
        ignore_booking_limits=ignore_booking_limits,
    )
    requested_start = booking.start_time_utc.replace(second=0, microsecond=0)
    for slot in slots:
        if slot.get("staff_id") != booking.staff_id:
            continue
        slot_start = slot["start_time"].astimezone(dt_timezone.utc).replace(
            second=0, microsecond=0
        )
        if slot_start == requested_start:
            return

    raise HTTPException(status_code=400, detail="Selected time is not available")

def _normalize_json_field(value: Optional[object]) -> Optional[dict]:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return None

def _insert_booking_log(
    db: Session,
    booking_id: str,
    action: str,
    performed_by: Optional[str],
    details: Optional[dict] = None,
) -> None:
    """Persist booking logs without breaking booking flows on schema drift."""
    details_payload = (
        json.dumps(jsonable_encoder(details)) if details is not None else None
    )

    try:
        db.execute(
            text(
                """
                INSERT INTO booking_logs (id, booking_id, action, performed_by, details)
                VALUES (:id, :booking_id, :action, :performed_by, CAST(:details AS jsonb))
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "booking_id": booking_id,
                "action": action,
                "performed_by": performed_by,
                "details": details_payload,
            },
        )
        db.commit()
        return
    except SQLAlchemyError as exc:
        db.rollback()
        error_text = str(exc).lower()
        if "booking_logs" not in error_text or "details" not in error_text:
            logger.warning("Failed to write booking log with details: %s", exc)
            return

    # Fallback for legacy schemas that do not include booking_logs.details.
    try:
        db.execute(
            text(
                """
                INSERT INTO booking_logs (id, booking_id, action, performed_by)
                VALUES (:id, :booking_id, :action, :performed_by)
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "booking_id": booking_id,
                "action": action,
                "performed_by": performed_by,
            },
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning("Failed to write booking log fallback: %s", exc)

def _get_service_staff_config(
    db: Session,
    staff_id: str,
    service_id: str,
    current_user: dict,
) -> Dict[str, object]:
    row = db.execute(
        """
        SELECT s.duration_minutes, s.buffer_minutes, s.max_capacity,
               s.price, s.deposit_amount,
               s.is_active, s.is_archived, s.paused_from, s.paused_until,
               ss.staff_id as assigned_staff_id,
               ss.duration_override, ss.buffer_override, ss.capacity_override,
               ss.price_override, ss.deposit_override,
               ss.is_bookable, ss.is_temporarily_unavailable, ss.admin_only
        FROM services s
        LEFT JOIN staff_services ss
          ON ss.service_id = s.id AND ss.staff_id = :staff_id
        WHERE s.id = :service_id
        """,
        {"staff_id": staff_id, "service_id": service_id},
    ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Service not found")

    row_map = row._mapping
    if row_map.get("is_archived") or not row_map.get("is_active"):
        raise HTTPException(status_code=400, detail="Service is not available")

    now_utc = datetime.now(dt_timezone.utc)
    paused_from = row_map.get("paused_from")
    paused_until = row_map.get("paused_until")
    if paused_from and paused_from <= now_utc and (paused_until is None or paused_until >= now_utc):
        raise HTTPException(status_code=400, detail="Service is paused")

    if row_map.get("assigned_staff_id") is None:
        raise HTTPException(status_code=400, detail="Staff is not assigned to this service")

    if not row_map.get("is_bookable", True) or row_map.get("is_temporarily_unavailable"):
        raise HTTPException(status_code=400, detail="Staff is not bookable for this service")

    if row_map.get("admin_only") and current_user.get("role") == "customer":
        raise HTTPException(status_code=403, detail="Staff is only bookable by admin")

    duration = row_map.get("duration_override") or row_map.get("duration_minutes")
    buffer_minutes = row_map.get("buffer_override")
    if buffer_minutes is None:
        buffer_minutes = row_map.get("buffer_minutes") or 0
    capacity = row_map.get("capacity_override")
    if capacity is None:
        capacity = row_map.get("max_capacity") or 1
    price = row_map.get("price_override")
    if price is None:
        price = row_map.get("price") or 0
    deposit = row_map.get("deposit_override")
    if deposit is None:
        deposit = row_map.get("deposit_amount") or 0

    duration_val = int(duration or 0)
    buffer_val = int(buffer_minutes or 0)
    capacity_val = max(int(capacity or 1), 1)

    return {
        "duration": duration_val,
        "buffer": buffer_val,
        "capacity": capacity_val,
        "price": Decimal(str(price or 0)),
        "deposit": Decimal(str(deposit or 0)),
    }

def _validate_booking_source(booking_source: str, current_user: dict) -> None:
    if booking_source not in ALLOWED_BOOKING_SOURCES:
        raise HTTPException(status_code=400, detail="Invalid booking source")

    if current_user.get("role") == "customer":
        if booking_source not in {"web", "social"}:
            raise HTTPException(status_code=403, detail="Forbidden booking source")

def _check_slot_capacity(
    db: Session,
    staff_id: str,
    service_id: str,
    start_time_utc: datetime,
    end_time_utc: datetime,
    capacity: int,
    exclude_booking_id: Optional[str] = None,
    exclude_hold_by: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    booking_query = (
        "SELECT id, service_id FROM bookings "
        "WHERE staff_id = :staff_id "
        "AND status NOT IN ('cancelled', 'no-show') "
        "AND start_time_utc < :end_time AND end_time_utc > :start_time"
    )
    booking_params: Dict[str, object] = {
        "staff_id": staff_id,
        "start_time": start_time_utc,
        "end_time": end_time_utc,
    }
    if exclude_booking_id:
        booking_query += " AND id <> :exclude_id"
        booking_params["exclude_id"] = exclude_booking_id

    booking_rows = db.execute(booking_query, booking_params).fetchall()
    same_count = 0
    other_conflict = False
    for row in booking_rows:
        if row[1] != service_id:
            other_conflict = True
            break
        same_count += 1

    hold_query = (
        "SELECT service_id, created_by FROM booking_holds "
        "WHERE staff_id = :staff_id "
        "AND expires_at_utc > NOW() "
        "AND start_utc < :end_time AND end_utc > :start_time"
    )
    hold_params: Dict[str, object] = {
        "staff_id": staff_id,
        "start_time": start_time_utc,
        "end_time": end_time_utc,
    }
    if exclude_hold_by:
        hold_query += " AND (created_by IS NULL OR created_by <> :exclude_by)"
        hold_params["exclude_by"] = exclude_hold_by

    hold_rows = db.execute(hold_query, hold_params).fetchall()
    for row in hold_rows:
        if row[0] != service_id:
            other_conflict = True
            break
        same_count += 1

    if other_conflict:
        return False, "Time slot is not available"

    if capacity <= 1 and same_count > 0:
        return False, "Time slot is not available"

    if capacity > 1 and same_count >= capacity:
        return False, "Time slot is full"

    return True, None

def _iter_next_available_slots(
    db: Session,
    service_id: str,
    staff_id: str,
    timezone: str,
    start_date: date,
) -> List[dict]:
    slots: List[dict] = []
    for offset in range(settings.MAX_BOOKING_DAYS + 1):
        target_date = start_date + timedelta(days=offset)
        daily = _compute_slots_for_date(
            db=db,
            service_id=service_id,
            target_date=target_date,
            timezone=timezone,
            staff_id=staff_id,
            location_id=None,
            granularity_minutes=settings.SLOT_GRANULARITY_MINUTES,
            window_start=None,
            window_end=None,
            min_notice_minutes=settings.MIN_NOTICE_MINUTES,
            max_booking_days=settings.MAX_BOOKING_DAYS,
        )
        if daily:
            daily_sorted = sorted(daily, key=lambda s: s["start_time"])
            slots.extend(daily_sorted)
            break
    return slots

def _is_nth_weekday_in_month(target_date: date, weekday: int, nth: int) -> bool:
    if target_date.weekday() != weekday:
        return False
    first_day = target_date.replace(day=1)
    offset = (weekday - first_day.weekday()) % 7
    first_occurrence = 1 + offset
    occurrence = ((target_date.day - first_occurrence) // 7) + 1
    if nth == -1:
        last_day = calendar.monthrange(target_date.year, target_date.month)[1]
        last_date = target_date.replace(day=last_day)
        last_offset = (last_date.weekday() - weekday) % 7
        last_occurrence_day = last_day - last_offset
        return target_date.day == last_occurrence_day
    return occurrence == nth

def _service_allows_booking(
    db: Session,
    service_id: str,
    local_start: datetime,
    local_end: datetime,
    schedule_tz: ZoneInfo,
) -> bool:
    schedule = db.execute(
        """
        SELECT * FROM service_operating_schedules
        WHERE service_id = :service_id
          AND is_active = TRUE
          AND (effective_from IS NULL OR effective_from <= :date)
          AND (effective_to IS NULL OR effective_to >= :date)
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"service_id": service_id, "date": local_start.date()},
    ).fetchone()

    if not schedule:
        return True

    schedule_map = schedule._mapping
    service_tz = ZoneInfo(schedule_map.get("timezone") or "UTC")

    exceptions = db.execute(
        """
        SELECT is_open, start_time, end_time
        FROM service_operating_exceptions
        WHERE service_id = :service_id AND date = :date
        """,
        {"service_id": service_id, "date": local_start.date()},
    ).fetchall()

    start_utc = local_start.astimezone(dt_timezone.utc)
    end_utc = local_end.astimezone(dt_timezone.utc)

    if exceptions:
        override_exceptions = [ex for ex in exceptions if ex[0] and ex[1] and ex[2]]
        closed_exceptions = [ex for ex in exceptions if not ex[0]]
        extra_open_exceptions = [ex for ex in exceptions if ex[0] and not (ex[1] and ex[2])]

        if override_exceptions:
            for ex in override_exceptions:
                start_dt = datetime.combine(local_start.date(), ex[1], tzinfo=service_tz)
                end_dt = datetime.combine(local_start.date(), ex[2], tzinfo=service_tz)
                if start_utc >= start_dt.astimezone(dt_timezone.utc) and end_utc <= end_dt.astimezone(dt_timezone.utc):
                    return True
            return False
        if closed_exceptions:
            return False
        if extra_open_exceptions:
            day_start = datetime.combine(local_start.date(), time(0, 0), tzinfo=service_tz)
            day_end = day_start + timedelta(days=1)
            return start_utc >= day_start.astimezone(dt_timezone.utc) and end_utc <= day_end.astimezone(dt_timezone.utc)

    rule_type = schedule_map.get("rule_type")
    open_time = schedule_map.get("open_time")
    close_time = schedule_map.get("close_time")
    weekday = (local_start.weekday() + 1) % 7

    if rule_type == "daily":
        if open_time and close_time:
            start_dt = datetime.combine(local_start.date(), open_time, tzinfo=service_tz)
            end_dt = datetime.combine(local_start.date(), close_time, tzinfo=service_tz)
            return start_utc >= start_dt.astimezone(dt_timezone.utc) and end_utc <= end_dt.astimezone(dt_timezone.utc)
        return True

    rules = db.execute(
        """
        SELECT rule_type, weekday, month_day, nth, start_time, end_time
        FROM service_operating_rules
        WHERE schedule_id = :schedule_id
        """,
        {"schedule_id": schedule_map.get("id")},
    ).fetchall()

    for rule in rules:
        rule_type_value = rule[0]
        rule_weekday = rule[1]
        month_day = rule[2]
        nth = rule[3]
        start_time = rule[4]
        end_time = rule[5]

        is_match = False
        if rule_type == "weekly" and rule_type_value == "weekly":
            is_match = rule_weekday == weekday
        elif rule_type == "monthly" and rule_type_value == "monthly_day":
            is_match = month_day == local_start.date().day
        elif rule_type == "monthly" and rule_type_value == "monthly_nth_weekday":
            if rule_weekday is not None and nth is not None:
                is_match = _is_nth_weekday_in_month(local_start.date(), rule_weekday, nth)

        if not is_match:
            continue

        if start_time and end_time:
            start_dt = datetime.combine(local_start.date(), start_time, tzinfo=service_tz)
            end_dt = datetime.combine(local_start.date(), end_time, tzinfo=service_tz)
            return start_utc >= start_dt.astimezone(dt_timezone.utc) and end_utc <= end_dt.astimezone(dt_timezone.utc)
        if open_time and close_time:
            start_dt = datetime.combine(local_start.date(), open_time, tzinfo=service_tz)
            end_dt = datetime.combine(local_start.date(), close_time, tzinfo=service_tz)
            return start_utc >= start_dt.astimezone(dt_timezone.utc) and end_utc <= end_dt.astimezone(dt_timezone.utc)
        return True

    return False

def _get_customer_id(db: Session, user_id: str) -> str | None:
    record = db.execute(
        "SELECT id FROM customers WHERE user_id = :user_id",
        {"user_id": user_id},
    ).fetchone()
    return record[0] if record else None

def _ensure_booking_access(db: Session, booking_id: str, current_user: dict) -> dict:
    record = db.execute(
        "SELECT id, staff_id, customer_id FROM bookings WHERE id = :id",
        {"id": booking_id},
    ).fetchone()

    if not record:
        raise HTTPException(status_code=404, detail="Booking not found")

    booking = {"id": record[0], "staff_id": record[1], "customer_id": record[2]}

    if is_admin(current_user):
        return booking

    role = current_user.get("role")
    if role == "staff":
        if booking["staff_id"] != current_user.get("id"):
            raise HTTPException(status_code=403, detail="Forbidden")
        return booking

    if role == "customer":
        customer_id = _get_customer_id(db, current_user.get("id"))
        if not customer_id or booking["customer_id"] != customer_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        return booking

    raise HTTPException(status_code=403, detail="Forbidden")

@router.post("/", response_model=BookingResponse)
async def create_booking(
    booking: BookingCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create a new booking"""
    booking_id = str(uuid.uuid4())
    booking.start_time_utc = _normalize_utc_datetime(booking.start_time_utc)

    role = current_user.get("role")
    if role == "customer":
        customer_id = _get_customer_id(db, current_user.get("id"))
        if not customer_id:
            raise HTTPException(status_code=403, detail="Customer profile not found")
        booking.customer_id = customer_id
    elif role == "staff":
        if booking.staff_id != current_user.get("id"):
            raise HTTPException(status_code=403, detail="Forbidden")

    _validate_booking_source(booking.booking_source, current_user)

    service_config = _get_service_staff_config(
        db,
        staff_id=booking.staff_id,
        service_id=booking.service_id,
        current_user=current_user,
    )
    duration_minutes = service_config["duration"]
    buffer_minutes = service_config["buffer"]
    capacity = service_config["capacity"]
    price_amount = Decimal(str(service_config["price"]))
    deposit_amount = Decimal(str(service_config["deposit"]))
    amount_due_now = deposit_amount if deposit_amount > 0 else price_amount
    booking_status = "pending" if amount_due_now > 0 else "confirmed"
    payment_status = "pending" if amount_due_now > 0 else "paid"

    end_time_utc = booking.start_time_utc + timedelta(minutes=duration_minutes + buffer_minutes)

    schedule = _get_staff_schedule(db, booking.staff_id, booking.start_time_utc.date())
    if not schedule:
        raise HTTPException(status_code=400, detail="Staff schedule is not configured")

    schedule_tz = ZoneInfo(schedule["timezone"])
    local_start = booking.start_time_utc.astimezone(schedule_tz)
    local_end = end_time_utc.astimezone(schedule_tz)
    local_date = local_start.date()

    schedule = _get_staff_schedule(db, booking.staff_id, local_date)
    if not schedule:
        raise HTTPException(status_code=400, detail="Staff schedule is not configured")

    schedule_tz = ZoneInfo(schedule["timezone"])
    local_start = booking.start_time_utc.astimezone(schedule_tz)
    local_end = end_time_utc.astimezone(schedule_tz)
    local_date = local_start.date()

    _enforce_daily_booking_limit(
        db=db,
        staff_id=booking.staff_id,
        schedule=schedule,
        local_date=local_date,
        schedule_tz=schedule_tz,
    )
    _enforce_per_customer_limit(
        db=db,
        staff_id=booking.staff_id,
        customer_id=booking.customer_id,
        schedule=schedule,
    )
    _enforce_daily_slot_limit(
        db=db,
        booking=booking,
        schedule=schedule,
        local_date=local_date,
    )

    if not _service_allows_booking(db, booking.service_id, local_start, local_end, schedule_tz):
        raise HTTPException(status_code=400, detail="Service is not available at this time")
    weekday = (local_start.weekday() + 1) % 7

    now_local = datetime.now(schedule_tz)
    min_notice_cutoff = now_local + timedelta(minutes=settings.MIN_NOTICE_MINUTES)
    if local_start < min_notice_cutoff:
        raise HTTPException(status_code=400, detail="Booking does not meet minimum notice")

    max_booking_cutoff = now_local + timedelta(days=settings.MAX_BOOKING_DAYS)
    if local_start > max_booking_cutoff:
        raise HTTPException(status_code=400, detail="Booking exceeds maximum window")

    work_blocks = db.execute(
        """
        SELECT start_time_local, end_time_local
        FROM staff_work_blocks
        WHERE schedule_id = :schedule_id AND weekday = :weekday
        """,
        {"schedule_id": schedule["id"], "weekday": weekday},
    ).fetchall()

    if not work_blocks:
        raise HTTPException(status_code=400, detail="Staff is not available on this day")

    fits_work_block = False
    for block in work_blocks:
        block_start = datetime.combine(local_start.date(), block[0], tzinfo=schedule_tz)
        block_end = datetime.combine(local_start.date(), block[1], tzinfo=schedule_tz)
        if local_start >= block_start and local_end <= block_end:
            fits_work_block = True
            break

    if not fits_work_block:
        raise HTTPException(status_code=400, detail="Time is outside staff working hours")

    break_blocks = db.execute(
        """
        SELECT start_time_local, end_time_local
        FROM staff_break_blocks
        WHERE schedule_id = :schedule_id AND weekday = :weekday
        """,
        {"schedule_id": schedule["id"], "weekday": weekday},
    ).fetchall()

    for block in break_blocks:
        block_start = datetime.combine(local_start.date(), block[0], tzinfo=schedule_tz)
        block_end = datetime.combine(local_start.date(), block[1], tzinfo=schedule_tz)
        if local_start < block_end and local_end > block_start:
            raise HTTPException(status_code=400, detail="Time overlaps a staff break")

    exception_result = db.execute(
        """
        SELECT id FROM staff_exceptions
        WHERE staff_id = :staff_id
          AND type IN ('time_off', 'blocked_time')
          AND start_utc < :end_time AND end_utc > :start_time
        """,
        {
            "staff_id": booking.staff_id,
            "start_time": booking.start_time_utc,
            "end_time": end_time_utc,
        },
    )

    if exception_result.fetchone():
        raise HTTPException(status_code=400, detail="Staff is unavailable for this time")

    slot_ok, error_message = _check_slot_capacity(
        db=db,
        staff_id=booking.staff_id,
        service_id=booking.service_id,
        start_time_utc=booking.start_time_utc,
        end_time_utc=end_time_utc,
        capacity=capacity,
        exclude_hold_by=current_user.get("id"),
    )
    if not slot_ok:
        raise HTTPException(status_code=400, detail=error_message)
    
    # Create booking
    db.execute(
        text("""
        INSERT INTO bookings (id, service_id, staff_id, customer_id, start_time_utc,
                            end_time_utc, booking_source, customer_timezone, status,
                            payment_status, location_id)
        VALUES (:id, :service_id, :staff_id, :customer_id, :start_time_utc,
                :end_time_utc, :booking_source, :customer_timezone, :status,
                :payment_status, :location_id)
        """),
        {
            "id": booking_id,
            "service_id": booking.service_id,
            "staff_id": booking.staff_id,
            "customer_id": booking.customer_id,
            "start_time_utc": booking.start_time_utc,
            "end_time_utc": end_time_utc,
            "booking_source": booking.booking_source,
            "customer_timezone": booking.customer_timezone,
            "status": booking_status,
            "payment_status": payment_status,
            "location_id": booking.location_id,
        }
    )
    db.commit()

    db.execute(
        """
        DELETE FROM booking_holds
        WHERE staff_id = :staff_id
          AND start_utc < :end_time
          AND end_utc > :start_time
          AND (created_by IS NULL OR created_by = :created_by)
        """,
        {
            "staff_id": booking.staff_id,
            "start_time": booking.start_time_utc,
            "end_time": end_time_utc,
            "created_by": current_user.get("id"),
        },
    )
    db.commit()
    
    _insert_booking_log(
        db=db,
        booking_id=booking_id,
        action="created",
        performed_by=current_user.get("id"),
        details={
            "service_id": booking.service_id,
            "staff_id": booking.staff_id,
            "customer_id": booking.customer_id,
            "start_time_utc": booking.start_time_utc.isoformat(),
            "end_time_utc": end_time_utc.isoformat(),
            "booking_source": booking.booking_source,
        },
    )

    if amount_due_now <= 0:
        _send_booking_emails(db, booking_id, "confirmation")
    
    result = db.execute(
        "SELECT * FROM bookings WHERE id = :id",
        {"id": booking_id}
    )
    return jsonable_encoder(dict(result.fetchone()._mapping))

@router.get("/{booking_id}", response_model=BookingWithDetails)
async def get_booking(
    booking_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get booking by ID with details"""
    _ensure_booking_access(db, booking_id, current_user)
    result = db.execute(
        """
        SELECT b.*, s.name as service_name, s.price as service_price,
               u.full_name as staff_name, c.full_name as customer_name
        FROM bookings b
        LEFT JOIN services s ON b.service_id = s.id
        LEFT JOIN users u ON b.staff_id = u.id
        LEFT JOIN customers c ON b.customer_id = c.id
        WHERE b.id = :id
        """,
        {"id": booking_id}
    )
    
    booking = result.fetchone()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    
    return jsonable_encoder(dict(booking._mapping))

@router.post("/{booking_id}/rebook", response_model=BookingResponse)
async def rebook_booking(
    booking_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Rebook the same service/staff at the next available slot."""
    _ensure_booking_access(db, booking_id, current_user)

    record = db.execute(
        "SELECT * FROM bookings WHERE id = :id",
        {"id": booking_id},
    ).fetchone()

    if not record:
        raise HTTPException(status_code=404, detail="Booking not found")

    booking_map = record._mapping
    service_id = booking_map["service_id"]
    staff_id = booking_map["staff_id"]
    customer_id = booking_map["customer_id"]

    timezone = _resolve_customer_timezone(
        booking_timezone=booking_map.get("customer_timezone"),
        user_timezone=current_user.get("timezone"),
    )
    start_date = datetime.now(ZoneInfo(timezone)).date()

    candidate_slots = _iter_next_available_slots(
        db=db,
        service_id=service_id,
        staff_id=staff_id,
        timezone=timezone,
        start_date=start_date,
    )

    if not candidate_slots:
        raise HTTPException(status_code=409, detail="No available slots")

    booking_source = "web" if current_user.get("role") == "customer" else "admin"

    last_error: Optional[str] = None
    for slot in candidate_slots:
        start_time_local = slot.get("start_time")
        if not start_time_local:
            continue
        start_time_utc = start_time_local.astimezone(dt_timezone.utc)

        booking_payload = BookingCreate(
            service_id=service_id,
            staff_id=staff_id,
            customer_id=customer_id,
            start_time_utc=start_time_utc,
            booking_source=booking_source,
            customer_timezone=timezone,
        )

        try:
            return await create_booking(booking_payload, current_user, db)
        except HTTPException as exc:
            if exc.status_code in (400, 409):
                last_error = str(exc.detail)
                continue
            raise

    raise HTTPException(
        status_code=409,
        detail=last_error or "No available slots",
    )

@router.get("/{booking_id}/logs", response_model=List[BookingLogResponse])
async def get_booking_logs(
    booking_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get audit logs for a booking."""
    _ensure_booking_access(db, booking_id, current_user)
    rows = db.execute(
        "SELECT * FROM booking_logs WHERE booking_id = :id ORDER BY created_at DESC",
        {"id": booking_id},
    ).fetchall()
    logs = []
    for row in rows:
        data = dict(row._mapping)
        data["details"] = _normalize_json_field(data.get("details"))
        logs.append(jsonable_encoder(data))
    return logs

@router.get("/{booking_id}/changes", response_model=List[BookingChangeResponse])
async def get_booking_changes(
    booking_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get reschedule/cancel history for a booking."""
    _ensure_booking_access(db, booking_id, current_user)
    rows = db.execute(
        "SELECT * FROM booking_changes WHERE booking_id = :id ORDER BY created_at DESC",
        {"id": booking_id},
    ).fetchall()
    return [jsonable_encoder(dict(row._mapping)) for row in rows]

@router.get("/{booking_id}/payment")
async def get_booking_for_payment(
    booking_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return booking details needed for payment screen."""
    _ensure_booking_access(db, booking_id, current_user)
    result = db.execute(
        text("""
        SELECT b.id, b.status, b.payment_status, b.start_time_utc,
               s.name as service_name, s.price, s.deposit_amount, s.duration_minutes,
               u.full_name as staff_name,
               l.name as location_name, l.address as location_address,
               l.latitude, l.longitude
        FROM bookings b
        LEFT JOIN services s ON b.service_id = s.id
        LEFT JOIN users u ON b.staff_id = u.id
        LEFT JOIN locations l ON b.location_id = l.id
        WHERE b.id = :id
        """),
        {"id": booking_id},
    ).fetchone()

    if not result:
        raise HTTPException(status_code=404, detail="Booking not found")

    r = result._mapping
    return {
        "id": r["id"],
        "status": r["status"],
        "payment_status": r["payment_status"],
        "start_time_utc": r["start_time_utc"],
        "services": {
            "name": r["service_name"],
            "price": r["price"],
            "deposit_amount": r["deposit_amount"],
            "duration_minutes": r["duration_minutes"],
        },
        "staff": {"full_name": r["staff_name"]},
        "location": {
            "name": r["location_name"],
            "address": r["location_address"],
            "latitude": r["latitude"],
            "longitude": r["longitude"],
        } if r["location_name"] else None,
    }

@router.get("/{booking_id}/confirmed")
async def get_booking_confirmed(
    booking_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return booking confirmation details."""
    _ensure_booking_access(db, booking_id, current_user)
    result = db.execute(
        """
        SELECT b.id, b.status, b.payment_status, b.start_time_utc,
               s.name as service_name, s.price, s.duration_minutes,
               u.full_name as staff_name, u.phone as staff_phone
        FROM bookings b
        LEFT JOIN services s ON b.service_id = s.id
        LEFT JOIN users u ON b.staff_id = u.id
        WHERE b.id = :id
        """,
        {"id": booking_id},
    ).fetchone()

    if not result:
        raise HTTPException(status_code=404, detail="Booking not found")

    return {
        "id": result[0],
        "status": result[1],
        "payment_status": result[2],
        "start_time_utc": result[3],
        "services": {
            "name": result[4],
            "price": result[5],
            "duration_minutes": result[6],
        },
        "staff": {"full_name": result[7], "phone": result[8]},
    }

@router.get("/", response_model=List[BookingWithDetails])
async def get_bookings(
    customer_id: str = None,
    staff_id: str = None,
    service_id: str = None,
    status: str = None,
    start_date: datetime = None,
    end_date: datetime = None,
    skip: int = 0,
    limit: int = 100,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get bookings with filters"""
    query = """
        SELECT b.*, s.name as service_name, s.price as service_price,
               u.full_name as staff_name, c.full_name as customer_name,
               l.id as location_record_id, l.name as location_name,
               l.address as location_address, l.latitude as location_latitude,
               l.longitude as location_longitude, l.timezone as location_timezone,
               r.id as review_id, r.rating as review_rating
        FROM bookings b
        LEFT JOIN services s ON b.service_id = s.id
        LEFT JOIN users u ON b.staff_id = u.id
        LEFT JOIN customers c ON b.customer_id = c.id
        LEFT JOIN locations l ON b.location_id = l.id
        LEFT JOIN reviews r ON r.booking_id = b.id
        WHERE 1=1
    """
    params = {}
    
    role = current_user.get("role")
    if is_admin(current_user):
        pass
    elif role == "staff":
        staff_id = current_user.get("id")
    elif role == "customer":
        customer_id = _get_customer_id(db, current_user.get("id"))
        if not customer_id:
            raise HTTPException(status_code=403, detail="Customer profile not found")
    else:
        raise HTTPException(status_code=403, detail="Forbidden")

    if customer_id:
        query += " AND b.customer_id = :customer_id"
        params["customer_id"] = customer_id
    if staff_id:
        query += " AND b.staff_id = :staff_id"
        params["staff_id"] = staff_id
    if service_id:
        query += " AND b.service_id = :service_id"
        params["service_id"] = service_id
    if status:
        query += " AND b.status = :status"
        params["status"] = status
    if start_date:
        query += " AND b.start_time_utc >= :start_date"
        params["start_date"] = start_date
    if end_date:
        query += " AND b.start_time_utc <= :end_date"
        params["end_date"] = end_date
    
    query += f" ORDER BY b.start_time_utc DESC LIMIT {limit} OFFSET {skip}"
    
    result = db.execute(query, params)
    bookings = result.fetchall()
    rows = []
    for row in bookings:
        d = dict(row._mapping)
        location_record_id = d.pop("location_record_id", None)
        location_name = d.pop("location_name", None)
        location_address = d.pop("location_address", None)
        location_latitude = d.pop("location_latitude", None)
        location_longitude = d.pop("location_longitude", None)
        location_timezone = d.pop("location_timezone", None)
        review_id = d.pop("review_id", None)
        review_rating = d.pop("review_rating", None)
        d["location"] = (
            {
                "id": str(location_record_id),
                "name": location_name,
                "address": location_address,
                "latitude": location_latitude,
                "longitude": location_longitude,
                "timezone": location_timezone,
            }
            if location_record_id is not None
            else None
        )
        d["review"] = (
            {"id": str(review_id), "rating": int(review_rating)}
            if review_id is not None
            else None
        )
        rows.append(d)
    return rows

@router.put("/{booking_id}", response_model=BookingResponse)
async def update_booking(
    booking_id: str,
    booking: BookingUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update a booking"""
    # Get current booking
    _ensure_booking_access(db, booking_id, current_user)
    result = db.execute("SELECT * FROM bookings WHERE id = :id", {"id": booking_id})
    current_booking = result.fetchone()
    if not current_booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    booking_map = current_booking._mapping
    staff_id = booking_map["staff_id"]
    service_id = booking_map["service_id"]
    old_start_time = booking_map["start_time_utc"]
    old_status = booking_map["status"]
    old_payment_status = booking_map["payment_status"]

    updates = []
    params = {"id": booking_id}
    change_type = None

    if booking.start_time_utc is not None:
        if current_user.get("role") == "staff" and not is_admin(current_user):
            raise HTTPException(status_code=403, detail="Forbidden")
        if old_status in {"cancelled", "completed", "no-show"}:
            raise HTTPException(status_code=400, detail="Cannot reschedule this booking")

        booking.start_time_utc = _normalize_utc_datetime(booking.start_time_utc)

        service_config = _get_service_staff_config(
            db,
            staff_id=staff_id,
            service_id=service_id,
            current_user=current_user,
        )
        duration_minutes = service_config["duration"]
        buffer_minutes = service_config["buffer"]
        capacity = service_config["capacity"]
        end_time_utc = booking.start_time_utc + timedelta(minutes=duration_minutes + buffer_minutes)

        schedule = _get_staff_schedule(db, staff_id, booking.start_time_utc.date())
        if not schedule:
            raise HTTPException(status_code=400, detail="Staff schedule is not configured")

        schedule_tz = ZoneInfo(schedule["timezone"])
        local_start = booking.start_time_utc.astimezone(schedule_tz)
        local_end = end_time_utc.astimezone(schedule_tz)
        local_date = local_start.date()

        schedule = _get_staff_schedule(db, staff_id, local_date)
        if not schedule:
            raise HTTPException(status_code=400, detail="Staff schedule is not configured")

        schedule_tz = ZoneInfo(schedule["timezone"])
        local_start = booking.start_time_utc.astimezone(schedule_tz)
        local_end = end_time_utc.astimezone(schedule_tz)
        local_date = local_start.date()

        _enforce_daily_booking_limit(
            db=db,
            staff_id=staff_id,
            schedule=schedule,
            local_date=local_date,
            schedule_tz=schedule_tz,
            exclude_booking_id=booking_id,
        )
        _enforce_per_customer_limit(
            db=db,
            staff_id=staff_id,
            customer_id=booking_map["customer_id"],
            schedule=schedule,
            exclude_booking_id=booking_id,
        )
        _enforce_daily_slot_limit(
            db=db,
            booking=BookingCreate(
                service_id=service_id,
                staff_id=staff_id,
                customer_id=booking_map["customer_id"],
                start_time_utc=booking.start_time_utc,
                booking_source=booking_map["booking_source"],
                customer_timezone=_resolve_customer_timezone(
                    booking_timezone=booking_map.get("customer_timezone"),
                    user_timezone=current_user.get("timezone"),
                ),
            ),
            schedule=schedule,
            local_date=local_date,
            ignore_booking_limits=True,
        )

        if not _service_allows_booking(db, service_id, local_start, local_end, schedule_tz):
            raise HTTPException(status_code=400, detail="Service is not available at this time")

        now_local = datetime.now(schedule_tz)
        min_notice_cutoff = now_local + timedelta(minutes=settings.MIN_NOTICE_MINUTES)
        if local_start < min_notice_cutoff:
            raise HTTPException(status_code=400, detail="Booking does not meet minimum notice")

        max_booking_cutoff = now_local + timedelta(days=settings.MAX_BOOKING_DAYS)
        if local_start > max_booking_cutoff:
            raise HTTPException(status_code=400, detail="Booking exceeds maximum window")

        weekday = (local_start.weekday() + 1) % 7
        work_blocks = db.execute(
            """
            SELECT start_time_local, end_time_local
            FROM staff_work_blocks
            WHERE schedule_id = :schedule_id AND weekday = :weekday
            """,
            {"schedule_id": schedule["id"], "weekday": weekday},
        ).fetchall()

        if not work_blocks:
            raise HTTPException(status_code=400, detail="Staff is not available on this day")

        fits_work_block = False
        for block in work_blocks:
            block_start = datetime.combine(local_start.date(), block[0], tzinfo=schedule_tz)
            block_end = datetime.combine(local_start.date(), block[1], tzinfo=schedule_tz)
            if local_start >= block_start and local_end <= block_end:
                fits_work_block = True
                break

        if not fits_work_block:
            raise HTTPException(status_code=400, detail="Time is outside staff working hours")

        break_blocks = db.execute(
            """
            SELECT start_time_local, end_time_local
            FROM staff_break_blocks
            WHERE schedule_id = :schedule_id AND weekday = :weekday
            """,
            {"schedule_id": schedule["id"], "weekday": weekday},
        ).fetchall()

        for block in break_blocks:
            block_start = datetime.combine(local_start.date(), block[0], tzinfo=schedule_tz)
            block_end = datetime.combine(local_start.date(), block[1], tzinfo=schedule_tz)
            if local_start < block_end and local_end > block_start:
                raise HTTPException(status_code=400, detail="Time overlaps a staff break")

        exception_result = db.execute(
            """
            SELECT id FROM staff_exceptions
            WHERE staff_id = :staff_id
              AND type IN ('time_off', 'blocked_time')
              AND start_utc < :end_time AND end_utc > :start_time
            """,
            {
                "staff_id": staff_id,
                "start_time": booking.start_time_utc,
                "end_time": end_time_utc,
            },
        )

        if exception_result.fetchone():
            raise HTTPException(status_code=400, detail="Staff is unavailable for this time")

        slot_ok, error_message = _check_slot_capacity(
            db=db,
            staff_id=staff_id,
            service_id=service_id,
            start_time_utc=booking.start_time_utc,
            end_time_utc=end_time_utc,
            capacity=capacity,
            exclude_booking_id=booking_id,
            exclude_hold_by=current_user.get("id"),
        )
        if not slot_ok:
            raise HTTPException(status_code=400, detail=error_message)

        updates.append("start_time_utc = :start_time_utc")
        updates.append("end_time_utc = :end_time_utc")
        params["start_time_utc"] = booking.start_time_utc
        params["end_time_utc"] = end_time_utc
        change_type = "reschedule"

    if booking.status is not None:
        if current_user.get("role") == "customer":
            raise HTTPException(status_code=403, detail="Forbidden")
        updates.append("status = :status")
        params["status"] = booking.status
        if not change_type:
            change_type = "cancel" if booking.status == "cancelled" else "status_update"

    if booking.payment_status is not None:
        if current_user.get("role") == "customer" and not is_admin(current_user):
            raise HTTPException(status_code=403, detail="Forbidden")
        updates.append("payment_status = :payment_status")
        params["payment_status"] = booking.payment_status

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    query = f"UPDATE bookings SET {', '.join(updates)} WHERE id = :id"
    db.execute(query, params)
    db.commit()
    
    # Log the change
    if change_type:
        db.execute(
            """
            INSERT INTO booking_changes (id, booking_id, old_start_time, new_start_time, 
                                        change_type, changed_by)
            VALUES (:id, :booking_id, :old_start_time, :new_start_time, :change_type, :changed_by)
            """,
            {
                "id": str(uuid.uuid4()),
                "booking_id": booking_id,
                "old_start_time": old_start_time if change_type == "reschedule" else None,
                "new_start_time": booking.start_time_utc if change_type == "reschedule" else None,
                "change_type": change_type,
                "changed_by": current_user.get("id"),
            },
        )
        db.commit()

    log_details: Dict[str, object] = {}
    if booking.start_time_utc is not None:
        log_details["old_start_time_utc"] = old_start_time.isoformat()
        log_details["new_start_time_utc"] = booking.start_time_utc.isoformat()
    if booking.status is not None:
        log_details["old_status"] = old_status
        log_details["new_status"] = booking.status
    if booking.payment_status is not None:
        log_details["old_payment_status"] = old_payment_status
        log_details["new_payment_status"] = booking.payment_status

    log_action = "updated"
    if change_type == "reschedule":
        log_action = "rescheduled"
    elif change_type == "cancel":
        log_action = "cancelled"
    elif change_type == "status_update":
        log_action = "status_updated"
    elif booking.payment_status is not None and booking.status is None and booking.start_time_utc is None:
        log_action = "payment_updated"

    _insert_booking_log(
        db=db,
        booking_id=booking_id,
        action=log_action,
        performed_by=current_user.get("id"),
        details=log_details if log_details else None,
    )

    if change_type == "reschedule":
        _send_booking_emails(db, booking_id, "confirmation")
    if booking.status is not None and booking.status == "cancelled":
        _send_booking_emails(db, booking_id, "cancellation")
    
    result = db.execute(
        "SELECT * FROM bookings WHERE id = :id",
        {"id": booking_id}
    )
    return jsonable_encoder(dict(result.fetchone()._mapping))

@router.delete("/{booking_id}")
async def cancel_booking(
    booking_id: str,
    reason: str = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cancel a booking"""
    _ensure_booking_access(db, booking_id, current_user)
    current_status = db.execute(
        "SELECT status FROM bookings WHERE id = :id",
        {"id": booking_id},
    ).fetchone()
    db.execute(
        "UPDATE bookings SET status = 'cancelled' WHERE id = :id",
        {"id": booking_id}
    )
    db.commit()
    
    # Log the cancellation
    db.execute(
        """
        INSERT INTO booking_changes (id, booking_id, change_type, changed_by, reason)
        VALUES (:id, :booking_id, 'cancel', :changed_by, :reason)
        """,
        {
            "id": str(uuid.uuid4()),
            "booking_id": booking_id,
            "changed_by": current_user.get("id"),
            "reason": reason,
        }
    )
    db.commit()

    _insert_booking_log(
        db=db,
        booking_id=booking_id,
        action="cancelled",
        performed_by=current_user.get("id"),
        details={
            "old_status": current_status[0] if current_status else None,
            "new_status": "cancelled",
            "reason": reason,
        },
    )

    _send_booking_emails(db, booking_id, "cancellation")
    
    return {"message": "Booking cancelled"}
